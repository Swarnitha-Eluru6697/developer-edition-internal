"""Where the sweep learns what happened to an organization's pipelines. Unlike the Workspace (which asks other services over HTTP), all of
it is in DataConnect's own database:

  state       dc_pipeline_runtime_state (what the reconciler last heard from Kafka Connect) + dc_pipelines.status (a stalled quality
              worker shows there as "degraded")                                   -> stored as an observation when it CHANGES
  operation   dc_operations: deploy / pause / resume / restart that finished           -> stored, one per operation
  threshold   dc_audit_logs "paused_threshold_exceeded" (the reconciler's error-threshold pause)  -> stored, one per pause
  dlq         dc_failure_counts kind=ingestion, one-minute buckets                   -> read live every sweep (already time-bucketed)
  quality     dc_failure_counts kind=validation, key "*" (failing records)            -> read live every sweep
  drift       dc_schema_drift_approvals still pending                                  -> read live every sweep

Each collector NEVER raises: a source that cannot be read is reported as not ok, and the sweep carries on with the others. A source being
unreadable is not itself a pipeline failure, so it raises no alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditLog,
    FailureCount,
    Operation,
    Pipeline,
    PipelineRuntimeState,
    SchemaDriftApproval,
)
from app.services.alerts.engine import Obs, reason_text
from app.services.alerts.kinds import LOOKBACK_SECONDS, span_text
from app.services.failure_counters import INGESTION, TOTAL_KEY, VALIDATION

log = structlog.get_logger(__name__)

PIPELINE_OPERATIONS = ("pipeline_deploy", "pipeline_pause", "pipeline_resume", "pipeline_restart")
# What a stored state observation can be. "absent" (no connector: never deployed or being deleted) is not a failure and not watched.
WATCHED_STATES = ("running", "paused", "failed", "degraded", "unknown")


@dataclass
class SourceResult:
    source: str
    ok: bool = True
    detail: str = ""
    observations: list[Obs] = field(default_factory=list)


def aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


@dataclass(frozen=True)
class PipelineInfo:
    name: str
    direction: str
    status: str


async def pipelines_of(db: AsyncSession, org_id: str) -> dict[str, PipelineInfo]:
    rows = (await db.execute(select(Pipeline.id, Pipeline.name, Pipeline.direction, Pipeline.status).where(Pipeline.org_id == org_id))).all()
    return {str(r.id): PipelineInfo(r.name, (r.direction or "SOURCE").upper(), r.status or "") for r in rows}


def state_of(row: PipelineRuntimeState, info: PipelineInfo) -> str | None:
    """The state the alert rules see. A connector that is RUNNING while its quality worker is stuck is "degraded" (the reconciler marks
    the pipeline so, keeping observed_state as Connect reported it). None = nothing to watch (no connector, or being deleted)."""
    if row.desired_state == "deleted" or info.status == "deleting":
        return None
    state = row.observed_state or "unknown"
    if state == "running" and info.status == "degraded":
        return "degraded"
    return state if state in WATCHED_STATES else None


async def collect_state(db: AsyncSession, org_id: str, pipelines: dict[str, PipelineInfo], last: dict[str, tuple[str, str | None]],
                        now: datetime) -> SourceResult:
    """A new observation for every pipeline whose (state, reason) differs from the last one stored. `last`: pipeline id -> (outcome,
    detail) of its newest stored state observation."""
    result = SourceResult("runtime")
    try:
        rows = (await db.scalars(select(PipelineRuntimeState).where(PipelineRuntimeState.org_id == org_id))).all()
        for row in rows:
            info = pipelines.get(str(row.pipeline_id))
            if info is None:
                continue
            state = state_of(row, info)
            if state is None:
                continue
            detail = reason_text(row.last_runtime_error) or None if state in ("failed", "degraded", "unknown") else None
            if last.get(str(row.pipeline_id)) == (state, detail):
                continue
            # "unknown" counts from the last time the state WAS known (last_observed_at, never last_reconciled_at: see the model's
            # docstring); anything else from when this sweep first saw it.
            when = (aware(row.last_observed_at) or now) if state == "unknown" else now
            result.observations.append(Obs(
                pipeline_id=str(row.pipeline_id), pipeline_name=info.name, direction=info.direction, category="state", outcome=state,
                occurred_at=when, ref_id=f"{row.pipeline_id}:{state}:{when.isoformat()}", detail=detail))
    except Exception as e:  # noqa: BLE001 - reported, never breaks the sweep
        result.ok, result.detail = False, f"pipeline states could not be read ({type(e).__name__})"
        log.warning("alerts.source_failed", source="runtime", org_id=org_id, error=type(e).__name__)
    return result


async def collect_operations(db: AsyncSession, org_id: str, pipelines: dict[str, PipelineInfo], known: set[str],
                             now: datetime) -> SourceResult:
    """Deploys / pauses / resumes / restarts that finished: failed ones raise, succeeded ones tell the rule the latest action worked."""
    result = SourceResult("operations")
    try:
        rows = (await db.scalars(select(Operation).where(
            Operation.org_id == org_id, Operation.operation_type.in_(PIPELINE_OPERATIONS),
            Operation.status.in_(("succeeded", "failed", "stale")),
            Operation.completed_at >= now - timedelta(seconds=LOOKBACK_SECONDS)))).all()
        for op in rows:
            ref = f"{op.id}|{op.operation_type}"
            info = pipelines.get(str(op.resource_id))
            if ref in known or info is None or op.completed_at is None:
                continue
            if op.status == "succeeded":
                outcome, detail = "succeeded", None
            elif op.status == "stale":
                outcome, detail = "failed", "it was retried until it gave up without finishing"
            else:
                outcome = "failed"
                detail = " ".join(x for x in (op.error_message, f"[{op.error_code}]" if op.error_code else None) if x) or None
            result.observations.append(Obs(
                pipeline_id=str(op.resource_id), pipeline_name=info.name, direction=info.direction, category="operation", outcome=outcome,
                occurred_at=aware(op.completed_at), ref_id=ref, detail=detail))
    except Exception as e:  # noqa: BLE001
        result.ok, result.detail = False, f"operations could not be read ({type(e).__name__})"
        log.warning("alerts.source_failed", source="operations", org_id=org_id, error=type(e).__name__)
    return result


async def collect_threshold_pauses(db: AsyncSession, org_id: str, pipelines: dict[str, PipelineInfo], known: set[str],
                                   now: datetime) -> SourceResult:
    result = SourceResult("audit")
    try:
        rows = (await db.scalars(select(AuditLog).where(
            AuditLog.org_id == org_id, AuditLog.action == "paused_threshold_exceeded",
            AuditLog.created_at >= now - timedelta(seconds=LOOKBACK_SECONDS)))).all()
        for row in rows:
            info = pipelines.get(str(row.resource_id))
            if str(row.id) in known or info is None:
                continue
            after = row.after_state or {}
            detail = None
            if after.get("error_count") is not None and after.get("max_errors") is not None:
                window = after.get("window_seconds")
                detail = (f"{after['error_count']} dead-letter records" + (f" in {span_text(int(window))}" if window else "")
                          + f", limit {after['max_errors']}")
            result.observations.append(Obs(
                pipeline_id=str(row.resource_id), pipeline_name=info.name, direction=info.direction, category="threshold", outcome="paused",
                occurred_at=aware(row.created_at), ref_id=str(row.id), detail=detail))
    except Exception as e:  # noqa: BLE001
        result.ok, result.detail = False, f"error-threshold pauses could not be read ({type(e).__name__})"
        log.warning("alerts.source_failed", source="audit", org_id=org_id, error=type(e).__name__)
    return result


async def collect_counts(db: AsyncSession, org_id: str, pipelines: dict[str, PipelineInfo], now: datetime) -> SourceResult:
    """Dead-letter and quality-failure counts, one observation per pipeline per one-minute bucket. Not stored: the counters already are."""
    result = SourceResult("failure counters")
    try:
        since = now - timedelta(seconds=LOOKBACK_SECONDS)
        rows = (await db.execute(
            select(FailureCount.pipeline_id, FailureCount.kind, FailureCount.bucket_start, func.sum(FailureCount.count))
            .where(FailureCount.org_id == org_id, FailureCount.bucket_start >= since,
                   ((FailureCount.kind == INGESTION) | ((FailureCount.kind == VALIDATION) & (FailureCount.key == TOTAL_KEY))))
            .group_by(FailureCount.pipeline_id, FailureCount.kind, FailureCount.bucket_start))).all()
        for pid, kind, bucket, total in rows:
            info = pipelines.get(str(pid))
            if info is None or not total:
                continue
            category = "dlq" if kind == INGESTION else "quality"
            when = aware(bucket)
            result.observations.append(Obs(
                pipeline_id=str(pid), pipeline_name=info.name, direction=info.direction, category=category, outcome="count",
                occurred_at=when, ref_id=f"{pid}:{category}:{when.isoformat()}", count=int(total)))
    except Exception as e:  # noqa: BLE001
        result.ok, result.detail = False, f"failure counters could not be read ({type(e).__name__})"
        log.warning("alerts.source_failed", source="failure_counts", org_id=org_id, error=type(e).__name__)
    return result


async def collect_drift(db: AsyncSession, org_id: str, pipelines: dict[str, PipelineInfo]) -> SourceResult:
    result = SourceResult("schema drift")
    try:
        rows = (await db.scalars(select(SchemaDriftApproval).where(
            SchemaDriftApproval.org_id == org_id, SchemaDriftApproval.status == "pending"))).all()
        for row in rows:
            info = pipelines.get(str(row.pipeline_id)) if row.pipeline_id else None
            if info is None:
                continue
            when = aware(row.detected_at or row.created_at)
            result.observations.append(Obs(
                pipeline_id=str(row.pipeline_id), pipeline_name=info.name, direction=info.direction, category="drift", outcome="pending",
                occurred_at=when, ref_id=str(row.id)))
    except Exception as e:  # noqa: BLE001
        result.ok, result.detail = False, f"schema drift approvals could not be read ({type(e).__name__})"
        log.warning("alerts.source_failed", source="drift", org_id=org_id, error=type(e).__name__)
    return result
