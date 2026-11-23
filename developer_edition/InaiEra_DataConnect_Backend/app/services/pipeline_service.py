"""
Pipeline Service — Postgres-authoritative.

CRUD and lifecycle state are backed by Postgres (`dc_pipelines` via
PipelineRepository) — previously this went through the in-memory store and
never survived a restart, two API-server processes could disagree about
which pipelines existed, and a connector could keep running in Kafka Connect
long after DataConnect's own record of it vanished on restart. Every write
opens its own session and commits before returning, following the same
pattern used throughout this codebase (see app/services/connection_service.py,
migrated the same way in an earlier pass).

runtime_reconciler.py imports `pipeline_service` back — but only locally,
inside the one method that needs it (`_reconcile_row`), specifically so this
module's own top-level `from app.services.runtime_reconciler import ...`
below doesn't become a circular import. Existing tests monkeypatch
`app.services.pipeline_service.runtime_reconciler.record_desired` as a
module-level attribute, so that side of the import stays top-level.

Improvements over original (unchanged from before this migration):
- State machine validation prevents invalid transitions (e.g. deploying a failed pipeline).
- delete_pipeline guards with org_id check.
- All deploy/pause/resume/restart transitions are logged.
- status() helper is a pure function to avoid repetition.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.connect_pools import canonical_pool_id
from app.core.error_policies import DLQ_POLICIES
from app.core.errors import KafkaConnectError, PipelineVersionConflictError, ValidationError
from app.db.session import AsyncSessionLocal
from app.models.pipeline import DEAD_LETTER_TOPIC_INDEX, Pipeline
from app.models.pipeline_runtime_state import PipelineRuntimeState
from app.repositories.dataconnect import PipelineRepository, PipelineRuntimeStateRepository
from app.schemas.errors import FieldError
from app.schemas.operation_results import (
    PipelineDeployResult,
    PipelinePauseResult,
    PipelineRestartResult,
    PipelineResumeResult,
    PipelineRuntimeSummary,
    build_result,
)
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.catalog_revisions import REVISION_FIELDS
from app.services.catalog_service import catalog_service
from app.services.connect_runtime import client_for_pipeline, pool_of
from app.services.connection_service import connection_service
from app.services.lineage_service import lineage_service
from app.services.pipeline_config import generate_pipeline_config
from app.services.pipeline_spec_validator import (
    shared_dead_letter_error,
    validate_pipeline_references,
)
from app.services.pipeline_topics import data_topic, input_topics
from app.services.quality_mode import prepared_topic_for
from app.services.quality_service import quality_service
from app.services.runtime_orchestrator import RuntimeOrchestratorService
from app.services.runtime_reconciler import config_hash_of, runtime_reconciler
from app.services.safe_error import to_safe_error
from app.services.schema_service import schema_service
from app.services.secret_store import hydrate_connection_secrets, secret_store
from app.services.secrets import redact_connector_config, sanitize_probe_message
from app.services.topic_naming import (
    to_catalog_qualified_name,
)
from app.services.topic_policy import resolve_topic_config, topic_policy_service
from app.services.topic_preflight import assert_topic_policy_feasible, evaluate_topic_policy
from app.services.transform_service import transform_service
from app.services.ucs_lineage_sync import (
    emit_deploy_lineage,
    lineage_job_names,
    retire_pipeline_lineage,
)

logger = structlog.get_logger(__name__)

# FE-BUG-24: the same dialect set app/services/connection_discovery.py's
# _COL dict dispatches on — check_deploy_readiness's "source table exists"
# check needs to know up front whether a connection is even introspectable,
# without importing that module's private dict.
_INTROSPECTABLE_SOURCE_TYPES = frozenset({"postgres", "postgresql", "mysql", "sqlserver", "oracle", "mongodb"})

# Valid transitions: current_status -> set_of_allowed_target_statuses.
#
# Kafka Connect operations are not instantaneous, so this models desired vs.
# observed state explicitly rather than treating "the API call returned" as
# the final runtime state:
#   - draft/deploying/pausing/resuming/restarting/deleting are all in-flight
#     states a lifecycle request moves a pipeline INTO; they are the only
#     targets this table ever gates (deploy/pause/resume/restart/delete are
#     the only things a user can request).
#   - running/paused/degraded/failed/unknown are purely OBSERVED states —
#     the runtime reconciler assigns them directly once it confirms Kafka
#     Connect's actual state, bypassing this gate entirely (it's reporting
#     reality, not fulfilling a request). They still need to be valid
#     dictionary keys here since a pipeline can legitimately sit in any of
#     them while a user requests the next operation.
# When settings.runtime_deploy_enabled is off (no real Connect, no
# reconciler running), lifecycle methods request the final state directly
# instead of a transitional one — there's no asynchronous operation to model
# in that mode.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"deploying", "deployed"},
    "deploying": {"deleting"},
    "deployed": {"pausing", "paused", "restarting", "deleting"},
    "running": {"pausing", "paused", "restarting", "deleting"},
    "pausing": {"deleting"},
    "paused": {"resuming", "running", "restarting", "deleting"},
    "resuming": {"deleting"},
    "restarting": {"deleting"},
    "degraded": {"pausing", "paused", "restarting", "deleting"},
    "failed": {"deploying", "deployed", "restarting", "deleting"},
    "unknown": {"deploying", "deployed", "restarting", "deleting"},
    "deleting": set(),
}

# FE-BUG-25: statuses a lifecycle action sets while its Operation is
# in flight — active_operation_id is meaningful exactly while `status` is
# one of these; _persist_pipeline_fields clears it the moment `status`
# moves to anything else (a stable/observed state).
TRANSITIONAL_STATUSES = frozenset({"deploying", "pausing", "resuming", "restarting", "deleting"})

# deploy()/_lifecycle() themselves compute their transitional-vs-final
# target status the same way (settings.runtime_deploy_enabled ? transitional
# : final) — mirrored here so _submit_lifecycle_operation can claim the
# EXACT status those methods will try to set, before either one runs. See
# PipelineService.status_to_claim_for.
_ACTION_STATUS: dict[str, tuple[str, str]] = {
    "deploy": ("deploying", "deployed"),
    "pause": ("pausing", "paused"),
    "resume": ("resuming", "running"),
    "restart": ("restarting", "running"),
}


def _allowed_sources_for(target_status: str) -> set[str]:
    """Every CURRENT status VALID_TRANSITIONS says can move to
    `target_status` — the reverse of that table, computed rather than
    hand-duplicated so it can never drift from the forward direction
    _assert_transition itself enforces."""
    return {current for current, targets in VALID_TRANSITIONS.items() if target_status in targets}


# Which action a claimed status belongs to (both the transitional and the
# final one), for the 409 wording below.
_ACTION_OF_STATUS = {s: action for action, pair in _ACTION_STATUS.items() for s in pair}
_ACTION_HINT: dict[str, str] = {
    "deploy": "It is already deployed — use Resume to start it again, or Restart.",
    "pause": "Only a deployed, running or degraded pipeline can be paused.",
    "resume": "Only a paused pipeline can be resumed.",
    "restart": "A draft pipeline has to be deployed first.",
}


def _claim_conflict_detail(current_status: str | None, claim_status: str) -> str:
    """claim_lifecycle_transition's 409 text. A pipeline mid-operation (a
    transitional status) really does have one in progress; any other status
    means the action just isn't valid from where the pipeline is, and saying
    "already in progress" there sends the user looking for an operation
    that doesn't exist (e.g. Deploy on a paused pipeline)."""
    if current_status is None or current_status in TRANSITIONAL_STATUSES:
        return (
            "Cannot start a new operation for this pipeline right now — it already has one "
            f"in progress (status: '{current_status or 'unknown'}'). Wait for it to finish."
        )
    action = _ACTION_OF_STATUS.get(claim_status, claim_status)
    hint = _ACTION_HINT.get(action, "")
    return f"Cannot {action} this pipeline while it is '{current_status}'. {hint}".strip()

# What PipelineService.update_pipeline is allowed to change on an existing
# row. Deliberately excludes:
#   - identity: id, org_id, zone, domain, source_system, entity, mode,
#     version — these determine the pipeline's topic/connector identity and
#     are immutable once the pipeline is created (see PipelineUpdate).
#   - runtime state: status, generated_config, topic_name, connector_name —
#     owned exclusively by deploy/pause/resume/restart and the runtime
#     reconciler.
EDITABLE_SPEC_FIELDS = {
    "name",
    "description",
    "template_id",
    "connection_profile_id",
    "transform_policy_id",
    "schema_version_id",
    "quality_policy_id",
    "quality_mode",
    "topic_policy_id",
    "connector_config",
    "runtime_pool_id",
    "error_handling_policy",
    "error_threshold_max_errors",
    "error_threshold_window_seconds",
}


def _pipeline_to_dict(row: Pipeline, runtime_state: PipelineRuntimeState | None = None) -> dict[str, Any]:
    return {
        # FE-BUG-07: desired vs. observed, straight from the reconciler's own
        # durable state — None for a pipeline that's never been deployed/
        # paused/resumed/restarted (no PipelineRuntimeState row exists yet).
        # last_observed_at is deliberately NOT last_reconciled_at — see
        # PipelineRuntimeState's own docstring for why those two differ.
        "desired_state": runtime_state.desired_state if runtime_state else None,
        "observed_state": runtime_state.observed_state if runtime_state else None,
        "last_observed_at": runtime_state.last_observed_at if runtime_state else None,
        "runtime_error": runtime_state.last_runtime_error if runtime_state else None,
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "description": row.description,
        "template_id": row.template_id,
        "connection_profile_id": row.connection_profile_id,
        "transform_policy_id": row.transform_policy_id,
        "schema_version_id": row.schema_version_id,
        "quality_policy_id": row.quality_policy_id,
        "quality_mode": row.quality_mode,
        # NEW-01: where an ENFORCE pipeline writes its validated records (None when OBSERVE).
        "prepared_topic": prepared_topic_for({"org_id": row.org_id, "domain": row.domain, "source_system": row.source_system,
                                              "entity": row.entity, "mode": row.mode, "version": row.version,
                                              "quality_mode": row.quality_mode, "direction": row.direction}),
        "topic_policy_id": row.topic_policy_id,
        "zone": row.zone,
        "domain": row.domain,
        "source_system": row.source_system,
        "entity": row.entity,
        "mode": row.mode,
        "version": row.version,
        "topic_name": row.topic_name,
        # BUG-01: the same topic's Gravitino-safe identifier — never the
        # real Kafka topic name (that's topic_name, above) — see
        # app/services/topic_naming.py's build_catalog_qualified_name.
        # Response-only; never client-set.
        "catalog_qualified_name": to_catalog_qualified_name(row.topic_name) if row.topic_name else None,
        "connector_name": row.connector_name,
        "connector_config": row.connector_config,
        "generated_config": row.generated_config,
        "template_revision_id": row.template_revision_id,
        "template_snapshot": row.template_snapshot,
        "runtime_pool_id": row.runtime_pool_id,
        "status": row.status,
        "active_operation_id": row.active_operation_id,
        "error_handling_policy": row.error_handling_policy,
        "error_threshold_max_errors": row.error_threshold_max_errors,
        "error_threshold_window_seconds": row.error_threshold_window_seconds,
        "schema_introspection_status": row.schema_introspection_status,
        "schema_introspection_message": row.schema_introspection_message,
        "direction": row.direction,
        "record_format": row.record_format or "v1",   # None only for a row not yet inserted (tests); never upgrade one silently
        "revision": row.revision,
        "created_at": row.created_at,
    }


class PipelineService:
    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def list_pipelines(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await PipelineRepository(session).list_for_org(org_id)
            # FE-BUG-07: one batch lookup for the whole list rather than one
            # query per row — see PipelineRuntimeStateRepository.list_by_pipeline_ids.
            runtime_states = await PipelineRuntimeStateRepository(session).list_by_pipeline_ids(
                [row.id for row in rows]
            )
            return [_pipeline_to_dict(row, runtime_states.get(row.id)) for row in rows]

    async def get_pipeline(self, org_id: str, pipeline_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await PipelineRepository(session).get_for_org(org_id, pipeline_id)
            if row is None:
                return None
            runtime_state = await PipelineRuntimeStateRepository(session).get_by_pipeline_id(row.id)
            return _pipeline_to_dict(row, runtime_state)

    async def get_or_404(self, org_id: str, pipeline_id: str) -> dict:
        pipeline = await self.get_pipeline(org_id, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline '{pipeline_id}' not found.",
            )
        return pipeline

    async def create_pipeline(self, org_id: str, payload: PipelineCreate) -> dict:
        row = await self._build_pipeline_row(org_id, payload)
        try:
            async with AsyncSessionLocal() as session:
                created_row = await PipelineRepository(session).create(row)
                await session.commit()
                created = _pipeline_to_dict(created_row)
        except IntegrityError as exc:
            await self._raise_if_dead_letter_conflict(exc, org_id, row)
            raise
        logger.info("pipeline.created", org_id=org_id, pipeline_id=created["id"])
        return created

    async def _raise_if_dead_letter_conflict(
        self, exc: IntegrityError, org_id: str, fields: dict[str, Any], *, own_id: str | None = None,
    ) -> None:
        """NEW-08: another sink pipeline took this dead-letter queue between the up-front check and this write (the unique index on a sink's
        dead-letter topic refused it). Raised as the SAME validation error the up-front check gives, naming the pipeline that owns the queue; any
        other integrity error is left for the caller to re-raise. `fields` carries the identity (domain, source system, entity, version)."""
        cause = getattr(exc.orig, "__cause__", None)
        if getattr(cause, "constraint_name", None) != DEAD_LETTER_TOPIC_INDEX:
            return
        owner = None
        for other in await self.list_pipelines(org_id):
            if (
                other["id"] != own_id
                and (other.get("direction") or "").upper() == "SINK"
                and other.get("error_handling_policy") in DLQ_POLICIES
                and all(other.get(k) == fields.get(k) for k in ("domain", "source_system", "entity", "version"))
            ):
                owner = other.get("name")
                break
        logger.warning("pipeline.dead_letter_topic_taken_concurrently", org_id=org_id, owner=owner)
        raise shared_dead_letter_error(owner) from exc

    async def _expand_wildcard_tables(self, pipeline: dict, connection: dict | None, org_id: str) -> None:
        """Turn connector_config.tables == ['*'] (or a comma string containing '*') into
        the real table list by introspecting the source, so the connector gets an explicit
        include-list. No-op when tables is unset or already explicit, or the source is
        unreachable (falls back to whatever remains / the entity)."""
        cc = pipeline.get("connector_config") or {}
        raw = cc.get("tables")
        if isinstance(raw, str):
            raw = [t.strip() for t in raw.split(",") if t.strip()]
        if not raw or "*" not in raw:
            return
        names = [t for t in raw if t != "*"]
        if connection:
            from app.services.connection_discovery import discover

            # Live introspection needs the connection's real secret bundle —
            # its own connection_config only ever has a MASK placeholder
            # (see connection_service._connection_to_dict).
            result = await discover(await hydrate_connection_secrets(connection), org_id=org_id)
            for t in result.get("tables") or []:
                name = f"{t['schema']}.{t['name']}" if t.get("schema") else t["name"]
                if name not in names:
                    names.append(name)
            logger.info("pipeline.tables_expanded", pipeline_id=pipeline["id"],
                        count=len(names), reachable=result.get("connected"))
        cc["tables"] = names
        pipeline["connector_config"] = cc

    async def set_status(self, org_id: str, pipeline_id: str, status_value: str) -> dict:
        """Used by the runtime reconciler to record an OBSERVED status change
        (Kafka Connect's actual reality) — never a user-requested transition,
        which goes through _assert_transition/the lifecycle methods instead."""
        return await self._persist_pipeline_fields(org_id, pipeline_id, {"status": status_value})

    async def hard_delete(self, org_id: str, pipeline_id: str) -> None:
        """Removes the pipeline row outright — used both by delete_pipeline's
        immediate-delete path (a pipeline with no runtime state to reconcile)
        and by the runtime reconciler once it confirms the connector is
        genuinely gone from Kafka Connect. A no-op if already gone. Its jobs are retired in UCS too: a deleted pipeline otherwise kept its
        lineage there forever (UCS retires an edge only when the same job runs again without it)."""
        async with AsyncSessionLocal() as session:
            repo = PipelineRepository(session)
            row = await repo.get_for_org(org_id, pipeline_id)
            if row is None:
                return
            names = lineage_job_names({"id": row.id, "name": row.name})
            await repo.delete(row)
            await session.commit()
        await retire_pipeline_lineage(org_id, names)

    async def _persist_pipeline_fields(self, org_id: str, pipeline_id: str, fields: dict[str, Any]) -> dict:
        """Persist a partial update to an existing pipeline row and return the
        fresh dict — the one place every lifecycle/CRUD mutation below writes
        through, so there's a single query/commit pattern to get right.

        FE-BUG-25: this is also the one place every status write lands
        (deploy/_lifecycle's own final set, and the runtime reconciler's
        set_status) — so it's the single chokepoint to auto-clear
        active_operation_id the moment `status` settles onto a stable value,
        without every one of those call sites having to remember to do it
        themselves. Left untouched while `status` stays transitional (the
        operation is still the one in flight), and never overridden if a
        caller explicitly set active_operation_id itself.
        """
        if (
            "status" in fields and fields["status"] not in TRANSITIONAL_STATUSES
            and "active_operation_id" not in fields
        ):
            fields = {**fields, "active_operation_id": None}
        async with AsyncSessionLocal() as session:
            repo = PipelineRepository(session)
            row = await repo.get_for_org(org_id, pipeline_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Pipeline '{pipeline_id}' not found.",
                )
            identity = {k: getattr(row, k) for k in ("domain", "source_system", "entity", "version")}
            try:
                updated_row = await repo.update(row, fields)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                await self._raise_if_dead_letter_conflict(exc, org_id, identity, own_id=pipeline_id)
                raise
            return _pipeline_to_dict(updated_row)

    async def autocreate_schema_from_source(self, org_id: str, pipeline: dict) -> dict | None:
        """If the source is reachable, introspect the entity's columns and register a schema
        version — or link an already-registered one for this exact contract.

        dc_schema_versions uniquely constrains on (org_id, domain, source_system, entity,
        version): a pipeline that was deleted and recreated against the same contract (same
        domain/source_system/entity/version — the common case when re-testing a pipeline)
        would otherwise hit that constraint on every recreation. Before this check, that
        IntegrityError was swallowed by the best-effort try/except in the create_pipeline
        route, leaving the new pipeline's schema_version_id permanently null — and, since
        connect_config.py's incrementing-mode PK lookup depends on that link, a JDBC source
        silently fell back to `bulk` mode even when the user explicitly picked `incrementing`.

        BUG-10: every exit path below persists schema_introspection_status/_message onto
        the pipeline row — including an unexpected failure (e.g. the source connection
        timing out), which this method now catches itself rather than letting propagate to
        create_pipeline's own try/except. Pipeline creation still always succeeds either
        way; the difference is that a failure is now recorded and logged instead of
        silently discarded.
        """
        if pipeline.get("schema_version_id"):
            return None  # user already linked a schema — introspection never attempted
        # FE-BUG-23: some sources (webhook, file, REST) have no introspectable
        # structure — attempting it wastes a live probe against the source and,
        # worse, can register a bogus schema from whatever shape the probe
        # happens to see. `.get(..., True)` matches the model column's own
        # default, so a pipeline built before this field existed (template_snapshot
        # predates it) is treated the same as an explicitly-curated True.
        if not (pipeline.get("template_snapshot") or {}).get("supports_schema_discovery", True):
            await self._persist_pipeline_fields(org_id, pipeline["id"], {
                "schema_introspection_status": "skipped",
                "schema_introspection_message": "This connector template does not support schema discovery.",
            })
            return None
        try:
            existing = await schema_service.get_by_contract(
                org_id, pipeline["domain"], pipeline["source_system"], pipeline["entity"], pipeline["version"],
            )
            if existing:
                await self._persist_pipeline_fields(org_id, pipeline["id"], {
                    "schema_version_id": existing["id"],
                    "schema_introspection_status": "linked_existing", "schema_introspection_message": None,
                })
                pipeline["schema_version_id"] = existing["id"]
                logger.info("pipeline.schema_linked_existing", org_id=org_id, pipeline_id=pipeline["id"],
                            schema_id=existing["id"])
                return existing
            connection = await connection_service.get_connection(org_id, pipeline["connection_profile_id"])
            if not connection:
                message = f"Connection profile '{pipeline['connection_profile_id']}' was not found."
                logger.warning("pipeline.schema_introspection_failed", org_id=org_id, pipeline_id=pipeline["id"],
                               reason=message)
                await self._persist_pipeline_fields(org_id, pipeline["id"], {
                    "schema_introspection_status": "failed", "schema_introspection_message": message,
                })
                return None
            from app.services.connection_discovery import introspect_columns

            result = await introspect_columns(
                await hydrate_connection_secrets(connection), pipeline["entity"], org_id=org_id
            )
            if not result.get("connected") or not result.get("fields"):
                reason = sanitize_probe_message(result.get("message")) or None
                # BUG-10: an attempt that went wrong (timeout, refused connection, bad credentials, ...) is a FAILURE,
                # recorded as such; "skipped" is only for the expected cases (unsupported source, nothing to read,
                # destination blocked by policy).
                if result.get("failed"):
                    logger.warning("pipeline.schema_introspection_failed", org_id=org_id, pipeline_id=pipeline["id"],
                                   reason=reason)
                    outcome = "failed"
                else:
                    logger.info("pipeline.schema_autocreate_skipped", pipeline_id=pipeline["id"], reason=reason)
                    outcome = "skipped"
                await self._persist_pipeline_fields(org_id, pipeline["id"], {
                    "schema_introspection_status": outcome, "schema_introspection_message": reason,
                })
                return None
            schema = await schema_service.create_schema(org_id, {
                "domain": pipeline["domain"], "source_system": pipeline["source_system"],
                "entity": pipeline["entity"], "version": pipeline["version"],
                "schema_format": "json_schema", "compatibility": "BACKWARD", "status": "active",
                "schema_definition": {"fields": result["fields"], "source": "auto-introspected"},
            })
            await self._persist_pipeline_fields(org_id, pipeline["id"], {
                "schema_version_id": schema["id"],
                "schema_introspection_status": "created", "schema_introspection_message": None,
            })
            pipeline["schema_version_id"] = schema["id"]
            logger.info("pipeline.schema_autocreated", org_id=org_id, pipeline_id=pipeline["id"],
                        schema_id=schema["id"], fields=len(result["fields"]))
            return schema
        except Exception as exc:
            # BUG-11: the recorded reason is the SafeError's — fixed message, code, log reference — never the exception's
            # own text, which may carry hosts, users or SQL. The detail is in the redacted log under that reference.
            message = to_safe_error(
                exc, event="pipeline.schema_autocreate_failed", org_id=org_id, pipeline_id=pipeline["id"],
            ).describe()
            await self._persist_pipeline_fields(org_id, pipeline["id"], {
                "schema_introspection_status": "failed", "schema_introspection_message": message,
            })
            return None

    async def _find_pipeline_by_topic(self, org_id: str, topic_name: str) -> dict | None:
        """Look up the pipeline that produces a given topic — used so a sink
        pipeline's autorun can sample the upstream source instead of its own
        (destination) connection/entity, which won't exist yet at create time."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Pipeline).where(Pipeline.org_id == org_id, Pipeline.topic_name == topic_name)
            )
            row = result.scalar_one_or_none()
            return _pipeline_to_dict(row) if row else None

    async def _record_quality_skip(self, org_id: str, pipeline: dict, policy: dict, reason: str) -> dict:
        """Persist a visible 'skipped' result instead of silently returning None —
        so the Quality tab shows *why* nothing ran instead of just staying empty."""
        from app.repositories.dataconnect import QualityResultRepository
        from app.services.quality_service import _result_to_dict

        async with AsyncSessionLocal() as session:
            row = await QualityResultRepository(session).create({
                "org_id": org_id,
                "pipeline_id": pipeline["id"],
                "quality_policy_id": policy["id"],
                "topic_name": data_topic(pipeline),
                "rule_name": "*",
                "severity": "error",
                "status": "skipped",
                "failed_count": 0,
                "total_count": 0,
                "sample_failures": [{"reason": reason}],
            })
            await session.commit()
            return _result_to_dict(row)

    async def autorun_quality_from_source(self, org_id: str, pipeline: dict) -> dict | None:
        """If a quality policy is attached and the source is reachable, sample real rows
        and evaluate the policy automatically — storing a quality result (same as manual
        Execute, but triggered on pipeline create).

        For a sink pipeline, `connection_profile_id`/`entity` point at the
        *destination* (e.g. a Postgres table Kafka Connect hasn't created yet),
        not the data actually flowing through it — sampling that would either
        fail outright or check stale/empty data. When the pipeline reads from
        an existing topic (`connector_config.topics`, i.e. it's a sink), this
        instead looks up the upstream pipeline that produced that topic and
        samples its source connection/entity.

        Always returns a result — on any skip condition (no policy, unreachable
        source, no rows) it persists a `status="skipped"` QualityResult carrying
        the reason, rather than silently returning None, so the Quality tab
        reflects reality instead of staying empty with no explanation."""
        policy_id = pipeline.get("quality_policy_id")
        if not policy_id:
            return None
        from app.services.connection_discovery import sample_rows
        from app.services.kafka_runtime import get_kafka_producer
        from app.services.quality_service import quality_service

        policy = await quality_service.get_policy(org_id, policy_id)
        if not policy:
            return None

        connection_profile_id = pipeline["connection_profile_id"]
        entity = pipeline["entity"]
        producer_pipeline = pipeline
        source_topic = (pipeline.get("connector_config") or {}).get("topics")
        if source_topic:
            upstream = await self._find_pipeline_by_topic(org_id, source_topic)
            if not upstream:
                reason = f"Sink reads from topic '{source_topic}' but no pipeline producing it was found to sample"
                return await self._record_quality_skip(org_id, pipeline, policy, reason)
            connection_profile_id = upstream["connection_profile_id"]
            entity = upstream["entity"]
            producer_pipeline = upstream
        # The policy checks records as they are on the topic, i.e. AFTER the producing pipeline's transformations (renamed, masked, added or
        # converted fields). A sample read straight from the source table has not been through them, so it would fail rules that the real
        # records pass (and pass some they fail). DataConnect cannot apply Kafka Connect transformations itself, so it does not guess: the
        # quality stream worker checks the real records once the pipeline runs.
        if producer_pipeline.get("transform_policy_id"):
            reason = ("Not sampled: the pipeline transforms records before they reach the topic, and a sample from the source table has not "
                      "been through those transformations. The quality stream worker checks the real records once the pipeline runs.")
            return await self._record_quality_skip(org_id, pipeline, policy, reason)

        connection = await connection_service.get_connection(org_id, connection_profile_id)
        if not connection:
            return await self._record_quality_skip(
                org_id, pipeline, policy, "Connection profile for the sampled source was not found"
            )
        sample = await sample_rows(await hydrate_connection_secrets(connection), entity, org_id=org_id)
        records = sample.get("records") or []
        if not records:
            reason = sample.get("message") or f"No rows available to sample from '{entity}'"
            logger.info("pipeline.quality_autorun_skipped", pipeline_id=pipeline["id"], reason=reason)
            return await self._record_quality_skip(org_id, pipeline, policy, reason)
        producer = await get_kafka_producer()
        result = await quality_service.execute_policy(
            org_id=org_id, pipeline=pipeline, policy=policy, records=records, producer=producer,
            # This is a one-time sample check, not the live stream — a
            # pipeline with quality_policy_id set already gets its failures
            # quarantined continuously by QualityStreamWorker once it's
            # running (see quality_stream_worker.py); quarantining here too
            # would duplicate every failure this sample happens to catch.
            route_failures_to_quarantine=False,
        )
        logger.info("pipeline.quality_autorun", org_id=org_id, pipeline_id=pipeline["id"],
                    failed=result["failed_count"], total=result["total_count"])
        return result

    async def update_pipeline(self, org_id: str, pipeline_id: str, payload: PipelineUpdate) -> dict:
        """Apply a partial update — only the fields actually present in
        `payload` are changed. Identity fields (zone/domain/source_system/
        entity/mode/version) and runtime state (status, generated_config,
        topic_name, connector_name) are never touched here; see
        PipelineUpdate's docstring for why. This replaced a previous
        implementation that rebuilt the entire row from a full PipelineCreate
        payload and upserted it wholesale — any field the client omitted (or
        that had a schema default, like status="draft") got silently reset."""
        pipeline = await self.get_or_404(org_id, pipeline_id)
        changes = payload.model_dump(exclude_unset=True)
        # FE-BUG-14: expected_revision isn't an editable spec field itself —
        # pull it out before it's treated as one (e.g. before the `not
        # changes` no-op check below, so a request that ONLY carries a stale
        # expected_revision with no real field changes still conflicts
        # rather than silently succeeding as a no-op).
        expected_revision = changes.pop("expected_revision", None)
        if expected_revision is not None and expected_revision != pipeline["revision"]:
            raise PipelineVersionConflictError(
                "This pipeline changed since you opened it.",
                details={"current": pipeline},
            )
        # A pipeline's worker pool is fixed by its connector and cannot be edited (runtime design).
        supplied_pool = changes.pop("runtime_pool_id", None)
        if supplied_pool and canonical_pool_id(supplied_pool) != canonical_pool_id(pipeline.get("runtime_pool_id")):
            raise ValidationError(
                "A pipeline's worker pool is set by its connector and cannot be changed.",
                field_errors=[FieldError(field="runtime_pool_id", message="Fixed by the connector.")],
            )
        if not changes:
            return pipeline

        # Re-run the same construction path create_pipeline uses — reference
        # validation + config-preview generation — seeded from the existing
        # row's immutable identity fields and current runtime status, with
        # only the requested spec fields overlaid. Because the identity
        # fields never change, the regenerated topic_name/connector_name are
        # guaranteed identical to what's already stored.
        spec = {**pipeline, **changes}
        effective_payload = PipelineCreate(
            name=spec["name"],
            description=spec.get("description"),
            template_id=spec["template_id"],
            connection_profile_id=spec["connection_profile_id"],
            transform_policy_id=spec.get("transform_policy_id"),
            schema_version_id=spec.get("schema_version_id"),
            quality_policy_id=spec.get("quality_policy_id"),
            quality_mode=spec.get("quality_mode") or "OBSERVE",
            topic_policy_id=spec.get("topic_policy_id"),
            zone=spec["zone"],
            domain=spec["domain"],
            source_system=spec["source_system"],
            entity=spec["entity"],
            mode=spec["mode"],
            version=spec["version"],
            connector_config=spec.get("connector_config") or {},
            runtime_pool_id=None,  # derived afresh from the (possibly retargeted) template by validation
            status=pipeline["status"],
            error_handling_policy=spec.get("error_handling_policy"),  # None -> the connector's own default
            error_threshold_max_errors=spec.get("error_threshold_max_errors"),
            error_threshold_window_seconds=spec.get("error_threshold_window_seconds"),
        )
        row = await self._build_pipeline_row(org_id, effective_payload)

        # Retargeting to a connector that runs in a DIFFERENT pool would strand the existing connector on its old cluster:
        # allowed only while nothing has been deployed (a draft), otherwise a new pipeline is the right move.
        current_pool = canonical_pool_id(pipeline.get("runtime_pool_id"))
        if current_pool and row.get("runtime_pool_id") != current_pool and pipeline["status"] != "draft":
            raise ValidationError(
                f"That connector runs in the '{row.get('runtime_pool_id')}' worker pool, but this pipeline's connector is "
                f"deployed in '{current_pool}'. A deployed pipeline cannot move between pools — create a new pipeline.",
                field_errors=[FieldError(field="template_id", message="Runs in a different worker pool.")],
            )

        updated_fields: dict[str, Any] = {}
        for field in EDITABLE_SPEC_FIELDS:
            if field in row:
                updated_fields[field] = row[field]
        # The row is built with exclude_none, so a field the caller set to null is simply absent from it — and would be left as it was. For the quality
        # policy that meant "remove the policy" did nothing: the pipeline kept enforcing (and kept its quality processor in the lineage).
        if "quality_policy_id" in changes and changes["quality_policy_id"] is None:
            updated_fields["quality_policy_id"] = None
        # Same for the SMT chain: "use no transformations" (null) must clear the link, not leave the old chain applied.
        if "transform_policy_id" in changes and changes["transform_policy_id"] is None:
            updated_fields["transform_policy_id"] = None
        # Refresh the stored config preview (it depends on template/connection/
        # transform, all of which may have just changed) — topic_name and
        # connector_name come along too, but are provably unchanged since
        # their only inputs (the identity fields above) are immutable here.
        updated_fields["topic_name"] = row["topic_name"]
        updated_fields["connector_name"] = row["connector_name"]
        updated_fields["generated_config"] = row["generated_config"]
        # FE-BUG-14: bump the optimistic-concurrency counter on every
        # applied edit — anyone still holding the pre-edit revision who
        # tries to PATCH next will conflict against this new value.
        updated_fields["revision"] = pipeline["revision"] + 1
        # GAP-12: not client-editable (not in EDITABLE_SPEC_FIELDS), but
        # re-derived every time since retargeting template_id can change it
        # — same reasoning as topic_name/connector_name above.
        updated_fields["direction"] = row["direction"]
        if "template_id" in changes:
            # Re-pin ONLY when the template reference itself changed — an
            # unrelated edit (description, connector_config, ...) must not
            # silently advance which template revision this pipeline is
            # pinned to. See _build_pipeline_row.
            updated_fields["template_revision_id"] = row["template_revision_id"]
            updated_fields["template_snapshot"] = row["template_snapshot"]

        updated = await self._persist_pipeline_fields(org_id, pipeline_id, updated_fields)
        if updated.get("status") != "draft":
            updated = await self._store_deployed_config(org_id, pipeline_id, updated)
        if settings.runtime_deploy_enabled:
            # GAP-08: an edit is a new desired spec. Without a new generation, a reconcile that resolved the previous
            # spec before this write would still look current to its freshness checks and apply it over the edit.
            await runtime_reconciler.bump_generation(pipeline_id)
        logger.info(
            "pipeline.updated", org_id=org_id, pipeline_id=pipeline_id, fields=sorted(changes)
        )
        # The lineage graph shows the quality processor, quarantine and prepared topics only when the pipeline has/ENFORCEs a policy. For a pipeline
        # that is already deployed, redraw it when that changes — lineage is otherwise only built on deploy and would show the old flow.
        if updated.get("status") != "draft" and {"quality_policy_id", "quality_mode"} & changes.keys():
            try:
                connection = await connection_service.get_connection(org_id, updated["connection_profile_id"])
                await lineage_service.materialize_pipeline_lineage(updated, connection)
            except Exception:
                logger.exception("pipeline.lineage_refresh_failed", org_id=org_id, pipeline_id=pipeline_id)
        # A deployed pipeline that changed its quality settings, the topics it reads, or its NAME redraws its lineage in UCS. A new input
        # needs nothing else: the same job reporting another input for the same output retires the old one in UCS. The name is the job's
        # name there, so on a rename the old one is retired first, or UCS keeps showing the pipeline under both names.
        renamed = "name" in changes and changes["name"] != pipeline["name"]
        inputs_changed = input_topics(updated) != input_topics(pipeline)
        if updated.get("status") != "draft" and (renamed or inputs_changed or {"quality_policy_id", "quality_mode"} & changes.keys()):
            if renamed:
                await retire_pipeline_lineage(org_id, [pipeline["name"], f"{pipeline['name']}:quality"])
            try:
                await emit_deploy_lineage(updated)
            except Exception:
                logger.exception("pipeline.ucs_lineage_refresh_failed", org_id=org_id, pipeline_id=pipeline_id)
        return updated

    async def _store_deployed_config(self, org_id: str, pipeline_id: str, pipeline: dict) -> dict:
        """The stored generated_config of a DEPLOYED pipeline is what it actually runs, not the creation-time preview an edit rebuilds (that
        preview has none of the deploy-time parts: the primary-key key transform, the converters, the routing to its topic). Sinks read a
        producer's stored config to decide how to read its topic (upsert by key, plain JSON or not — connect_config.build_connector), so an
        edit of a running source (e.g. OBSERVE -> ENFORCE) used to turn its sinks' upsert into plain inserts. Built exactly as the reconciler
        builds what it deploys, then redacted like a deploy's. If it cannot be built, the preview stays and the reconciler reports why."""
        from app.services.runtime_reconciler import _resolve_config

        try:
            config, _hash = await _resolve_config(pipeline)
        except Exception:  # noqa: BLE001 — never fail the edit over the stored copy
            logger.warning("pipeline.deployed_config_not_built", org_id=org_id, pipeline_id=pipeline_id, exc_info=True)
            return pipeline
        return await self._persist_pipeline_fields(org_id, pipeline_id, {
            "generated_config": redact_connector_config({"name": pipeline["connector_name"], **config}),
        })

    async def delete_pipeline(self, org_id: str, pipeline_id: str) -> None:
        """Deletion policy for the pipeline <-> Kafka Connect connector pair
        (explicit by design, not incidental — see the runtime reconciler
        ticket this closed):

            SOFT-DELETE THE PIPELINE, THEN RECONCILE THE CONNECTOR AWAY.

        A pipeline that has (or may have) a live connector is never deleted
        on the spot. Its desired_state is durably recorded as "deleted"
        (transactional at the desired-state level: this write happens BEFORE
        anything else, so the intent survives a crash or timeout right after
        this call — same guarantee every other lifecycle operation gets),
        the row is marked status="deleting", and the runtime reconciler
        removes the real connector from Kafka Connect (idempotently — see
        delete_converged in kafka_connect_retry.py, which treats a 404 as
        "already gone" rather than an error) before removing this row.

        The alternatives were considered and rejected:
          - retain-but-orphan: leaves a connector nothing in DataConnect
            still tracks, silently consuming source/sink capacity forever.
          - block-delete-while-deployed: forces every delete through an
            extra pause/undeploy step even when the user just wants it gone.
          - hard delete + best-effort connector cleanup: exactly the bug
            this policy replaced — the row could vanish from the app while
            Kafka Connect kept running the connector, and a failed/slow
            cleanup call had nowhere durable to retry from.

        A pipeline that was never deployed (status="draft") has no runtime
        state to reconcile, so it's the one case that IS deleted immediately
        — there's nothing for the soft-delete step to protect against.
        """
        pipeline = await self.get_or_404(org_id, pipeline_id)

        if not settings.runtime_deploy_enabled or pipeline["status"] == "draft":
            # Nothing was ever (or will be) deployed to Kafka Connect for this
            # pipeline, so there's no runtime state to wait on — delete is
            # immediate, same as before.
            if settings.runtime_deploy_enabled:
                await runtime_reconciler.record_desired(
                    org_id, pipeline_id, pipeline["connector_name"], "deleted", pool_id=pool_of(pipeline)
                )
            await self.hard_delete(org_id, pipeline_id)
            logger.info("pipeline.deleted", org_id=org_id, pipeline_id=pipeline_id)
            return

        # A real connector may exist in Kafka Connect — deletion is
        # asynchronous like every other lifecycle operation: record the
        # intent, mark the pipeline "deleting", and let the reconciler
        # actually remove the connector (and, once confirmed gone, this row)
        # on its next tick. This also means the intent survives a crash or
        # timeout right after this call, same as deploy/pause/resume/restart.
        self._assert_transition(pipeline, "deleting")
        await runtime_reconciler.record_desired(
            org_id, pipeline_id, pipeline["connector_name"], "deleted", pool_id=pool_of(pipeline)
        )
        await self._persist_pipeline_fields(org_id, pipeline_id, {"status": "deleting"})
        logger.info("pipeline.delete_requested", org_id=org_id, pipeline_id=pipeline_id)

    # ── Lifecycle operations ──────────────────────────────────────────────────

    def status_to_claim_for(self, action: str) -> str:
        """FE-BUG-25: the exact status deploy()/pause()/resume()/restart()
        will themselves try to set — transitional while a reconciler will
        confirm the real outcome, the final status directly when there's no
        async operation to model (settings.runtime_deploy_enabled off),
        mirroring those methods' own branch exactly so the submission-time
        claim below (which must run BEFORE either method does) locks in the
        same value they'll each redundantly re-assert."""
        transitional, target = _ACTION_STATUS[action]
        return transitional if settings.runtime_deploy_enabled else target

    async def claim_lifecycle_transition(
        self, org_id: str, pipeline_id: str, claim_status: str, operation_id: str,
    ) -> dict:
        """FE-BUG-25: the actual idempotency guard — atomically sets
        pipeline.status = claim_status and active_operation_id = operation_id
        in one conditional UPDATE, gated by the exact set of current
        statuses VALID_TRANSITIONS allows moving to claim_status FROM (the
        same table _assert_transition itself reads, so this can never
        silently drift out of sync with what deploy()/_lifecycle() would
        separately allow).

        This is a compare-and-swap, not a read-then-write: Postgres
        serializes two concurrent UPDATEs to the same row, so if a double
        -click (or two browser tabs) submit the same action within
        milliseconds of each other, the second UPDATE is evaluated against
        whatever the FIRST one already committed — which is no longer in
        the allowed source set — and affects zero rows. Called from
        _submit_lifecycle_operation BEFORE any Operation row is created, so
        a rejected claim never leaves an orphaned queued Operation behind.

        Raises 409 if zero rows matched (pipeline already has an operation
        in flight, or this specific transition isn't valid from its current
        status) — the caller is expected to have already 404'd a missing
        pipeline via get_or_404, so that case isn't re-checked here.
        """
        allowed_sources = _allowed_sources_for(claim_status)
        async with AsyncSessionLocal() as session:
            repo = PipelineRepository(session)
            row = await repo.claim_transition(org_id, pipeline_id, allowed_sources, claim_status, operation_id)
            if row is not None:
                await session.commit()
        if row is None:
            current = await self.get_pipeline(org_id, pipeline_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_claim_conflict_detail((current or {}).get("status"), claim_status),
            )
        return _pipeline_to_dict(row)

    async def release_claim_on_failure(self, org_id: str, pipeline_id: str) -> None:
        """FE-BUG-25 follow-up — see app/api/routes/pipelines.py's
        _lifecycle_work, the only caller. claim_lifecycle_transition moves a
        pipeline onto a transitional status (and sets active_operation_id)
        BEFORE the actual deploy()/pause()/resume()/restart() call runs, so
        a duplicate submission has nothing left to claim. If that call then
        raises — a live preflight rejection, a Kafka Connect error, anything
        — nothing else was ever going to move status off that transitional
        value: _persist_pipeline_fields only clears active_operation_id when
        a NEW status gets written, and nothing writes one on a failure path
        that never reaches it. Left alone, the pipeline is stuck forever
        (TRANSITIONAL_STATUSES's own claim guard permits only "deleting"
        from most of them) and even a legitimate retry is rejected as "an
        operation already in progress" — the operation that supposedly owns
        that claim already finished (failed), it just never let go of it.

        "failed" is used unconditionally rather than trying to restore
        whatever status preceded the claim: an Operation's work must be
        reconstructible from just (org_id, pipeline_id) so a different
        worker/process can retry it later (see operation_runner.py's own
        docstring) — there is no submission-time state available here to
        restore, only what's in the row right now. "failed" is always a
        valid, retryable landing spot (VALID_TRANSITIONS allows deploying/
        deployed/restarting/deleting from it) regardless of which of the
        four actions actually failed.

        A no-op if the pipeline has already moved off a transitional status
        by the time this runs (e.g. a stale retry of an operation a newer
        attempt already superseded) — never overwrites a status a
        legitimate, more-recent write already settled on."""
        async with AsyncSessionLocal() as session:
            row = await PipelineRepository(session).get_for_org(org_id, pipeline_id)
            if row is None or row.status not in TRANSITIONAL_STATUSES:
                return
        await self._persist_pipeline_fields(org_id, pipeline_id, {"status": "failed"})
        logger.info("pipeline.claim_released_after_failure", org_id=org_id, pipeline_id=pipeline_id)

    async def _refresh_stale_connection(self, org_id: str, connection: dict) -> dict:
        """Returns the connection as it stands after a fresh live test when its stored
        verification is not "ok" or is older than the configured max age; otherwise as given.
        Never raises — a failed refresh just leaves the stored result to be judged."""
        tested_at = connection.get("last_tested_at")
        max_age = timedelta(seconds=settings.deploy_readiness_connection_max_age_seconds)
        too_old = isinstance(tested_at, datetime) and datetime.now(UTC) - tested_at > max_age
        if connection.get("verification_status") == "ok" and not too_old:
            return connection
        try:
            await connection_service.probe_and_record(org_id, connection["id"])
            return await connection_service.get_connection(org_id, connection["id"]) or connection
        except Exception:  # noqa: BLE001 - a broken refresh must not hide every other check
            logger.warning("deploy_readiness.connection_refresh_failed", connection_id=connection.get("id"), exc_info=True)
            return connection

    async def assert_ready_to_deploy(self, org_id: str, pipeline_id: str) -> None:
        """Raises ValidationError (422) listing every failed readiness check, so a deploy that the
        "Ready to deploy" screen would have blocked is blocked server-side too. No-op when
        settings.deploy_requires_readiness is off."""
        if not settings.deploy_requires_readiness:
            return
        readiness = await self.check_deploy_readiness(org_id, pipeline_id)
        if readiness["ready"]:
            return
        failed = [c for c in readiness["checks"] if not c["ok"]]
        summary = "; ".join(f"{c['label']}: {c['reason'] or 'failed'}" for c in failed)
        raise ValidationError(
            f"Pipeline is not ready to deploy — {summary}",
            field_errors=[FieldError(field=c["key"], message=c["reason"] or f"{c['label']} failed") for c in failed],
        )

    async def check_deploy_readiness(self, org_id: str, pipeline_id: str) -> dict:
        """FE-BUG-24: the same setup deploy() below does — resolve every
        reference, expand wildcard tables, build the connector config, run
        preflight — but purely to REPORT readiness, never to act on it: no
        topic provisioning, no desired-state record, no call to Kafka
        Connect's deploy endpoint. Meant to back a "Ready to deploy" review
        screen the UI shows before Deploy is ever clicked, so a user sees
        every reason a deploy would fail up front, not one at a time across
        repeated failed attempts.

        Every check appends a {key, label, ok, reason} entry rather than
        raising — an unexpected exception from any one check is caught and
        turned into a failed check too, so one broken check (e.g. a
        connection whose live probe times out) doesn't hide every other
        check's result behind a 500.
        """
        pipeline = await self.get_or_404(org_id, pipeline_id)
        checks: list[dict[str, Any]] = []

        def add(key: str, label: str, ok: bool, reason: str | None = None) -> None:
            checks.append({"key": key, "label": label, "ok": ok, "reason": reason if not ok else None})

        connection = await connection_service.get_connection(org_id, pipeline["connection_profile_id"])
        template = pipeline.get("template_snapshot") or await catalog_service.get_template(pipeline["template_id"])
        schema = (
            await schema_service.get_schema(org_id, pipeline["schema_version_id"])
            if pipeline.get("schema_version_id") else None
        )
        transform_policy = (
            await transform_service.get_policy(org_id, pipeline["transform_policy_id"])
            if pipeline.get("transform_policy_id") else None
        )
        quality_policy = (
            await quality_service.get_policy(org_id, pipeline["quality_policy_id"])
            if pipeline.get("quality_policy_id") else None
        )
        topic_policy = (
            await topic_policy_service.get_policy(org_id, pipeline["topic_policy_id"])
            if pipeline.get("topic_policy_id") else None
        )

        # 1. Connection verified — judged on the stored result of "Test connection"
        # (connection_service.py), re-tested live first when that result is missing,
        # failed or older than settings.deploy_readiness_connection_max_age_seconds, so
        # fixing a connection and reopening this screen is enough — no separate trip to
        # the Connections page — and a long-stale pass isn't trusted either.
        if connection is not None:
            connection = await self._refresh_stale_connection(org_id, connection)
        if connection is None:
            add("connection_verified", "Connection verified", False, "Connection profile not found.")
        else:
            vs = connection.get("verification_status")
            if connection.get("verification_stale"):
                reason = connection.get("last_test_message")  # "settings changed after it was last tested…"
            elif not connection.get("last_tested_at"):
                reason = "Connection has never been tested — use \"Test connection\" first."
            else:
                detail = f": {connection['last_test_message']}" if connection.get("last_test_message") else ""
                reason = f"The last connection test failed (status: {vs}){detail} — fix it, then use \"Test connection\"."
            add("connection_verified", "Connection verified", vs == "ok", reason)

        # 2 & 10. Connector plugin installed, and everything else
        # deploy_preflight.py already checks about the live Kafka Connect
        # worker (SMTs/converters/version/mode) — reusing its exact logic,
        # non-raising, rather than re-deriving it. Needs a real connector
        # config first, which also doubles as the "can this pipeline's spec
        # even be built" check every other item downstream assumes passed.
        config: dict[str, Any] | None = None
        topic: str | None = None
        build_error: str | None = None
        if connection is not None and template is not None:
            try:
                from app.services.connect_config import build_connector

                await self._expand_wildcard_tables(pipeline, connection, org_id)
                config, topic = await build_connector(pipeline, connection, template, schema, transform_policy, topic_policy)
            except ValueError as exc:
                build_error = str(exc)
        else:
            build_error = "Connection or connector template could not be resolved."

        # Both checks are always added, on every path below, so the response's
        # set of check keys never varies by environment (runtime_deploy_enabled
        # on/off) or outcome — a caller can always render the same fixed list.
        compat_label = "Connector compatibility (SMTs, converters, plugin version, mode)"
        if build_error:
            # The plugin question doesn't need the config: answer it on its own, so a pipeline whose config can't be built (a CDC topic
            # into a JDBC sink, say) isn't also reported as "plugin not installed" when the plugin is right there.
            reason = f"Could not build this pipeline's connector configuration: {build_error}"
            plugin_ok, plugin_reason = True, None
            if template is not None and settings.runtime_deploy_enabled:
                from app.services.deploy_preflight import connector_plugin_finding

                try:
                    finding = connector_plugin_finding(template, await client_for_pipeline(pipeline).list_connector_plugins())
                    plugin_ok, plugin_reason = finding is None, (finding[1] if finding else None)
                except KafkaConnectError as exc:
                    plugin_ok, plugin_reason = False, "Could not reach the Kafka Connect worker: " + to_safe_error(
                        exc, event="readiness.connect_unreachable", pipeline_id=pipeline_id,
                    ).describe()
            add("connector_plugin_installed", "Connector plugin installed", plugin_ok, plugin_reason)
            add("connector_compatibility", compat_label, False, reason)
        elif not settings.runtime_deploy_enabled:
            # Mirrors deploy()'s own gate exactly — it never touches Kafka
            # Connect at all in this mode, so there is no live worker to
            # check a plugin against.
            add("connector_plugin_installed", "Connector plugin installed", True, None)
            add("connector_compatibility", compat_label, True, None)
        else:
            try:
                from app.services.deploy_preflight import collect_preflight_failures

                client = client_for_pipeline(pipeline)
                failures = await collect_preflight_failures(template=template, pipeline=pipeline, config=config, client=client)
            except KafkaConnectError as exc:
                reason = "Could not reach the Kafka Connect worker: " + to_safe_error(
                    exc, event="readiness.connect_unreachable", pipeline_id=pipeline_id,
                ).describe()
                add("connector_plugin_installed", "Connector plugin installed", False, reason)
                add("connector_compatibility", compat_label, False, reason)
            else:
                by_check = {f["check"]: f["reason"] for f in failures}
                add("connector_plugin_installed", "Connector plugin installed",
                    "connector_class_installed" not in by_check, by_check.get("connector_class_installed"))
                other_reasons = [
                    reason for check, reason in by_check.items() if check != "connector_class_installed"
                ]
                add("connector_compatibility", compat_label, not other_reasons, "; ".join(other_reasons) if other_reasons else None)

        # 3. Source table exists — only meaningful for a source pipeline
        # whose connection type this app can actually introspect (see
        # FE-BUG-23's supports_schema_discovery, the same flag the backend's
        # own auto-schema-discovery is gated by); anything else reports as
        # trivially satisfied rather than a check that can never pass.
        direction = pipeline.get("direction") or (template.get("connector_type", "").upper() if template else None)
        introspectable = bool(template and template.get("supports_schema_discovery", True))
        # Same dialect set connection_discovery.py's own _COL dispatches on
        # — checked here too (not just via the template flag above) so a
        # connection whose source_type isn't actually one of these reports
        # as "not applicable" rather than a failed live probe attempt.
        source_type = (connection.get("source_type") or "").lower() if connection else ""
        if (connection is not None and direction == "SOURCE" and introspectable
                and source_type in _INTROSPECTABLE_SOURCE_TYPES and pipeline.get("entity")):
            try:
                from app.services.connection_discovery import introspect_columns

                result = await introspect_columns(await hydrate_connection_secrets(connection), pipeline["entity"], org_id=org_id)
                add("source_table_exists", "Source table exists", bool(result.get("connected") and result.get("fields")),
                    result.get("message") or f"Could not confirm '{pipeline['entity']}' exists on the source.")
            except Exception as exc:  # a live probe against an external system — never let it 500 this check
                add("source_table_exists", "Source table exists", False,
                    "Could not probe the source: " + to_safe_error(
                        exc, event="readiness.source_probe_failed", pipeline_id=pipeline_id,
                    ).describe())
        else:
            add("source_table_exists", "Source table exists", True, None)

        # 4. Topic policy valid — resolves the same way build_connector's own
        # topic-creation config does; a structurally invalid resolved policy
        # (non-positive partitions/replication/min-ISR) is caught here
        # rather than only surfacing as a rejected Kafka Admin call at
        # deploy time.
        try:
            resolved = await resolve_topic_config(org_id, pipeline["domain"], topic_policy)
            invalid = [
                field for field in ("partitions", "replication_factor", "min_insync_replicas")
                if not isinstance(resolved.get(field), int) or resolved.get(field) <= 0
            ]
            add("topic_policy_valid", "Topic policy valid", not invalid,
                f"Resolved topic policy has invalid values for: {', '.join(invalid)}." if invalid else None)
        except Exception as exc:
            add("topic_policy_valid", "Topic policy valid", False, f"Could not resolve topic policy: {exc}")

        # 4b. GAP-06: can the Kafka cluster actually create this pipeline's topic with that policy? "Broker count 2 / requested
        # replication 3" is caught here, as a failed check, instead of at deploy or after it. Sources only (a sink creates no
        # topic); a topic that already exists, or a cluster that could not be asked, does not fail it.
        try:
            preflight = await evaluate_topic_policy(org_id, pipeline, topic_policy)
        except Exception:
            logger.warning("readiness.topic_feasibility_failed", pipeline_id=pipeline_id, exc_info=True)
            preflight = None
        if preflight is not None and preflight.applicable:
            add("kafka_brokers_sufficient", "Kafka brokers sufficient for the topic policy", preflight.ok,
                None if preflight.ok else (
                    f"Kafka has {preflight.brokers if preflight.brokers is not None else 'an unknown number of'} broker(s); "
                    f"this topic policy asks for replication factor {preflight.resolved.get('replication_factor')} and "
                    f"min.insync.replicas {preflight.resolved.get('min_insync_replicas')}. "
                    + " ".join(p.message for p in preflight.problems)
                ))
        else:
            add("kafka_brokers_sufficient", "Kafka brokers sufficient for the topic policy", True, None)

        # 5. Topic name available — no OTHER pipeline in this org already
        # owns this exact topic name. (Kafka-side existence is checked at
        # actual provisioning time — kafka_admin.py's ensure_topic() — which
        # is idempotent create-or-verify against a topic this same pipeline
        # produces, not a collision this check is about.)
        topic_name = pipeline.get("topic_name")
        if topic_name:
            siblings = await self.list_pipelines(org_id)
            collision = next((p for p in siblings if p["id"] != pipeline_id and p.get("topic_name") == topic_name), None)
            add("topic_name_available", "Topic name available", collision is None,
                f"Topic '{topic_name}' is already used by pipeline \"{collision['name']}\"." if collision else None)
        else:
            add("topic_name_available", "Topic name available", True, None)

        # 6. Schema valid
        if pipeline.get("schema_version_id"):
            if schema is None:
                add("schema_valid", "Schema valid", False, "Linked schema version could not be found.")
            else:
                result = schema_service.validate_compatibility(schema)
                add("schema_valid", "Schema valid", bool(result.get("valid")), "; ".join(result.get("errors") or []) or None)
        else:
            add("schema_valid", "Schema valid", True, None)

        # 7. Transform configuration valid — re-validates the linked
        # policy's own SMT steps (transform_policy.py's validate_transform,
        # the same rule POST /transform-policies/{id}/validate uses), not
        # just that the policy id resolves.
        if pipeline.get("transform_policy_id"):
            if transform_policy is None:
                add("transform_configuration_valid", "Transform configuration valid", False,
                    "Linked transform policy could not be found.")
            else:
                from app.services.transform_policy import (
                    source_transform_problems,
                    validate_transform,
                )

                errors: list[str] = []
                for step in transform_policy.get("transforms") or []:
                    try:
                        validate_transform(step)
                    except ValueError as exc:
                        errors.append(str(exc))
                if direction == "SOURCE":
                    errors.extend(source_transform_problems(transform_policy.get("transforms") or []))
                add("transform_configuration_valid", "Transform configuration valid", not errors, "; ".join(errors) or None)
        else:
            add("transform_configuration_valid", "Transform configuration valid", True, None)

        # 8. Quality policy valid — same idea, reusing
        # quality_service.validate_quality_policy (also backs
        # POST /quality-policies/{id}/validate).
        if pipeline.get("quality_policy_id"):
            if quality_policy is None:
                add("quality_policy_valid", "Quality check valid", False, "Linked quality check could not be found.")
            else:
                from app.services.quality import validate_quality_policy

                result = validate_quality_policy(quality_policy)
                add("quality_policy_valid", "Quality check valid", bool(result.get("valid")), "; ".join(result.get("errors") or []) or None)
        else:
            add("quality_policy_valid", "Quality check valid", True, None)

        # 9. Secret reference resolved — connection.secret_ref (if any)
        # actually resolves in the secret store, checked directly rather
        # than via hydrate_connection_secrets (which silently no-ops on an
        # unresolvable ref instead of signaling failure — see its own
        # docstring in secret_store.py).
        secret_ref = connection.get("secret_ref") if connection else None
        if secret_ref:
            try:
                resolved_secret = await secret_store.get(secret_ref)
                add("secret_reference_resolved", "Secret reference resolved", bool(resolved_secret),
                    f"Secret reference '{secret_ref}' did not resolve to a stored secret." if not resolved_secret else None)
            except Exception as exc:
                add("secret_reference_resolved", "Secret reference resolved", False,
                    "Could not resolve secret reference: " + to_safe_error(
                        exc, event="readiness.secret_resolution_failed", pipeline_id=pipeline_id,
                    ).describe())
        else:
            add("secret_reference_resolved", "Secret reference resolved", True, None)

        return {
            "pipeline_id": pipeline_id,
            "topic_name": topic_name,
            "ready": all(c["ok"] for c in checks),
            "checks": checks,
        }

    async def deploy(self, org_id: str, pipeline_id: str) -> dict:
        pipeline = await self.get_or_404(org_id, pipeline_id)
        # "deploying" when a reconciler will actually confirm the outcome;
        # otherwise there's no async operation to model, so go straight to
        # the final state as before.
        target_status = "deploying" if settings.runtime_deploy_enabled else "deployed"
        self._assert_transition(pipeline, target_status, allow_self=True)
        runtime: dict[str, Any] = {}
        if settings.runtime_deploy_enabled:
            from app.services.connect_config import build_connector

            connection = await connection_service.get_connection(org_id, pipeline["connection_profile_id"])
            # The revision pinned at creation/last-retarget time, not
            # whatever the template currently looks like — see
            # _build_pipeline_row. Falls back to the live template only for
            # a pipeline created before revisioning existed.
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
            await self._expand_wildcard_tables(pipeline, connection, org_id)
            config, topic = await build_connector(pipeline, connection, template, schema, transform_policy, topic_policy)  # raises ValueError → 422
            # GAP-05: verify the connector class/SMTs/converters/ConfigProvider
            # refs this exact config needs, and the pipeline's mode, are all
            # actually supported by the live Kafka Connect worker BEFORE
            # either provisioning a topic or calling deploy_connector below —
            # raises ConnectorCompatibilityError (422) rather than letting an
            # incompatible connector reach Kafka Connect at all. One client,
            # reused for the deploy_connector call further down too.
            from app.services.deploy_preflight import run_deploy_preflight

            client = client_for_pipeline(pipeline)
            await run_deploy_preflight(template=template, pipeline=pipeline, config=config, client=client)
            # Explicitly create/validate the topic via the Kafka Admin API and
            # record the resolved TopicPolicy — BEFORE the connector is ever
            # deployed, never relying on the broker's implicit auto-create (see
            # app/services/kafka_admin.py). `topic` is only set for sources —
            # a sink reads a topic that must already exist, it never creates one.
            if topic:
                # GAP-06: can the cluster actually create this topic with the resolved policy? Asked whether or not explicit
                # provisioning is on — with it off, Kafka Connect creates the topic itself from the same settings and fails
                # AFTER the connector is deployed. Raises TopicReplicationFactorError (422); an existing topic is skipped.
                await assert_topic_policy_feasible(org_id, pipeline, topic_policy)
            if topic and settings.topic_admin_provisioning_enabled:
                from app.services.kafka_admin import provision_topic_and_record_audit

                await provision_topic_and_record_audit(org_id, pipeline, topic, topic_policy)
            if prepared := prepared_topic_for(pipeline):
                # NEW-01: an ENFORCE pipeline's validated records go to its prepared topic, and a sink reads it. Created here, before the
                # connector is deployed, so a sink never subscribes to a topic that does not exist yet (the worker would only create it
                # on the first passing record).
                from app.services.kafka_admin import ensure_simple_topic

                await ensure_simple_topic(prepared, replication_factor=settings.topic_default_replication_factor)
            # `config` holds real, resolved credentials — it must only ever be used
            # transiently, to build the Connect deploy call below. What gets
            # persisted/returned (pipeline["generated_config"]) is the redacted copy.
            pipeline["generated_config"] = redact_connector_config(
                {"name": pipeline["connector_name"], **config}
            )
            # Durably record the desired spec BEFORE calling Connect: if this
            # process dies right after Connect accepts the request, or the
            # response times out, the reconciler still knows what should exist
            # and converges it on its next tick regardless of what happened here.
            await runtime_reconciler.record_desired(
                org_id, pipeline_id, pipeline["connector_name"], "deployed", config_hash_of(config),
                pool_id=pool_of(pipeline),
            )
            # deploy_connector already raises (KafkaConnectError) if Connect
            # rejected the request — its response body itself is never
            # needed beyond that (GAP-11: it used to be embedded whole into
            # this operation's persisted result as `runtime_response`, an
            # unbounded echo of the full connector config that nothing
            # internal or in the frontend ever read).
            await client.deploy_connector(pipeline["connector_name"], config)
            runtime = {"connector": pipeline["connector_name"], "topic": topic, "state": "deployed"}
            logger.info("pipeline.connector_deployed", pipeline_id=pipeline_id,
                        connector=pipeline["connector_name"], topic=topic)
        deployed = await self._persist_pipeline_fields(
            org_id, pipeline_id,
            {"status": target_status, "generated_config": pipeline["generated_config"]}
            if settings.runtime_deploy_enabled else {"status": target_status},
        )
        connection = await connection_service.get_connection(org_id, pipeline["connection_profile_id"])
        await lineage_service.materialize_pipeline_lineage(deployed, connection)
        await emit_deploy_lineage(deployed)
        logger.info("pipeline.deployed", org_id=org_id, pipeline_id=pipeline_id, status=target_status)
        status = self.build_status(deployed)
        runtime_summary = (
            PipelineRuntimeSummary(state=runtime["state"], connector=runtime.get("connector"), topic=runtime.get("topic"))
            if runtime else PipelineRuntimeSummary(state=target_status)
        )
        return build_result(
            PipelineDeployResult,
            pipeline_id=status["pipeline_id"], status=status["status"],
            connector_name=status["connector_name"], topic_name=status["topic_name"],
            runtime=runtime_summary,
        )

    async def pause(self, org_id: str, pipeline_id: str) -> dict:
        return await self._lifecycle(org_id, pipeline_id, "paused", "pausing", "pause_pipeline", PipelinePauseResult)

    async def resume(self, org_id: str, pipeline_id: str) -> dict:
        return await self._lifecycle(org_id, pipeline_id, "running", "resuming", "resume_pipeline", PipelineResumeResult)

    async def restart(self, org_id: str, pipeline_id: str) -> dict:
        # desired_state recorded for the reconciler is "running", not
        # "restarting": a restart is a one-shot action (issued once, right
        # below), not an ongoing intent — the reconciler treats the pipeline
        # exactly like any other desired-running pipeline afterward,
        # self-healing it again if needed. Recording "restarting" as a
        # persistent desired_state would make the reconciler re-issue a
        # restart on every single tick forever.
        return await self._lifecycle(
            org_id, pipeline_id, "running", "restarting", "restart_pipeline", PipelineRestartResult
        )

    async def _lifecycle(
        self, org_id: str, pipeline_id: str, target_status: str, transitional_status: str,
        orchestrator_method: str, result_model: type[PipelinePauseResult | PipelineResumeResult | PipelineRestartResult],
    ) -> dict:
        pipeline = await self.get_or_404(org_id, pipeline_id)
        # Transitional while a reconciler will confirm the outcome; the final
        # state directly otherwise (no async operation to model).
        status_to_set = transitional_status if settings.runtime_deploy_enabled else target_status
        self._assert_transition(pipeline, status_to_set, allow_self=True)
        runtime: dict[str, Any] = {}
        if settings.runtime_deploy_enabled:
            await runtime_reconciler.record_desired(
                org_id, pipeline_id, pipeline["connector_name"], target_status, pool_id=pool_of(pipeline)
            )
            orchestrator = RuntimeOrchestratorService(client_for_pipeline(pipeline))
            runtime = await getattr(orchestrator, orchestrator_method)(pipeline)
        updated = await self._persist_pipeline_fields(org_id, pipeline_id, {"status": status_to_set})
        if settings.runtime_deploy_enabled:
            # Connect has already applied the change; have the reconciler observe it now so "pausing"/"resuming" settles
            # in seconds rather than after the rest of its 15s interval.
            runtime_reconciler.request_tick()
        logger.info(
            f"pipeline.{status_to_set}", org_id=org_id, pipeline_id=pipeline_id
        )
        status = self.build_status(updated)
        # GAP-11: RuntimeOrchestratorService.pause_pipeline/resume_pipeline/
        # restart_pipeline key their status under "status", not "state" the
        # way this method's own fallback (and deploy()'s inline runtime
        # dict) does — normalizing both into PipelineRuntimeSummary.state
        # fixes a real pre-existing inconsistency, not just adds a schema.
        runtime_summary = (
            PipelineRuntimeSummary(state=runtime.get("status", status_to_set), connector=runtime.get("connector_name"))
            if runtime else PipelineRuntimeSummary(state=status_to_set)
        )
        return build_result(
            result_model,
            pipeline_id=status["pipeline_id"], status=status["status"],
            connector_name=status["connector_name"], topic_name=status["topic_name"],
            runtime=runtime_summary,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def get_reconciliation_state(self, pipeline_id: str) -> dict | None:
        """The reconciler's durable view of this pipeline's connector — desired
        vs. observed state/generation, last reconcile attempt, last error. None
        if the pipeline has never gone through a lifecycle operation."""
        return await runtime_reconciler.get_state(pipeline_id)

    def build_status(self, pipeline: dict, reconciliation: dict | None = None) -> dict:
        runtime: dict[str, Any] = {"tasks": [], "state": pipeline["status"]}
        if reconciliation:
            runtime["reconciliation"] = reconciliation
        return {
            "pipeline_id": pipeline["id"],
            "status": pipeline["status"],
            "connector_name": pipeline["connector_name"],
            "topic_name": pipeline["topic_name"],
            "runtime": runtime,
        }

    def _assert_transition(self, pipeline: dict, target: str, *, allow_self: bool = False) -> None:
        current = pipeline.get("status", "draft")
        # FE-BUG-25: deploy()/_lifecycle() pass allow_self=True because
        # claim_lifecycle_transition already moved the pipeline to `target`
        # atomically at submission time, before either of them runs — by
        # the time they call this, the pipeline is already sitting at the
        # exact status it's about to "transition" to, and rejecting that
        # self-transition would make every legitimately-claimed operation
        # fail its own internal check. Everywhere else (delete_pipeline
        # included — see test_deleting_a_pipeline_twice_is_rejected) this
        # stays strict: a resource already mid-transition is NOT a valid
        # target for the same transition again, since nothing atomically
        # claimed it first the way deploy/pause/resume/restart now do.
        if allow_self and current == target:
            return
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot transition pipeline from '{current}' to '{target}'. "
                    f"Allowed: {sorted(allowed) or 'none'}."
                ),
            )

    async def _build_pipeline_row(self, org_id: str, payload: PipelineCreate) -> dict:
        # Every org-scoped reference on the payload (connection, transform
        # policy, schema version, quality policy) is validated in one place —
        # see pipeline_spec_validator.py — so a stale or cross-org id is
        # rejected here rather than silently accepted and only discovered
        # later at deploy time (or never, for references nothing dereferences).
        refs = await validate_pipeline_references(org_id, payload)
        generated = await generate_pipeline_config(
            org_id=org_id,
            pipeline=payload,
            connector_template=refs.template,
            connection_profile=refs.connection,
            transform_policy=refs.transform,
            topic_policy=refs.topic_policy,
        )
        # Pin the exact template revision this pipeline is built against —
        # see ConnectorTemplateRevision's docstring in app/models/catalog.py.
        # deploy() (and the runtime reconciler) read this snapshot instead of
        # the live template, so editing the template later — PostgreSQL is
        # authoritative for it, see catalog_service.py — can never
        # retroactively change what an already-created pipeline deploys.
        # Callers decide whether to re-pin it on update; see update_pipeline.
        # Explicit empty-collection defaults, not a bare KeyError or None: a
        # template dict missing one of these (a test fixture, or a
        # hand-inserted row) would otherwise blow up build_connector()'s /
        # generate_pipeline_config()'s .update()/.get(x, {}) calls on it.
        template_snapshot = {
            "connector_class": refs.template.get("connector_class"),
            "default_config": refs.template.get("default_config") or {},
            "default_transforms": refs.template.get("default_transforms") or [],
            "config_schema": refs.template.get("config_schema") or {},
            "supported_transforms": refs.template.get("supported_transforms") or [],
            "connector_plugin": refs.template.get("connector_plugin"),
            "connector_version": refs.template.get("connector_version"),
            "supported_kafka_connect_versions": refs.template.get("supported_kafka_connect_versions") or [],
            "supported_source_versions": refs.template.get("supported_source_versions") or [],
            "config_schema_version": refs.template.get("config_schema_version"),
            "transform_compatibility": refs.template.get("transform_compatibility") or {},
            "migration_notes": refs.template.get("migration_notes"),
            "supported_modes": refs.template.get("supported_modes") or [],
            "supports_dlq": refs.template.get("supports_dlq", True),
            "supports_schema_discovery": refs.template.get("supports_schema_discovery", True),
        }
        assert set(template_snapshot) == set(REVISION_FIELDS)
        return payload.model_dump(exclude_none=True) | {
            "org_id": org_id,
            **generated,
            "template_revision_id": refs.template.get("current_revision_id"),
            "template_snapshot": template_snapshot,
            # GAP-12: derived from the resolved template's connector_type
            # (validate_pipeline_references), never user-submitted.
            "direction": refs.direction,
            # FE-BUG-14: only meaningful for create_pipeline (a fresh row
            # starts at 1) — update_pipeline sets its own `revision` in
            # updated_fields explicitly and never reads this dict's copy.
            "revision": 1,
        }


pipeline_service = PipelineService()
