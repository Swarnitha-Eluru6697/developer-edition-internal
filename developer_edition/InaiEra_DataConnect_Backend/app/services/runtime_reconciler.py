"""
DataConnect runtime controller/reconciler.

Lifecycle endpoints (deploy/pause/resume/restart/delete) durably record what
*should* be true — desired_state, desired_generation, config_hash — in
Postgres (via `record_desired`) before they ever touch Kafka Connect. This
reconciler then runs continuously, comparing that durable specification
against what Kafka Connect actually reports, and converges the two:

- If the process crashes right after Connect accepted a request but before
  anything else was saved, the desired intent already survived the crash —
  the next tick re-checks Connect directly and finishes the job.
- If Connect later fails a connector, a task fails, someone edits it by hand
  through the Connect REST API, or Connect restarts and drops state, the next
  tick sees the drift (config hash mismatch, unexpected state, failed task)
  and corrects it — redeploying the config and/or restarting the connector.
- A network timeout after Connect applied a change is never trusted at face
  value: observed_state always comes from asking Connect directly, never
  from the outcome of the request that may or may not have landed.

The decision logic (`derive_observed_state`, `plan_actions`, `is_converged`,
`next_generation`) is pure and side-effect free so it can be unit tested
without a database or HTTP connection, the same way
`MonitoringService.aggregate_connector_statuses` is tested.

Pipelines, connection profiles, schema versions, transform policies,
connector templates, and topic policies are all Postgres-authoritative (see
their respective services) — `_resolve_config` and `_reconcile_row` read
every one of them through those services rather than any in-memory store.
`pipeline_service`/`connection_service`/`schema_service`/`transform_service`
are imported locally inside the functions that use them, not at module
level: `pipeline_service.py` imports `runtime_reconciler` (for
`config_hash_of`/`runtime_reconciler`) at ITS module level — existing tests
monkeypatch `app.services.pipeline_service.runtime_reconciler.record_desired`
as a module attribute, so that side stays top-level — which means this
module importing `pipeline_service` at ITS own top level would be a genuine
circular import. Deferring the import to inside `_reconcile_row` (the only
place it's needed) breaks the cycle without disturbing that existing
convention.

GAP-08 — two race conditions this module must protect against, since
`app/main.py` starts `run_forever()` as a plain `asyncio.create_task` with
no coordination promised beyond this module's own:

1. Multiple reconciler processes. Nothing stops two API server replicas
   from each running their own `run_forever()` loop against the same
   Postgres. Without coordination, both would reconcile the same pipeline
   on overlapping ticks — issuing duplicate/conflicting Kafka Connect calls
   (e.g. both pausing and resuming the same connector in the same window).
   `_claim_rows` (a claim/lease query, same shape as BUG-02's
   `operation_runner._claim_next` but claiming an entire batch every tick
   rather than one row once) makes sure only one live process's tick ever
   acts on a given PipelineRuntimeState row: every tick, a process
   atomically claims (or renews its own already-held lease on) every row
   that's unleased, already its own, or whose lease has lapsed — see
   `leased_by`/`lease_expires_at` on the model. If process A dies, it stops
   renewing; once `runtime_reconciler_lease_seconds` passes, process B's
   next claim query sees the lapsed lease and takes over.

   A claim is only checked when it is made, and rows are then worked one at
   a time with real Connect calls in between, so a slow tick can outlive its
   lease on a late row. Three things close that: each row's lease is renewed
   (and the row skipped if another process holds it now) just before it is
   worked; `_still_current` checks lease ownership as well as generation
   before every action and every deploy retry; and the result is written
   under a row lock taken by `PipelineRuntimeStateRepository.fence`, which
   confirms in the same step that this process still owns the row — a tick
   that lost the row discards its result instead of overwriting the new
   owner's. Rows are committed one at a time so those locks are short.

2. A reconciliation racing a concurrent edit. `_reconcile_row` reads
   `row.desired_state`/`desired_generation` once, near the start of a tick,
   and may spend real wall-clock time afterward (resolving config, calling
   Kafka Connect) before actually acting on that snapshot. If a PATCH lands
   on the SAME pipeline in the meantime (a different request, a different
   DB transaction, via `record_desired`) and bumps `desired_generation`,
   this tick's already-planned actions were decided against stale intent.
   `deploy_if_current` already re-checks generation before every attempt
   for the deploy action specifically (kafka_connect_retry.py) — GAP-08
   adds the same guarantee for delete/pause/resume/restart, which don't
   retry and previously had no re-check at all: a single freshness check
   right before `_reconcile_row` acts on any of them, skipping every
   action for that row this tick (and not stamping `observed_generation`)
   if the desired_generation it planned against is already stale — the
   next tick reads the fresh row and acts correctly instead.

   Three further rules keep the result from clobbering newer intent: the
   config hash is recorded as applied only when the deploy really ran and
   nothing newer was recorded (a skipped deploy used to store its hash, so
   the next tick saw "no change" and never deployed it); the reconciler's own
   desired-state change (the error-threshold pause) is a conditional update on
   the generation it read; and `record_desired` reads its row FOR UPDATE, with
   pipeline edits bumping the generation (`bump_generation`), so two writers
   queue and an in-flight tick sees an edit as newer.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value

from app.core.audit import record_audit
from app.core.config import settings
from app.core.errors import ConnectorCompatibilityError
from app.core.metrics import record_operation
from app.core.redaction import redact_diagnostic, redact_secrets
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import PipelineRuntimeStateRepository
from app.schemas.pipeline import (
    DEFAULT_ERROR_THRESHOLD_MAX_ERRORS,
    DEFAULT_ERROR_THRESHOLD_WINDOW_SECONDS,
)
from app.services.connect_config import build_connector
from app.services.connect_runtime import pool_endpoint
from app.services.error_threshold import count_recent_connector_errors
from app.services.kafka_connect_retry import (
    delete_converged,
    deploy_if_current,
    get_status_retryable,
    list_connectors_retryable,
    pause_if_needed,
    restart_once,
    resume_if_needed,
)
from app.services.quality_worker_health import quality_stall
from app.services.runtime_orchestrator import KafkaConnectClient
from app.services.safe_error import to_safe_error
from app.services.secrets import sanitize_probe_message
from app.services.topic_naming import (
    build_connector_quarantine_topic_name,
)
from app.services.topic_preflight import assert_topic_policy_feasible

logger = structlog.get_logger(__name__)

# Shown as the pipeline's runtime error while its connector is still on the shared cluster though its pool now has a cluster of
# its own — see RuntimeReconciler._lives_on_the_shared_cluster. Fixed text: nothing from Connect is echoed.
_ON_SHARED_CLUSTER_MESSAGE = (
    "This connector is still running on the shared Kafka Connect cluster, but its worker pool now has a cluster of its own. "
    "It has not been created there to avoid running two copies: pause and remove it from the shared cluster (carrying its "
    "offsets over if it must resume) and the reconciler will create it on the pool's cluster."
)

# Reconciler-side observed states, derived purely from Kafka Connect's response,
# mapped onto the pipeline's user-facing status. "absent" and "unknown" are
# deliberately excluded: "absent" is handled specially (delete completion /
# orphan cleanup) and "unknown" must never silently overwrite whatever
# transitional status (deploying/pausing/...) the user is actively waiting on.
OBSERVED_TO_PIPELINE_STATUS = {
    "running": "running",
    "paused": "paused",
    "degraded": "degraded",
    "failed": "failed",
}


def config_hash_of(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def derive_observed_state(connector_exists: bool, status: dict[str, Any] | None) -> str:
    """Turn a Kafka Connect /status response into one of our observed_state values."""
    if not connector_exists:
        return "absent"
    if status is None:
        return "unknown"
    connector_state = (status.get("connector") or {}).get("state", "UNKNOWN")
    task_states = [t.get("state") for t in status.get("tasks", [])]
    if connector_state in ("FAILED", "UNASSIGNED"):
        return "failed"
    if "FAILED" in task_states:
        # The connector itself is up but one or more tasks are down — worth
        # distinguishing from a fully failed connector: some data may still
        # be flowing on the surviving tasks.
        return "degraded"
    if connector_state == "PAUSED":
        return "paused"
    if connector_state == "RUNNING":
        return "running"
    return "unknown"


CONNECT_FAILURE_PREFIX = "Kafka Connect reports"


def connect_failure_reason(status: dict[str, Any] | None) -> str | None:
    """Why Kafka Connect says the connector (or one of its tasks) FAILED: the first line of its trace, which is the exception and its
    message — e.g. "...ConfigException: TimestampConverter requires format option ...". None when nothing failed. Without this a
    connector Connect itself reported as failed showed no reason anywhere, because last_runtime_error only ever held errors from
    our own calls to Connect."""
    if not status:
        return None
    candidates = [status.get("connector") or {}, *(status.get("tasks") or [])]
    failed = next((c for c in candidates if c.get("state") == "FAILED"), None)
    if failed is None:
        return None
    first_line = (failed.get("trace") or "").strip().split("\n", 1)[0].strip()
    label = f"task {failed['id']}" if "id" in failed else "connector"
    return sanitize_probe_message(f"{CONNECT_FAILURE_PREFIX} the {label} FAILED: {first_line or 'no reason given'}")


def connect_healthy(status: dict[str, Any] | None) -> bool:
    """The connector and every one of its tasks are RUNNING or PAUSED (a connector with no tasks yet is not)."""
    if not status:
        return False
    ok = ("RUNNING", "PAUSED")
    tasks = status.get("tasks") or []
    return (status.get("connector") or {}).get("state") in ok and bool(tasks) and all(t.get("state") in ok for t in tasks)


def is_converged(desired_state: str, observed_state: str) -> bool:
    if desired_state == "deleted":
        return observed_state == "absent"
    if desired_state in ("deployed", "running"):
        # Strict on purpose: "degraded" and "failed" are real observations,
        # not errors, but they are NOT convergence — plan_actions keeps
        # self-healing every tick until the connector is genuinely healthy.
        return observed_state == "running"
    if desired_state == "paused":
        return observed_state == "paused"
    return False


def next_generation(current_generation: int, state_changed: bool, config_changed: bool) -> int:
    return current_generation + 1 if (state_changed or config_changed) else current_generation


def plan_actions(
    desired_state: str, connector_exists: bool, config_changed: bool, observed_state: str
) -> dict[str, bool]:
    """Pure decision: which Kafka Connect calls this tick should issue to move
    the connector from `observed_state` toward `desired_state`. There is no
    "restarting" desired_state: a user-requested restart is a one-shot action
    issued immediately by pipeline_service, and afterward the reconciler just
    treats the pipeline as desired_state="running" like any other — including
    self-healing it here if it fails again, rather than needing a persistent
    "restarting" intent that would otherwise re-issue a restart every tick
    forever."""
    if desired_state == "deleted":
        return {"delete": connector_exists}

    actions: dict[str, bool] = {"deploy": (not connector_exists) or config_changed}
    if actions["deploy"]:
        return actions  # apply the config first; state actions follow on a later tick

    if desired_state == "paused" and observed_state != "paused":
        actions["pause"] = True
    elif desired_state in ("running", "deployed") and observed_state == "paused":
        actions["resume"] = True

    if observed_state in ("failed", "degraded"):
        actions["restart"] = True  # self-heal
    return actions


async def _resolve_config(pipeline: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Raises ValueError (via build_connector) if the pipeline can't be built."""
    from app.services.catalog_service import catalog_service
    from app.services.connection_service import connection_service
    from app.services.schema_service import schema_service
    from app.services.topic_policy import topic_policy_service
    from app.services.transform_service import transform_service

    org_id = pipeline["org_id"]
    connection = await connection_service.get_connection(org_id, pipeline["connection_profile_id"])
    # The revision pinned when the pipeline was (re)targeted at this
    # template, not whatever the template currently looks like — see
    # PipelineService._build_pipeline_row. Every reconcile tick rebuilds and
    # redeploys this config, so using the live template here would mean an
    # unrelated template edit silently reshapes an already-running connector.
    template = pipeline.get("template_snapshot") or await catalog_service.get_template(
        pipeline["template_id"]
    )
    schema = (
        await schema_service.get_schema(org_id, pipeline["schema_version_id"])
        if pipeline.get("schema_version_id")
        else None
    )
    transform_policy = (
        await transform_service.get_policy(org_id, pipeline["transform_policy_id"])
        if pipeline.get("transform_policy_id")
        else None
    )
    topic_policy = (
        await topic_policy_service.get_policy(org_id, pipeline["topic_policy_id"])
        if pipeline.get("topic_policy_id")
        else None
    )
    config, _topic = await build_connector(pipeline, connection, template, schema, transform_policy, topic_policy)
    return config, config_hash_of(config)


def _set_in_memory(row: Any, **values: Any) -> None:
    """Reflect a value already written to (or deliberately never written to) the database onto the loaded row WITHOUT
    marking it dirty — the end-of-tick flush must not write it again from this tick's possibly stale snapshot."""
    for key, value in values.items():
        try:
            set_committed_value(row, key, value)
        except Exception:  # not a mapped instance (hand-rolled test double)
            setattr(row, key, value)


class RuntimeReconciler:
    def _preflight_client(self, client: Any) -> Any:
        """One plugin-list cache per Connect client for the reconciler's lifetime, so many pipelines preflighted in
        one tick share a single (large) /connector-plugins response instead of one each."""
        from app.services.deploy_preflight import CachedPluginClient

        cache = self.__dict__.setdefault("_preflight_clients", {})
        key = id(client)
        if key not in cache:
            cache[key] = CachedPluginClient(client)
        return cache[key]

    def _safe_error_text(self, pipeline_id: str, exc: Exception) -> str:
        """BUG-11: what is stored as a pipeline's runtime error (and shown in the UI) — the SafeError's fixed message,
        code and log reference, never the raw exception text (Connect's response, a driver message, ...). The detailed,
        redacted entry is logged once under that reference. A condition that persists across ticks (Connect down) keeps
        the same reference and logs once, rather than a new error line per pipeline every tick."""
        cache = self.__dict__.setdefault("_error_cache", {})
        fingerprint = (type(exc).__name__, redact_diagnostic(str(exc)))
        cached = cache.get(pipeline_id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        text = to_safe_error(
            exc, event="reconciler.runtime_error", target="kafka_connect", pipeline_id=pipeline_id,
        ).describe()
        cache[pipeline_id] = (fingerprint, text)
        return text

    def __init__(self, client_factory=None, worker_id: str | None = None):
        # Runtime design: each worker pool is its own Connect cluster (app/services/connect_runtime.py). `client_factory`,
        # when given (tests), is a no-argument factory standing in for ONE cluster that every row shares; by default a
        # client is built per cluster URL, so each row is reconciled against the cluster its pool runs on.
        self._single_client_factory = client_factory
        self._clients: dict[str, Any] = {}
        # Set by request_tick() to wake run_forever() early; created inside the running loop (None until then).
        self._wake: asyncio.Event | None = None
        # Identifies THIS process's reconciler for the claim/lease query
        # below (GAP-08) — stable for the process's whole lifetime (a fresh
        # RuntimeReconciler is only ever constructed once per process, at
        # this module's own import time). hostname+pid makes a stuck lease
        # traceable back to a real process in logs; the trailing random
        # suffix disambiguates two RuntimeReconciler instances that might
        # otherwise share both (e.g. two in the same test process, or a pid
        # reused after a restart). Overridable for tests that need to
        # simulate two distinct processes deterministically.
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"

    async def record_desired(
        self,
        org_id: str,
        pipeline_id: str,
        connector_name: str,
        desired_state: str,
        config_hash: str | None = None,
        pool_id: str | None = None,
    ) -> None:
        """Durably persist the caller's intent BEFORE any Kafka Connect call is made,
        so a crash or timeout mid-request never loses what was supposed to happen.

        GAP-08: the row is read FOR UPDATE, so two simultaneous callers queue and each derives its generation from the
        other's committed result instead of both reading N and both writing N+1 (which would let a reconcile planned
        against the first edit pass its freshness check against the second). Two simultaneous FIRST writes race on the
        unique pipeline_id instead; the loser retries once and takes the update path."""
        for attempt in (1, 2):
            try:
                await self._record_desired_once(org_id, pipeline_id, connector_name, desired_state, config_hash, pool_id)
                return
            except IntegrityError:
                if attempt == 2:
                    raise

    async def _record_desired_once(
        self,
        org_id: str,
        pipeline_id: str,
        connector_name: str,
        desired_state: str,
        config_hash: str | None,
        pool_id: str | None = None,
    ) -> None:
        async with AsyncSessionLocal() as session:
            repo = PipelineRuntimeStateRepository(session)
            row = await repo.get_by_pipeline_id(pipeline_id, for_update=True)
            if row is None:
                await repo.create(
                    {
                        "org_id": org_id,
                        "pipeline_id": pipeline_id,
                        "connector_name": connector_name,
                        "desired_state": desired_state,
                        "desired_generation": 1,
                        "config_hash": config_hash,
                        "runtime_pool_id": pool_id,
                        "observed_state": "unknown",
                        "observed_generation": 0,
                    }
                )
            else:
                state_changed = row.desired_state != desired_state
                config_changed = config_hash is not None and config_hash != row.config_hash
                values: dict[str, Any] = {
                    "desired_state": desired_state,
                    "connector_name": connector_name,
                    "desired_generation": next_generation(
                        row.desired_generation, state_changed, config_changed
                    ),
                }
                if config_hash is not None:
                    values["config_hash"] = config_hash
                if pool_id and not row.runtime_pool_id:
                    values["runtime_pool_id"] = pool_id  # a row from before pools existed learns its pool
                await repo.update(row, values)
            await session.commit()

    async def bump_generation(self, pipeline_id: str) -> None:
        """GAP-08: a user edit to a pipeline that changes what should be applied but not its desired_state
        (record_desired only bumps the generation on a state or config-hash change, and an edit goes through neither).
        Without this, a reconcile that resolved the OLD spec before the edit looks perfectly current to its freshness
        checks and applies it. Best-effort: the edit itself already succeeded, and the next tick's config-hash
        comparison still converges the connector — this only closes the window where the stale tick could act."""
        try:
            async with AsyncSessionLocal() as session:
                await PipelineRuntimeStateRepository(session).bump_generation(pipeline_id)
                await session.commit()
        except Exception:
            logger.warning("reconciler.bump_generation_failed", pipeline_id=pipeline_id, exc_info=True)

    async def get_state(self, pipeline_id: str) -> dict[str, Any] | None:
        """Best-effort: the status endpoint should degrade to "no reconciliation
        info" rather than fail the whole request if Postgres is unreachable."""
        try:
            async with AsyncSessionLocal() as session:
                row = await PipelineRuntimeStateRepository(session).get_by_pipeline_id(pipeline_id)
        except Exception as exc:
            logger.warning("reconciler.state_lookup_failed", pipeline_id=pipeline_id, error=str(exc))
            return None
        if not row:
            return None
        return {
            "desired_state": row.desired_state,
            "observed_state": row.observed_state,
            "desired_generation": row.desired_generation,
            "observed_generation": row.observed_generation,
            "config_hash": row.config_hash,
            "last_reconciled_at": row.last_reconciled_at,
            "last_runtime_error": row.last_runtime_error,
        }

    async def _enforce_error_threshold(self, row, pipeline: dict[str, Any]) -> None:
        """Counts this pipeline's connector-DLQ topic volume in its
        configured rolling window and pauses it (durably, via desired_state
        — see _reconcile_row's caller) if error_threshold_max_errors is
        exceeded. Best-effort in the strictest sense: nothing here — a
        Kafka lookup failure, an audit-write failure — may ever break
        reconciliation for this pipeline, let alone any other pipeline in
        the same tick."""
        try:
            dlq_topic = build_connector_quarantine_topic_name(
                domain=pipeline["domain"], source_system=pipeline["source_system"],
                entity=pipeline["entity"], version=pipeline["version"]
            )
            max_errors = pipeline.get("error_threshold_max_errors") or DEFAULT_ERROR_THRESHOLD_MAX_ERRORS
            window_seconds = pipeline.get("error_threshold_window_seconds") or DEFAULT_ERROR_THRESHOLD_WINDOW_SECONDS
            error_count = await count_recent_connector_errors(dlq_topic, window_seconds)
        except Exception:
            logger.exception("reconciler.error_threshold_check_failed", pipeline_id=pipeline["id"])
            return

        if error_count <= max_errors:
            return

        logger.warning(
            "reconciler.error_threshold_exceeded", pipeline_id=pipeline["id"],
            error_count=error_count, max_errors=max_errors, window_seconds=window_seconds,
        )
        # Conditional on the generation this tick read and on still holding the lease (GAP-08): an unconditional write
        # of "paused" + generation+1 from this tick's snapshot would silently erase an edit recorded in the meantime.
        try:
            async with AsyncSessionLocal() as session:
                new_generation = await PipelineRuntimeStateRepository(session).bump_desired_if_current(
                    row.id, self._worker_id, row.desired_generation, "paused"
                )
                await session.commit()
        except Exception:
            logger.exception("reconciler.threshold_pause_failed", pipeline_id=pipeline["id"])
            return
        if new_generation is None:
            logger.info("reconciler.threshold_pause_superseded", pipeline_id=pipeline["id"])
            return
        _set_in_memory(row, desired_state="paused", desired_generation=new_generation)
        try:
            await record_audit(
                org_id=row.org_id, actor="system:error-threshold", action="paused_threshold_exceeded",
                resource_type="pipeline", resource_id=pipeline["id"],
                after_state={"error_count": error_count, "max_errors": max_errors, "window_seconds": window_seconds},
            )
        except Exception:
            logger.exception("reconciler.error_threshold_audit_failed", pipeline_id=pipeline["id"])

    async def _claim_rows(self) -> list[str]:
        """GAP-08: claim (or renew this process's own already-held lease on)
        every PipelineRuntimeState row not currently leased by ANOTHER live
        process, and return just their ids. A short, separate transaction
        from the reconciliation work itself (mirrors
        operation_runner._claim_next) — the actual work below can involve
        several real HTTP calls per row and must not hold the claim query's
        row locks open for that whole duration. See
        PipelineRuntimeStateRepository.claim_batch for the actual query."""
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=settings.runtime_reconciler_lease_seconds)
        async with AsyncSessionLocal() as session:
            ids = await PipelineRuntimeStateRepository(session).claim_batch(self._worker_id, now, lease_until)
            await session.commit()
            return ids

    def _client_for_pool(self, pool_id: str | None) -> tuple[str, Any]:
        """(cluster key, client) for a pool's Connect cluster. Rows whose pools share a cluster share a key, so that
        cluster's connector list is fetched once per tick, not once per row."""
        if self._single_client_factory is not None:
            if "single" not in self._clients:
                self._clients["single"] = self._single_client_factory()
            return "single", self._clients["single"]
        url = pool_endpoint(pool_id)
        if url not in self._clients:
            self._clients[url] = KafkaConnectClient(url)
        return url, self._clients[url]

    async def reconcile_once(self) -> list[dict[str, Any]]:
        claimed_ids = await self._claim_rows()

        results: list[dict[str, Any]] = []
        # cluster key -> connector names that cluster currently has; None = unreachable this tick.
        connectors_by_cluster: dict[str, set[str] | None] = {}
        async with AsyncSessionLocal() as session:
            repo = PipelineRuntimeStateRepository(session)
            for row_id in claimed_ids:
                # GAP-08: every row was claimed at the start of the tick, but rows are worked one after another with
                # real Connect calls in between, so a row late in the batch can outlive the lease it was claimed
                # under. Renew each row's lease just before working on it, and skip it if another process has taken it
                # since — it is that process's row now.
                if not await self._renew_lease(row_id):
                    logger.info("reconciler.lease_lost", row_id=row_id)
                    continue
                row = await repo.get(row_id)
                if row is None:
                    # Deleted (e.g. by another process's tick, or this
                    # pipeline's own delete-converged cleanup) between the
                    # claim above and this fetch — nothing left to reconcile.
                    continue
                pool_id = getattr(row, "runtime_pool_id", None)
                cluster, client = self._client_for_pool(pool_id)
                if cluster not in connectors_by_cluster:
                    try:
                        connectors_by_cluster[cluster] = set(await list_connectors_retryable(client))
                    except Exception as exc:
                        # Only THIS cluster's rows wait: one pool's Connect being down must not stall the others.
                        logger.warning("reconciler.connect_unreachable", pool=pool_id, cluster=cluster, error=str(exc))
                        connectors_by_cluster[cluster] = None
                existing = connectors_by_cluster[cluster]
                if existing is None:
                    continue
                if await self._lives_on_the_shared_cluster(row, cluster, existing, connectors_by_cluster):
                    row.last_runtime_error = _ON_SHARED_CLUSTER_MESSAGE
                    row.last_reconciled_at = datetime.now(UTC)
                    await session.flush()
                    await session.commit()
                    logger.warning("reconciler.connector_on_shared_cluster", pool=pool_id, connector=row.connector_name)
                    continue
                results.append(await self._reconcile_row(session, repo, client, existing, row))
                # Commit per row, not once per tick: the row lock taken by the final fence/write is released as soon as
                # this pipeline is done, so a user's edit (record_desired) and another process's claim are never held
                # up behind the rest of the batch, and a failure on a later row can't discard earlier rows' results.
                await session.commit()
        return results

    async def _lives_on_the_shared_cluster(
        self, row: Any, cluster: str, existing: set[str], connectors_by_cluster: dict[str, set[str] | None],
    ) -> bool:
        """A pool that has been given a Connect cluster of its own AFTER connectors already ran on the shared one: the
        connector is missing from the new cluster, so the reconciler would create a SECOND copy while the original keeps
        running — two source connectors on one database (duplicate records, a contested replication slot). Refuse until the
        original has been dealt with (paused and removed — and its offsets carried over — by an operator). Only asked when the
        connector is absent from its own cluster and that cluster is not the shared one."""
        if self._single_client_factory is not None or row.connector_name in existing:
            return False
        shared_url = pool_endpoint(None)
        if cluster == shared_url or row.desired_state == "deleted":
            return False
        if shared_url not in connectors_by_cluster:
            try:
                connectors_by_cluster[shared_url] = set(
                    await list_connectors_retryable(self._client_for_pool(None)[1])
                )
            except Exception:
                connectors_by_cluster[shared_url] = None
        return row.connector_name in (connectors_by_cluster[shared_url] or set())

    async def _renew_lease(self, row_id: str) -> bool:
        lease_until = datetime.now(UTC) + timedelta(seconds=settings.runtime_reconciler_lease_seconds)
        async with AsyncSessionLocal() as session:
            renewed = await PipelineRuntimeStateRepository(session).renew_lease(row_id, self._worker_id, lease_until)
            await session.commit()
        return renewed

    async def _reconcile_row(self, session, repo, client, existing: set[str], row) -> dict[str, Any]:
        from app.services.pipeline_service import pipeline_service

        pipeline = await pipeline_service.get_pipeline(row.org_id, row.pipeline_id)
        error: str | None = None

        if pipeline is None and row.desired_state != "deleted":
            # The pipeline was removed from the store without going through
            # delete_pipeline (or delete_pipeline's own write raced this tick) —
            # treat it as an orphan and clean up the connector rather than leak it.
            # In memory only (GAP-08): persisting an absolute "deleted" from this tick's snapshot could overwrite a
            # newer recorded intent. Nothing needs it persisted — the row is removed once the delete converges, and an
            # unconverged tick simply re-derives the orphan next time.
            _set_in_memory(row, desired_state="deleted")

        connector_exists = row.connector_name in existing
        config: dict[str, Any] | None = None
        new_hash = row.config_hash
        if row.desired_state != "deleted" and pipeline is not None:
            try:
                config, new_hash = await _resolve_config(pipeline)
            except ValueError as exc:
                # Authored by the config builder ("pipeline can't be built because ..."), so the wording is kept — only
                # secrets are masked (BUG-11).
                error = redact_secrets(str(exc))

        status_resp = None
        if connector_exists:
            try:
                status_resp = await get_status_retryable(client, row.connector_name)
            except Exception as exc:
                error = error or self._safe_error_text(row.pipeline_id, exc)

        observed_state = derive_observed_state(connector_exists, status_resp)
        # FE-BUG-07: True whenever observed_state above actually came from a
        # genuine Connect response — connector_exists=False is itself a real
        # answer (the connector list call succeeded and said "not there");
        # the only case this is False is connector_exists=True but the
        # per-connector status call failed (status_resp stayed None, error
        # set above) — Connect was reachable enough to list connectors but
        # not to answer about this one. Re-derived below wherever
        # observed_state itself gets recomputed, mirroring it exactly.
        observed_from_connect = (not connector_exists) or status_resp is not None

        # GAP-04's THRESHOLD policy: Kafka Connect has no native "N errors
        # per window" concept (errors.tolerance is only ever none/all — see
        # pipeline_config.error_handling_connect_config), so this is that
        # policy's own enforcement, checked every tick for a running
        # THRESHOLD-policy pipeline before actions are planned below — a
        # breach sets desired_state to "paused" right here, in time for
        # THIS tick's plan_actions to actually pause it, the same way the
        # orphan-pipeline case above sets desired_state to "deleted" ahead
        # of its own plan_actions call.
        if (
            pipeline is not None
            and error is None
            and observed_state == "running"
            and row.desired_state not in ("paused", "deleted")
            and pipeline.get("error_handling_policy") == "THRESHOLD"
        ):
            await self._enforce_error_threshold(row, pipeline)

        config_changed = row.desired_state != "deleted" and new_hash != row.config_hash
        actions = {} if error else plan_actions(row.desired_state, connector_exists, config_changed, observed_state)

        # Re-checked (not just captured) inside deploy_if_current before every
        # attempt — a delayed retry must never apply a stale config over a
        # desired state some other request has since superseded.
        expected_generation = row.desired_generation

        async def _still_current() -> bool:
            """Both halves of "this tick may still act": nothing newer was recorded, AND this process still holds the
            row's lease (a tick that outlived its lease must not keep acting alongside the process that took over)."""
            live = await repo.fence(row.id, self._worker_id)
            return live is not None and live[0] == expected_generation and live[1]

        # GAP-08: `actions` above was planned from `row.desired_state`, read
        # at the top of this tick — possibly a while ago, since resolving
        # config and fetching Connect's status in between both take real
        # time. If some other request has since recorded a newer
        # desired_generation (a PATCH, another lifecycle call), everything
        # `actions` decided is stale: skip acting on it entirely rather than
        # execute a decision made against outdated intent. The next tick
        # (this row's lease is still held, having just been renewed by
        # _claim_rows above) reads the fresh row and acts correctly instead.
        # deploy_if_current below does its own, even finer-grained version of
        # this same check per attempt (it retries with backoff, so this
        # single upfront check alone wouldn't be enough for it) — this just
        # extends the same guarantee to delete/pause/resume/restart, which
        # don't retry and previously had no staleness check at all.
        stale = bool(actions) and not await _still_current()
        if stale:
            logger.info("reconciler.stale_generation_skipped", pipeline_id=row.pipeline_id, actions=list(actions))
            actions = {}

        deployed_this_tick = False
        try:
            if actions.get("delete"):
                await delete_converged(client, row.connector_name)
                connector_exists = False
            if actions.get("deploy") and config is not None:
                if settings.reconciler_preflight_enabled:
                    # GAP-05: the explicit Deploy action preflights, and so must this path — it is the one that
                    # applies a pipeline EDIT and re-creates a connector Connect lost. A failure becomes the
                    # pipeline's visible runtime error and nothing is sent to Connect; the next tick retries
                    # (so it self-heals once the plugin/SMT/provider is installed).
                    from app.services.catalog_service import catalog_service
                    from app.services.deploy_preflight import collect_preflight_failures

                    template = pipeline.get("template_snapshot") or await catalog_service.get_template(
                        pipeline["template_id"]
                    )
                    failures = await collect_preflight_failures(
                        template=template or {}, pipeline=pipeline, config=config, client=self._preflight_client(client),
                    )
                    if failures:
                        raise ConnectorCompatibilityError(
                            "Connector plugin compatibility preflight failed — not applying this config: "
                            + "; ".join(f["reason"] for f in failures),
                            details={"failures": failures},
                        )
                # The reconciler can be the one actually (re)creating a
                # connector too — e.g. self-healing after Connect lost it —
                # not only PipelineService.deploy(). Explicit topic
                # provisioning must cover that path as well; "topic.creation.
                # enable" in config is connect_config.py's own marker that
                # this is a source connector (see app/services/topic_policy.py).
                if "topic.creation.enable" in config:
                    from app.services.topic_policy import topic_policy_service

                    topic_policy = (
                        await topic_policy_service.get_policy(pipeline["org_id"], pipeline["topic_policy_id"])
                        if pipeline.get("topic_policy_id")
                        else None
                    )
                    # GAP-06: the same "can the cluster create this topic?" check deploy runs, whether or not explicit
                    # provisioning is on. A failure becomes the pipeline's runtime error and nothing is sent to Connect.
                    await assert_topic_policy_feasible(pipeline["org_id"], pipeline, topic_policy)
                    if settings.topic_admin_provisioning_enabled:
                        from app.services.kafka_admin import provision_topic_and_record_audit

                        await provision_topic_and_record_audit(
                            pipeline["org_id"], pipeline, pipeline["topic_name"], topic_policy
                        )
                applied = await deploy_if_current(
                    client, row.connector_name, config, still_current=_still_current
                )
                if applied is not None:
                    connector_exists = True
                    deployed_this_tick = True
                else:
                    # deploy_if_current gave up because this tick was superseded (or lost its lease) while retrying.
                    stale = True
            if actions.get("pause"):
                await pause_if_needed(client, row.connector_name)
            if actions.get("resume"):
                await resume_if_needed(client, row.connector_name)
            if actions.get("restart"):
                await restart_once(client, row.connector_name)
        except Exception as exc:
            error = self._safe_error_text(row.pipeline_id, exc)

        acted = deployed_this_tick or any(actions.get(k) for k in ("delete", "pause", "resume", "restart"))
        if error is None and acted:
            # Re-check reality after acting — never trust the action call's own
            # response as the observed truth.
            if row.desired_state == "deleted":
                observed_state = "absent"
                observed_from_connect = True  # inferred from delete_converged's own successful Connect calls above
            else:
                try:
                    status_resp = await get_status_retryable(client, row.connector_name)
                    observed_state = derive_observed_state(True, status_resp)
                    observed_from_connect = True
                except Exception as exc:
                    error = self._safe_error_text(row.pipeline_id, exc)
                    # observed_state/observed_from_connect keep whatever the
                    # pre-action check above determined — this recheck failing
                    # doesn't retroactively make that earlier observation any
                    # less real.

        # GAP-08 fence: lock the row and, in the same step, confirm this process still holds its lease and learn the
        # live generation. Every write below then happens under that lock, so a process that took the row over after
        # our lease lapsed can never be overwritten by this (older) tick's result, and an edit recorded while we
        # worked is detected rather than clobbered.
        live = await repo.fence(row.id, self._worker_id, lock=True)
        if live is None or not live[1]:
            logger.warning("reconciler.lease_lost_discarding_result", pipeline_id=row.pipeline_id)
            return {
                "pipeline_id": row.pipeline_id,
                "connector_name": row.connector_name,
                "desired_state": row.desired_state,
                "observed_state": observed_state,
                "error": error,
                "skipped": "lease_lost",
            }
        if live[0] != expected_generation:
            stale = True

        row.observed_state = observed_state
        row.last_reconciled_at = datetime.now(UTC)
        if observed_from_connect:
            row.last_observed_at = row.last_reconciled_at
        previous_error = row.last_runtime_error
        row.last_runtime_error = error
        if error is None and observed_state != "absent":
            # The self-heal restarts a failed task on every tick, and the tick after a restart sees it UNASSIGNED/RESTARTING with
            # no trace, so the last reason Connect gave is kept until the connector and every task really run (or are paused) —
            # otherwise it flickers in and out of the UI.
            kept = previous_error if (previous_error or "").startswith(CONNECT_FAILURE_PREFIX) and not connect_healthy(status_resp) else None
            row.last_runtime_error = connect_failure_reason(status_resp) or kept
        # GAP-03: the connector is fine but its quality worker has been stuck past the configured age. That is DEGRADED, with the
        # reason attached — decided HERE from the worker's own heartbeat, never through observed_state, which would make
        # plan_actions restart a healthy connector. Only overlays a connector Connect says is running and wanted running.
        stall = None
        if error is None and observed_state == "running" and row.desired_state in ("running", "deployed"):
            stall = quality_stall(row)
            if stall is not None and stall.degraded:
                row.last_runtime_error = stall.message
            else:
                stall = None
        if error is None:
            self.__dict__.setdefault("_error_cache", {}).pop(row.pipeline_id, None)
        # Recorded as the applied config only when it really was applied (or there was nothing to apply) and nothing
        # newer superseded this tick. A deploy skipped as stale/superseded must NOT record its hash: the next tick
        # would then compare against it, see "no change", and never deploy the config at all.
        config_applied = not config_changed or deployed_this_tick
        if error is None and row.desired_state != "deleted" and new_hash and config_applied and not stale:
            row.config_hash = new_hash
        # Never stamped when stale (GAP-08): row.desired_generation here is
        # the value captured at the top of this tick, and `stale` means a
        # newer one is already on record — claiming convergence with the
        # OLD generation would be a lie, even though nothing was actually
        # acted on this tick.
        if error is None and not stale and is_converged(row.desired_state, observed_state):
            row.observed_generation = row.desired_generation
        await session.flush()

        if pipeline is not None:
            if error is not None:
                # We tried to find out and couldn't get an answer at all
                # (build failure, Connect unreachable, status fetch failed).
                # Only promote to "unknown" if we've NEVER once confirmed a
                # real observation for this pipeline (observed_generation
                # still 0) — an established pipeline hitting one transient
                # error shouldn't flap its displayed status.
                if row.observed_generation == 0 and pipeline.get("status") != "unknown":
                    pipeline["status"] = "unknown"
                    await pipeline_service.set_status(row.org_id, pipeline["id"], "unknown")
            else:
                mapped_status = "degraded" if stall is not None else OBSERVED_TO_PIPELINE_STATUS.get(observed_state)
                if mapped_status and mapped_status != pipeline.get("status"):
                    pipeline["status"] = mapped_status
                    await pipeline_service.set_status(row.org_id, pipeline["id"], mapped_status)

        if row.desired_state == "deleted" and observed_state == "absent" and error is None:
            if pipeline is not None:
                # The connector is gone: release what Debezium left on the source (replication slot, publication), in the background.
                from app.services.cdc_slot_cleanup import schedule_release

                schedule_release(pipeline)
                await pipeline_service.hard_delete(row.org_id, pipeline["id"])
            await repo.delete(row)

        connector_type = None
        if pipeline is not None:
            from app.services.catalog_service import catalog_service

            template = (
                await catalog_service.get_template(pipeline["template_id"])
                if pipeline.get("template_id")
                else None
            )
            connector_type = template.get("connector_type") if template else None
        record_operation(
            runtime_pool=pipeline.get("runtime_pool_id") if pipeline else None,
            connector_type=connector_type,
            # observed_state (not row.desired_state/error) is the actual
            # outcome of this tick, and it's already a bounded enum — never
            # the connector_name/pipeline_id also in this result dict.
            status="unknown" if error else observed_state,
            operation="reconcile",
        )

        return {
            "pipeline_id": row.pipeline_id,
            "connector_name": row.connector_name,
            "desired_state": row.desired_state,
            "observed_state": observed_state,
            "error": error,
        }

    def request_tick(self, delays: tuple[float, ...] = (1.5, 4.0)) -> None:
        """Ask the reconciler loop to run a tick soon instead of waiting out the rest of its interval.

        Pause / resume / restart answer as soon as Kafka Connect accepts the request, and the pipeline then sits in a
        transitional status ("pausing" / "resuming") until a tick OBSERVES the connector in its new state — up to a whole
        interval (15s) later even though Connect itself changed state in well under a second. Nudging the loop shortly
        after the action (once Connect has had a moment to apply it, and once more in case it needed longer) makes the
        status settle in a couple of seconds. Best effort: if the loop isn't running here (tests, another process), the
        normal interval still does the job.
        """
        wake = self._wake
        if wake is None:
            return
        loop = asyncio.get_running_loop()
        for delay in delays:
            loop.call_later(delay, wake.set)

    async def run_forever(self, interval_seconds: float | None = None) -> None:
        interval = interval_seconds or settings.runtime_reconcile_interval_seconds
        logger.info("reconciler.started", interval_seconds=interval)
        self._wake = asyncio.Event()
        while True:
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("reconciler.tick_failed")
            self._wake.clear()
            # Sleep out the interval, but wake early when request_tick() asks for a tick.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=interval)


runtime_reconciler = RuntimeReconciler()
