"""FlinkSQL job lifecycle service.

Restart- and replica-safe lifecycle (RUN-FLINK-011/013/014):

  * deploy()/stop()/cancel() lock the job row (SELECT ... FOR UPDATE) for
    their read-modify-write and claim it with a guarded UPDATE; deploy()
    atomically takes the next per-job GENERATION and records the SHA-256 of
    the SQL artifact it will submit.
  * Idempotency keys are bound to a fingerprint of the request they were
    first used with — the same key with a different payload is a 422.
  * Before anything is sent to Flink, the deployment row durably records
    how to find the submission again: a unique `submission_tag` (submitted
    as the job's `pipeline.name`), the Gateway session, and the time the
    submission started; the Gateway operation handle is committed the
    moment the Gateway accepts the job-submitting statement, and the Flink
    job id the moment it is known. A crash at ANY point after submission
    therefore leaves enough to re-attach instead of resubmitting.
  * The process driving a deploy/stop holds a heartbeated per-row lease
    (app.services.coordination); the reconciler (leader-elected, one
    replica) only touches rows whose lease expired, after re-claiming them.
  * If Flink accepted a job but its id couldn't be read, the job is located
    by its unique pipeline name and adopted — or, if even that is
    impossible right now, the deployment is left 'deploying' with its
    submission record for the reconciler. It is never left untracked.
  * A failed stop leaves job and deployment consistent with what Flink
    actually reports (still running -> back to 'running'; gone -> terminal;
    unknown -> left 'pausing' for the reconciler).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import conflict, flush_or_conflict, not_found, validation_failed
from app.core.sql_script import (
    JOB_SUBMITTING_KINDS,
    is_job_submitting,
    split_sql_script,
    statement_kind,
)
from app.executors.flink_gateway import JobIdUnavailableError, get_flink_client
from app.models import FlinkDeployment, FlinkJob, FlinkSavepoint
from app.schemas import DeployRequest, FlinkJobCreate, FlinkJobUpdate, StopRequest
from app.services import execution_manifest, pg_lake
from app.services.coordination import LeaseHeartbeat, claim_lease
from app.services.sql_plan_service import kafka_consumer_groups
from app.services.validate_service import validate_job_sql

log = structlog.get_logger()

_DEPLOYABLE_STATUSES = ("validated", "paused", "failed", "cancelled")
_LOCKED_STATUSES = ("running", "deploying", "pausing", "cancelling")

TERMINAL_FLINK_STATES = ("FAILED", "CANCELED", "FINISHED")
# job_state() pseudo-states (app.executors.flink_gateway.FlinkGatewayClient.job_state)
NOT_FOUND = "NOT_FOUND"
UNKNOWN = "UNKNOWN"

# Postgres NOTIFY channel that wakes the leader's reconciler when a deploy/stop is persisted (FL-011).
WORK_CHANNEL = "flink_deployment_work"

# Deployment ids this process is currently driving — an in-process guard
# only; cross-process safety comes from the per-row lease.
_ACTIVE_DEPLOYMENT_IDS: set[uuid.UUID] = set()


def is_alive_state(state: str | None) -> bool:
    return bool(state) and state not in TERMINAL_FLINK_STATES and state not in (NOT_FOUND, UNKNOWN)


def sql_artifact_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def request_fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _idempotency_mismatch() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "IDEMPOTENCY_KEY_REUSED",
                "message": "This idempotency key was already used with a different request payload"},
    )


def _check_fingerprint(stored: str | None, current: str) -> None:
    # Rows created before fingerprints existed (stored is None) are accepted.
    if stored is not None and stored != current:
        raise _idempotency_mismatch()


def _source_record(source) -> dict | None:
    """The job's Git provenance as stored: credentials stripped from the URL, the commit lowercased."""
    if source is None:
        return None
    return {"git_url": execution_manifest.redact_url(source.git_url), "git_ref": source.git_ref,
            "sql_path": source.sql_path, "commit_sha": source.commit_sha.lower(), "provider": source.provider,
            "requested_ref": source.requested_ref,
            "imported_at": source.imported_at.isoformat() if source.imported_at else None}


def _dq_record(record) -> dict | None:
    """The DQ artifact record as stored (X-004): JSON-safe, ids as strings."""
    return None if record is None else record.model_dump(mode="json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_until() -> datetime:
    return _now() + timedelta(seconds=get_settings().deployment_lease_ttl_seconds)


def submission_tag_for(deployment: FlinkDeployment) -> str:
    """Unique per deployment; used as the Flink job's pipeline.name."""
    return f"inaiera-{deployment.job_id.hex[:12]}-g{deployment.generation or 0}-{deployment.id.hex[:12]}"


def resolved_consumer_groups(job: FlinkJob) -> list[str]:
    return kafka_consumer_groups(resolve_placeholders(job.sql, job_name=job.name))


NAME_CONSTRAINT = "uq_flink_job_org_name"


def _name_taken(name: str | None) -> HTTPException:
    return conflict(f"Job '{name}' already exists in this org", code="JOB_NAME_TAKEN", resource="job", field="name", details={"name": name})


class JobService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, *, org_id: str, user_id: str, req: FlinkJobCreate) -> FlinkJob:
        existing = await self._db.execute(
            select(FlinkJob).where(FlinkJob.org_id == org_id, FlinkJob.name == req.name)
        )
        if existing.scalar_one_or_none():
            raise _name_taken(req.name)
        job = FlinkJob(
            org_id=org_id, created_by_user_id=user_id,
            workspace_id=req.workspace_id,
            name=req.name, description=req.description,
            sql=req.sql, parallelism=req.parallelism,
            sink_engine=req.sink_engine, status="draft",
            job_kind=req.job_kind, ucs_fqn=req.ucs_fqn, source=_source_record(req.source),
            dq_artifacts=_dq_record(req.dq_artifacts),
        )
        # RUN-FLINK-015: the real consumer group, from the SQL itself.
        groups = kafka_consumer_groups(resolve_placeholders(req.sql, job_name=req.name))
        job.consumer_group_id = groups[0] if groups else None
        self._db.add(job)
        await flush_or_conflict(self._db, f"Job '{req.name}' already exists in this org", constraint=NAME_CONSTRAINT,
                                code="JOB_NAME_TAKEN", details={"name": req.name})
        await self._db.refresh(job)
        return job

    async def get(self, org_id: str, job_id: uuid.UUID, *, for_update: bool = False) -> FlinkJob:
        if for_update:
            job = (await self._db.execute(
                select(FlinkJob).where(FlinkJob.id == job_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
        else:
            job = await self._db.get(FlinkJob, job_id)
        if not job or job.org_id != org_id:
            raise not_found("flink_job", str(job_id))
        return job

    async def list_for_org(
        self, org_id: str, workspace_id: uuid.UUID | None = None,
    ) -> list[FlinkJob]:
        stmt = select(FlinkJob).where(FlinkJob.org_id == org_id)
        if workspace_id:
            stmt = stmt.where(FlinkJob.workspace_id == workspace_id)
        result = await self._db.execute(stmt.order_by(FlinkJob.updated_at.desc()))
        return list(result.scalars())

    async def update(self, org_id: str, job_id: uuid.UUID, req: FlinkJobUpdate) -> FlinkJob:
        job = await self.get(org_id, job_id, for_update=True)
        if job.status in _LOCKED_STATUSES:
            raise validation_failed("status", f"Cannot edit a job while it is {job.status} — stop it first")
        if req.name is not None and req.name != job.name:
            taken = (await self._db.execute(
                select(FlinkJob.id).where(FlinkJob.org_id == org_id, FlinkJob.name == req.name, FlinkJob.id != job.id)
            )).scalar_one_or_none()
            if taken:
                raise _name_taken(req.name)
        if req.name is not None:
            job.name = req.name
        if req.description is not None:
            job.description = req.description
        if req.sql is not None and req.sql != job.sql:
            job.sql = req.sql
            job.status = "draft"   # editing SQL resets validation
            # A savepoint belongs to one SQL artifact; new SQL retires it.
            job.savepoint_path = None
            job.savepoint_generation = None
            job.savepoint_sql_hash = None
            # X-004: a recorded Git source describes the SQL it was imported with; new SQL without a new source
            # is no longer from that commit.
            job.source = _source_record(req.source)
            # Same for the DQ artifact it was generated from.
            job.dq_artifacts = _dq_record(req.dq_artifacts)
        elif req.source is not None:
            job.source = _source_record(req.source)           # a re-sync of identical SQL at a new commit
        if req.dq_artifacts is not None and not (req.sql is not None and req.sql != job.sql):
            job.dq_artifacts = _dq_record(req.dq_artifacts)   # DQ re-asserting the artifact for identical SQL
        if req.parallelism is not None:
            job.parallelism = req.parallelism
        if req.sink_engine is not None:
            job.sink_engine = req.sink_engine
        groups = resolved_consumer_groups(job)
        job.consumer_group_id = groups[0] if groups else None
        # (org, name) is unique: renaming onto an existing job — or losing a race for the name — is a conflict the caller can act on,
        # not an internal error (W-022, FL-002). Only THAT constraint is translated; anything else stays an error.
        await flush_or_conflict(self._db, f"Job '{req.name}' already exists in this org", constraint=NAME_CONSTRAINT,
                                code="JOB_NAME_TAKEN", details={"name": req.name})
        await self._db.refresh(job)
        return job

    async def delete(self, org_id: str, job_id: uuid.UUID) -> None:
        job = await self.get(org_id, job_id, for_update=True)
        if job.status in _LOCKED_STATUSES:
            raise validation_failed("status", f"Cannot delete a job while it is {job.status} — stop it first")
        await self._db.delete(job)

    async def validate(self, org_id: str, job_id: uuid.UUID) -> FlinkJob:
        job = await self.get(org_id, job_id, for_update=True)
        if job.status in _LOCKED_STATUSES:
            raise validation_failed("status", f"Cannot validate a job while it is {job.status}")
        job.status = "validating"
        await self._db.flush()
        result = await validate_job_sql(job.sql)
        job.validation_errors = result["errors"]
        job.status = "validated" if result["valid"] else "draft"
        await self._db.flush()
        await self._db.refresh(job)
        return job

    # ── deploy ────────────────────────────────────────────────────────────

    async def _deployment_by_key(self, job_id: uuid.UUID, *, key: str, stop: bool) -> FlinkDeployment | None:
        col = FlinkDeployment.stop_idempotency_key if stop else FlinkDeployment.idempotency_key
        return (await self._db.execute(
            select(FlinkDeployment).where(FlinkDeployment.job_id == job_id, col == key)
        )).scalar_one_or_none()

    async def deploy(
        self, org_id: str, job_id: uuid.UUID, user_id: str, req: DeployRequest,
    ) -> FlinkDeployment:
        fingerprint = request_fingerprint(
            {"from_savepoint": req.from_savepoint, "parallelism": req.parallelism})
        # Row lock: concurrent deploys of this job serialize here, so the
        # idempotency lookup below always sees a committed earlier attempt.
        job = await self.get(org_id, job_id, for_update=True)

        if req.idempotency_key:
            existing = await self._deployment_by_key(job.id, key=req.idempotency_key, stop=False)
            if existing:
                _check_fingerprint(existing.idempotency_request_hash, fingerprint)
                return existing

        if job.status not in _DEPLOYABLE_STATUSES:
            raise validation_failed(
                "status",
                f"Job must be in validated/paused/failed/cancelled state to deploy (current: {job.status})",
            )
        # FL-001: never freeze (or run) SQL that carries a credential, has a placeholder nothing substitutes, or needs a
        # governed pg_lake catalog this runner does not have.
        problems = pg_lake.sql_problems(
            resolve_placeholders(job.sql, job_name=job.name, run_ref="deploy"), get_settings())
        if problems:
            raise validation_failed("sql", "; ".join(problems))
        parallelism = req.parallelism or job.parallelism
        sql_hash = sql_artifact_hash(job.sql)
        from_savepoint = await self._resolve_restore_savepoint(job, req.from_savepoint, sql_hash)

        groups = resolved_consumer_groups(job)
        if groups:
            job.consumer_group_id = groups[0]
        deployment_id = uuid.uuid4()
        try:
            async with self._db.begin_nested():
                # Atomic claim + next generation, in the same savepoint as the
                # insert so a unique-index conflict rolls both back together.
                generation = (await self._db.execute(
                    update(FlinkJob)
                    .where(FlinkJob.id == job.id, FlinkJob.status.in_(_DEPLOYABLE_STATUSES))
                    .values(status="deploying", generation_counter=FlinkJob.generation_counter + 1)
                    .returning(FlinkJob.generation_counter)
                    .execution_options(synchronize_session=False)
                )).scalar_one_or_none()
                if generation is None:
                    raise conflict("Job is no longer in a deployable state", code="JOB_NOT_DEPLOYABLE", resource="job", field="status")
                manifest = execution_manifest.build_manifest(
                    org_id=job.org_id, workspace_id=str(job.workspace_id) if job.workspace_id else None,
                    subject_id=user_id, job_id=str(job.id), job_name=job.name, job_kind=job.job_kind,
                    generation=generation, sql=job.sql, engine=job.sink_engine, parallelism=parallelism,
                    from_savepoint=from_savepoint, source=job.source, dq_artifacts=job.dq_artifacts,
                    target_resource_id=job.ucs_fqn,
                    idempotency_key=req.idempotency_key, flink_version=get_settings().flink_version)
                deployment = FlinkDeployment(
                    id=deployment_id, job_id=job.id, trigger="manual",
                    status="submitting",
                    deployed_by_user_id=user_id,
                    from_savepoint=from_savepoint,
                    parallelism=parallelism,
                    idempotency_key=req.idempotency_key,
                    idempotency_request_hash=fingerprint if req.idempotency_key else None,
                    submitted_at=_now(),
                    generation=generation,
                    sql_hash=sql_hash,
                    manifest=manifest,
                    manifest_sha256=execution_manifest.manifest_sha256(manifest),
                    consumer_groups=groups,
                    # FL-011: no owner and no lease. The request process does not run the deploy; the leader's reconciler claims
                    # the row (compare-and-set on the lease) after this transaction commits.
                    owner_id=None, lease_expires_at=None,
                )
                self._db.add(deployment)
                await self._db.flush()
        except IntegrityError:
            if req.idempotency_key:
                existing = await self._deployment_by_key(job.id, key=req.idempotency_key, stop=False)
                if existing:
                    _check_fingerprint(existing.idempotency_request_hash, fingerprint)
                    return existing
            raise conflict("A deployment for this job is already in progress", code="DEPLOYMENT_IN_PROGRESS", resource="deployment", field="job_id")
        await self._db.refresh(job)
        await self._announce_work(deployment.id)
        return deployment

    async def _resolve_restore_savepoint(
        self, job: FlinkJob, requested: str | None, sql_hash: str,
    ) -> str | None:
        """RUN-FLINK-013: only restore a savepoint into the SQL artifact
        that produced it."""
        if requested:
            sp = (await self._db.execute(
                select(FlinkSavepoint).where(FlinkSavepoint.job_id == job.id,
                                             FlinkSavepoint.path == requested)
            )).scalar_one_or_none()
            if sp and sp.sql_hash and sp.sql_hash != sql_hash:
                raise validation_failed(
                    "from_savepoint",
                    f"Savepoint was taken from generation {sp.generation} with a different SQL "
                    "artifact; it cannot be restored into the current SQL",
                )
            return requested
        if job.savepoint_path and job.savepoint_sql_hash not in (None, sql_hash):
            log.warning("savepoint_skipped_sql_changed", job_id=str(job.id),
                        savepoint=job.savepoint_path)
            return None
        return job.savepoint_path

    # ── stop ──────────────────────────────────────────────────────────────

    async def stop(
        self, org_id: str, job_id: uuid.UUID, req: StopRequest,
    ) -> FlinkDeployment:
        fingerprint = request_fingerprint({"drain": req.drain, "savepoint_path": req.savepoint_path})
        job = await self.get(org_id, job_id, for_update=True)

        if req.idempotency_key:
            existing = await self._deployment_by_key(job.id, key=req.idempotency_key, stop=True)
            if existing:
                _check_fingerprint(existing.stop_idempotency_request_hash, fingerprint)
                return existing

        if job.status != "running":
            raise validation_failed("status", "Job is not running")
        deployment = await self._get_active_deployment(job.id, for_update=True)
        if not deployment or not deployment.flink_job_id:
            raise validation_failed("deployment", "No active Flink job ID found")

        settings = get_settings()
        try:
            async with self._db.begin_nested():
                deployment.stop_savepoint_dir = req.savepoint_path or settings.checkpoint_storage_path
                deployment.stop_drain = req.drain
                deployment.stop_idempotency_key = req.idempotency_key
                deployment.stop_idempotency_request_hash = fingerprint if req.idempotency_key else None
                deployment.stop_trigger_id = None
                # FL-011: the reconciler, not this request, drives the stop (it claims the lease after commit).
                deployment.owner_id = None
                deployment.lease_expires_at = None
                await self._db.flush()
        except IntegrityError:
            await self._db.refresh(deployment)
            if req.idempotency_key:
                existing = await self._deployment_by_key(job.id, key=req.idempotency_key, stop=True)
                if existing:
                    _check_fingerprint(existing.stop_idempotency_request_hash, fingerprint)
                    return existing
            raise conflict("A stop for this job is already in progress", code="STOP_IN_PROGRESS", resource="deployment", field="job_id")

        claim = await self._db.execute(
            update(FlinkJob).where(FlinkJob.id == job.id, FlinkJob.status == "running")
            .values(status="pausing").execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            raise conflict("Job is no longer running", code="JOB_NOT_RUNNING", resource="job", field="status")

        await self._announce_work(deployment.id)
        return deployment

    # ── cancel ────────────────────────────────────────────────────────────

    async def cancel(self, org_id: str, job_id: uuid.UUID) -> FlinkDeployment:
        """RUN-FLINK-014: guarded (row lock + compare-and-set) and
        idempotent — cancelling an already-cancelled job returns its
        cancelled deployment; a concurrent second cancel waits on the row
        lock and then takes that same idempotent path."""
        job = await self.get(org_id, job_id, for_update=True)
        deployment = (await self._db.execute(
            select(FlinkDeployment).where(FlinkDeployment.job_id == job.id)
            .order_by(FlinkDeployment.created_at.desc()).limit(1)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()

        if job.status == "cancelled" and deployment is not None and deployment.status == "cancelled":
            return deployment
        if job.status not in ("running", "pausing", "cancelling"):
            raise validation_failed("status", "Job is not running")
        if not deployment or not deployment.flink_job_id:
            raise validation_failed("deployment", "No active Flink job ID")

        claimed = await self._db.execute(
            update(FlinkJob)
            .where(FlinkJob.id == job.id, FlinkJob.status.in_(("running", "pausing", "cancelling")))
            .values(status="cancelling").execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise conflict("Job state changed while cancelling", code="JOB_STATE_CHANGED", resource="job", field="status")

        client = get_flink_client()
        try:
            await client.cancel_job(deployment.flink_job_id)
        except Exception as e:  # noqa: BLE001
            state = await client.job_state(deployment.flink_job_id)
            if is_alive_state(state) or state == UNKNOWN:
                # Nothing changes in the DB (the request transaction rolls back
                # the 'cancelling' claim) — the job is still running in Flink.
                log.warning("cancel_failed", job_id=str(job.id), error=str(e), flink_state=state)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail={"code": "FLINK_CANCEL_FAILED",
                            "message": f"Flink did not cancel the job (state: {state}): {str(e)[:200]}"},
                )
        await self._db.refresh(job)
        job.status = "cancelled"
        job.last_stopped_at = _now()
        deployment.status = "cancelled"
        deployment.stopped_at = job.last_stopped_at
        deployment.owner_id = None
        deployment.lease_expires_at = None
        await self._db.flush()
        await self._db.refresh(deployment)
        return deployment

    # ── Private ───────────────────────────────────────────────────────────

    async def _announce_work(self, deployment_id: uuid.UUID) -> None:
        """FL-011: persisted desired state (job 'deploying' / 'pausing' + the deployment row) is the work item; this NOTIFY, delivered
        at COMMIT, only wakes the leader's reconciler so it does not wait for its next poll. Losing it costs latency, never the work."""
        await self._db.execute(text("SELECT pg_notify(:channel, :id)"),
                               {"channel": WORK_CHANNEL, "id": str(deployment_id)})

    async def _get_active_deployment(
        self, job_id: uuid.UUID, *, for_update: bool = False,
    ) -> FlinkDeployment | None:
        stmt = (select(FlinkDeployment)
                .where(FlinkDeployment.job_id == job_id, FlinkDeployment.status == "running")
                .order_by(FlinkDeployment.created_at.desc()).limit(1))
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self._db.execute(stmt)).scalar_one_or_none()


# ═════════════════════════════════════════════════════════════════════════
# Background execution: driven ONLY by the leader's reconciler (FL-011) — exactly one
# owner and one implementation of each.
# ═════════════════════════════════════════════════════════════════════════

def _session():
    from app.db import session as db_session
    return db_session.AsyncSessionLocal()


async def _locked(db: AsyncSession, deployment_id: uuid.UUID) -> tuple[FlinkJob | None, FlinkDeployment | None]:
    """Lock the job row then the deployment row (always in that order)."""
    dep = await db.get(FlinkDeployment, deployment_id)
    if dep is None:
        return None, None
    job = (await db.execute(
        select(FlinkJob).where(FlinkJob.id == dep.job_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    dep = (await db.execute(
        select(FlinkDeployment).where(FlinkDeployment.id == deployment_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return job, dep


async def locate_submitted_job(client, tag: str | None) -> tuple[str, str | None, str | None]:
    """Find the job a deployment submitted by its unique pipeline name.
    Returns ("alive", jid, state) | ("terminal", jid, state) |
    ("absent", None, None) | ("unknown", None, None). Extra alive copies
    (should never exist) are cancelled, keeping the newest."""
    if not tag:
        return "absent", None, None
    try:
        jobs = await client.find_jobs_by_name(tag)
    except Exception as e:  # noqa: BLE001
        log.warning("locate_submitted_job_failed", tag=tag, error=str(e))
        return "unknown", None, None
    if not jobs:
        return "absent", None, None
    jobs = sorted(jobs, key=lambda j: j.get("start-time") or 0, reverse=True)
    alive = [j for j in jobs if is_alive_state(j.get("state"))]
    if alive:
        for extra in alive[1:]:
            try:
                await client.cancel_job(extra["jid"])
                log.error("duplicate_submission_cancelled", tag=tag, flink_job_id=extra["jid"])
            except Exception:  # noqa: BLE001
                log.exception("duplicate_submission_cancel_failed", tag=tag)
        return "alive", alive[0]["jid"], alive[0].get("state")
    return "terminal", jobs[0]["jid"], jobs[0].get("state")


def build_submission_sql(job: FlinkJob, deployment: FlinkDeployment, tag: str, *,
                         frozen: dict | None = None) -> str:
    """The SQL submitted to Flink. With `frozen` (the deployment's verified manifest, X-004) the SQL, name and engine
    come from it, never from the mutable job row; without it (a deployment created by an older runner) from the job."""
    src_sql, name, engine = (frozen["sql"]["text"], frozen["job"]["name"], frozen["engine"]) if frozen \
        else (job.sql, job.name, job.sink_engine)
    sql = resolve_placeholders(src_sql, job_name=name, run_ref=str(deployment.id))
    sql = _inject_catalog_config(sql, engine, deployment.from_savepoint)
    if deployment.parallelism != 1:
        sql = f"SET 'parallelism.default' = '{deployment.parallelism}';\n" + sql
    statements = split_sql_script(sql)
    submitting = [i for i, st in enumerate(statements) if is_job_submitting(st)]
    kinds = [statement_kind(statements[i]) for i in submitting]
    job_count = kinds.count("STATEMENT_SET") + (1 if "INSERT" in kinds else 0)
    if job_count == 0:
        raise ValueError("SQL contains no INSERT / STATEMENT SET — nothing to deploy")
    if job_count > 1:
        raise ValueError(
            f"SQL would submit {job_count} separate Flink jobs; combine them into one STATEMENT SET")
    # Our pipeline.name goes immediately before the submitting statement so
    # nothing in the user's SQL can override it.
    statements.insert(submitting[0], f"SET 'pipeline.name' = '{tag}'")
    return "\n;\n".join(statements) + "\n;"


async def _execute_deploy(deployment_id: uuid.UUID, *, lease_held: bool = False) -> None:
    """Drive one deploy attempt. Claims the deployment's lease (unless the
    caller already holds it) and heartbeats it for the duration."""
    if deployment_id in _ACTIVE_DEPLOYMENT_IDS:
        return
    _ACTIVE_DEPLOYMENT_IDS.add(deployment_id)
    try:
        async with _session() as db:
            if not lease_held and not await claim_lease(db, deployment_id):
                log.info("deploy_lease_held_elsewhere", deployment_id=str(deployment_id))
                return
            async with LeaseHeartbeat(deployment_id):
                await _run_deploy(db, deployment_id)
    finally:
        _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)


async def _run_deploy(db: AsyncSession, deployment_id: uuid.UUID) -> None:
    client = get_flink_client()
    deployment = await db.get(FlinkDeployment, deployment_id)
    if not deployment:
        return
    job = await db.get(FlinkJob, deployment.job_id)
    if not job or job.status != "deploying" or deployment.status != "submitting":
        return  # finished, failed or cancelled by someone else

    session_id: str | None = None
    try:
        frozen = None
        if deployment.manifest is not None:
            # X-004: execute exactly what was frozen when the deployment was created (also after a restart-resume).
            try:
                frozen = execution_manifest.verify(deployment.manifest, deployment.manifest_sha256)
            except execution_manifest.ManifestIntegrityError as e:
                raise ValueError(f"refusing to deploy: {e}") from e
        elif deployment.sql_hash and sql_artifact_hash(job.sql) != deployment.sql_hash:
            raise ValueError("Job SQL changed after this deployment was created")
        tag = deployment.submission_tag or submission_tag_for(deployment)
        sql = build_submission_sql(job, deployment, tag, frozen=frozen)

        session_id = await client.create_session(session_name=tag)
        settings = get_settings()
        if pg_lake.references_catalog(sql, settings):
            # FL-001: the governed connection, from runner configuration; the user's SQL only names `pg_lake.<db>.<table>`.
            await pg_lake.register_catalog(client, session_id, settings)
        # RUN-FLINK-013: durable BEFORE the job-submitting statement is sent.
        deployment.submission_tag = tag
        deployment.gateway_session_id = session_id
        deployment.gateway_operation_handle = None
        deployment.submit_started_at = _now()
        deployment.flink_job_id = None
        await db.commit()

        async def _record_operation(op_handle: str, _stmt: str) -> None:
            deployment.gateway_operation_handle = op_handle
            await db.commit()

        try:
            results = await client.execute_statements(session_id, sql, on_submit=_record_operation)
            flink_id = _job_id_from_statement_results(results)
            if not flink_id:
                raise JobIdUnavailableError(
                    "no job id reported for the submitting statement",
                    session_id=session_id, op_handle=deployment.gateway_operation_handle or "",
                    statement="")
        except JobIdUnavailableError as e:
            # RUN-FLINK-011: Flink HAS a job for this deployment — find it by
            # its unique pipeline name rather than leave it untracked.
            outcome, located, _state = await locate_submitted_job(client, tag)
            if outcome == "alive":
                log.warning("deploy_job_id_recovered_by_tag", deployment_id=str(deployment_id),
                            flink_job_id=located)
                flink_id = located
            elif outcome == "terminal":
                raise RuntimeError(f"submitted job {located} is already terminal ({_state})") from e
            elif outcome == "absent":
                raise RuntimeError(
                    f"no job id reported by the Gateway and no Flink job carries this "
                    f"deployment's submission tag {tag!r}") from e
            else:
                deployment.error_message = (
                    f"Flink accepted the submission but its job id is not yet known ({e}); "
                    "left for the reconciler to re-attach via submission_tag")[:1000]
                await db.commit()
                log.error("deploy_job_id_unavailable_left_for_reconciler",
                          deployment_id=str(deployment_id), tag=tag)
                return
        await _finalize_running(db, deployment_id, flink_id)
    except Exception as e:  # noqa: BLE001
        log.exception("flink_deploy_failed", job_id=str(job.id))
        # If the submission may have reached Flink, make sure no orphan job
        # survives a deployment we are about to mark failed.
        if deployment.gateway_operation_handle:
            outcome, located, _ = await locate_submitted_job(client, deployment.submission_tag)
            if outcome == "unknown":
                deployment.error_message = f"submission outcome unknown: {e}"[:1000]
                await db.commit()
                return  # reconciler re-checks via submission_tag
            if outcome == "alive":
                try:
                    await client.cancel_job(located)
                    log.warning("orphan_submission_cancelled", flink_job_id=located)
                except Exception:  # noqa: BLE001
                    deployment.flink_job_id = located
                    deployment.error_message = f"submission failed and orphan cancel failed: {e}"[:1000]
                    await db.commit()
                    return
        await _finalize_failed_deploy(db, deployment_id, str(e))
    finally:
        if session_id:
            try:
                await client.close_session(session_id)
            except Exception:  # noqa: BLE001
                pass


async def _finalize_running(db: AsyncSession, deployment_id: uuid.UUID, flink_id: str) -> None:
    job, dep = await _locked(db, deployment_id)
    if job is None or dep is None:
        await db.rollback()
        return
    dep.flink_job_id = flink_id
    if job.status == "deploying" and dep.status == "submitting":
        dep.status = "running"
        dep.started_at = dep.started_at or _now()
        dep.error_message = None
        job.status = "running"
        job.last_deployed_at = dep.started_at
        await db.commit()
        log.info("flink_job_deployed", job_id=str(job.id), flink_job_id=flink_id,
                 generation=dep.generation)
        return
    # The job was cancelled/changed while we were submitting: the Flink job
    # we just started is unwanted — record it and cancel it.
    await db.commit()
    log.warning("deploy_superseded_cancelling_job", job_id=str(job.id), job_status=job.status,
                flink_job_id=flink_id)
    try:
        await get_flink_client().cancel_job(flink_id)
    except Exception:  # noqa: BLE001
        log.exception("superseded_job_cancel_failed", flink_job_id=flink_id)


async def _finalize_failed_deploy(db: AsyncSession, deployment_id: uuid.UUID, error: str) -> None:
    job, dep = await _locked(db, deployment_id)
    if job is None or dep is None:
        await db.rollback()
        return
    if dep.status == "submitting":
        dep.status = "failed"
        dep.error_message = error[:1000]
    if job.status == "deploying":
        job.status = "failed"
    await db.commit()


async def _execute_stop(deployment_id: uuid.UUID, *, lease_held: bool = False) -> None:
    if deployment_id in _ACTIVE_DEPLOYMENT_IDS:
        return
    _ACTIVE_DEPLOYMENT_IDS.add(deployment_id)
    try:
        async with _session() as db:
            if not lease_held and not await claim_lease(db, deployment_id):
                log.info("stop_lease_held_elsewhere", deployment_id=str(deployment_id))
                return
            async with LeaseHeartbeat(deployment_id):
                await _run_stop(db, deployment_id)
    finally:
        _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)


async def _run_stop(db: AsyncSession, deployment_id: uuid.UUID) -> None:
    client = get_flink_client()
    deployment = await db.get(FlinkDeployment, deployment_id)
    if not deployment:
        return
    job = await db.get(FlinkJob, deployment.job_id)
    if not job or job.status != "pausing" or deployment.status != "running":
        return

    async def _record_trigger(trigger_id: str) -> None:
        deployment.stop_trigger_id = trigger_id
        await db.commit()

    try:
        savepoint_dir = deployment.stop_savepoint_dir or get_settings().checkpoint_storage_path
        savepoint_path = await client.stop_with_savepoint(
            deployment.flink_job_id, savepoint_dir, drain=deployment.stop_drain,
            on_trigger=_record_trigger,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("flink_stop_failed", job_id=str(job.id))
        state = await client.job_state(deployment.flink_job_id)
        await resolve_pausing(db, deployment_id, state, error=str(e))
        return
    await finalize_paused(db, deployment_id, savepoint_path)


async def finalize_paused(db: AsyncSession, deployment_id: uuid.UUID, savepoint_path: str | None) -> None:
    job, dep = await _locked(db, deployment_id)
    if job is None or dep is None or job.status != "pausing":
        await db.rollback()
        return
    dep.status = "paused"
    dep.stopped_at = _now()
    dep.error_message = None
    job.status = "paused"
    job.last_stopped_at = dep.stopped_at
    if savepoint_path:
        job.savepoint_path = savepoint_path
        job.savepoint_generation = dep.generation
        job.savepoint_sql_hash = dep.sql_hash
        await db.execute(
            pg_insert(FlinkSavepoint).values(
                job_id=job.id, deployment_id=dep.id, generation=dep.generation,
                sql_hash=dep.sql_hash, path=savepoint_path, kind="savepoint",
            ).on_conflict_do_nothing(constraint="uq_flink_savepoint_job_path")
        )
    await db.commit()
    log.info("flink_job_stopped", job_id=str(job.id), savepoint=savepoint_path)


async def resolve_pausing(
    db: AsyncSession, deployment_id: uuid.UUID, state: str, *, error: str | None = None,
) -> str:
    """Bring a 'pausing' job whose stop did not (or may not have)
    completed into a state consistent with Flink. Returns the action taken:
    "running" | "paused" | "failed" | "cancelled" | "waiting"."""
    client = get_flink_client()
    dep = await db.get(FlinkDeployment, deployment_id)
    if dep is None:
        return "waiting"

    if state == UNKNOWN:
        # RUN-FLINK-013: a transient JobManager failure is NOT evidence the
        # job stopped — leave 'pausing' for the reconciler.
        if error:
            dep.error_message = f"stop outcome unknown (Flink unreachable): {error}"[:1000]
            await db.commit()
        return "waiting"

    if is_alive_state(state):
        if dep.stop_trigger_id:
            sp = await client.get_savepoint_status(dep.flink_job_id, dep.stop_trigger_id)
            if sp["status"] in ("IN_PROGRESS", "UNKNOWN") and error is None:
                return "waiting"
        # The stop failed and the job is still running: back to 'running'.
        job, dep = await _locked(db, deployment_id)
        if job is None or dep is None or job.status != "pausing":
            await db.rollback()
            return "waiting"
        job.status = "running"
        dep.stop_trigger_id = None
        dep.error_message = f"stop failed, job still running: {error or 'stop not confirmed'}"[:1000]
        await db.commit()
        return "running"

    # Terminal or gone. Did the savepoint complete?
    savepoint_path = None
    if dep.stop_trigger_id:
        sp = await client.get_savepoint_status(dep.flink_job_id, dep.stop_trigger_id)
        if sp["status"] == "COMPLETED":
            savepoint_path = sp.get("location")
    if savepoint_path:
        await finalize_paused(db, deployment_id, savepoint_path)
        return "paused"

    job, dep = await _locked(db, deployment_id)
    if job is None or dep is None or job.status != "pausing":
        await db.rollback()
        return "waiting"
    outcome = "failed" if state == "FAILED" else "cancelled"
    dep.status = outcome
    dep.stopped_at = _now()
    dep.error_message = (
        f"job ended ({state}) without a confirmed savepoint"
        + (f": {error}" if error else ""))[:1000]
    job.status = outcome
    job.last_stopped_at = dep.stopped_at
    await db.commit()
    return outcome


def resolve_placeholders(sql: str, *, job_name: str = "", run_ref: str = "") -> str:
    """Substitute the ${...} placeholders the SQL templates and DQ artifacts use,
    so validate and deploy see exactly the same SQL."""
    settings = get_settings()
    values = {
        "KAFKA_BOOTSTRAP_SERVERS": settings.kafka_bootstrap_servers,
        "SCHEMA_REGISTRY_URL": settings.schema_registry_url,
        "POLARIS_REST_URI": settings.polaris_rest_uri,
        "ICEBERG_WAREHOUSE": settings.iceberg_warehouse,
        "CONSUMER_GROUP_ID": f"flink-{job_name}" if job_name else "flink-job",
        "DQ_RUN_REF": run_ref,
    }
    for key, value in values.items():
        sql = sql.replace(f"${{{key}}}", str(value))
    return sql


def _inject_catalog_config(sql: str, engine: str, from_savepoint: str | None) -> str:
    """Prepend Iceberg/pg_lake catalog SET statements."""
    settings = get_settings()
    config_stmts: list[str] = []

    if engine == "trino":
        config_stmts += [
            f"SET 'execution.checkpointing.interval' = '{settings.checkpoint_interval_seconds}s';",
            f"SET 'execution.checkpointing.timeout' = '{settings.checkpoint_timeout_seconds}s';",
            f"SET 'execution.checkpointing.min-pause-between-checkpoints' = '{settings.checkpoint_min_pause_seconds}s';",
            f"SET 'state.checkpoints.dir' = '{settings.checkpoint_storage_path}';",
            "SET 'table.catalog.inaiera.type' = 'iceberg';",
            "SET 'table.catalog.inaiera.catalog-impl' = 'org.apache.iceberg.rest.RESTCatalog';",
            f"SET 'table.catalog.inaiera.uri' = '{settings.polaris_rest_uri}';",
            f"SET 'table.catalog.inaiera.warehouse' = '{settings.iceberg_warehouse}';",
        ]
    if from_savepoint:
        config_stmts.append(f"SET 'execution.savepoint.path' = '{from_savepoint}';")
    return "\n".join(config_stmts) + "\n" + sql if config_stmts else sql


def _job_id_from_statement_results(results: list[dict]) -> str | None:
    """RUN-FLINK-011: the job id of the JOB-SUBMITTING statement (INSERT /
    STATEMENT SET) — not merely the first result that happens to carry a
    value. Returns None if no submitting statement reported one; raises if
    more than one distinct job was submitted (never guesses between them)."""
    ids: list[str] = []
    for r in results:
        kind = r.get("kind") or statement_kind(r.get("statement") or "")
        if kind in JOB_SUBMITTING_KINDS and r.get("flink_job_id"):
            if r["flink_job_id"] not in ids:
                ids.append(r["flink_job_id"])
    if len(ids) > 1:
        raise RuntimeError(f"more than one Flink job was submitted for one deployment: {ids}")
    return ids[0] if ids else None
