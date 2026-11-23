"""FlinkSQL Jobs API."""
from __future__ import annotations
from uuid import UUID
from fastapi import APIRouter, HTTPException, Query, Request, status
from app.core.config import get_settings
from app.core.deps import DB
from app.core.deps import RequireFlinkDeploy, RequireFlinkEdit, RequireJobRead
from app.core.workspace_scope import authorize_workspace
from app.schemas import (
    DeployRequest, FlinkJobCreate, FlinkJobOut, FlinkJobUpdate,
    DeploymentManifestOut, FlinkDeploymentOut, JobStatusOut, MetricHistoryOut, MetricHistoryPoint,
    StopRequest, ValidateRequest, ValidateResponse,
    ThroughputMetrics, BackpressureInfo, CheckpointInfo, KafkaLagInfo,
)
from app.services.job_service import JobService
from app.executors.flink_gateway import get_flink_client

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", response_model=list[FlinkJobOut])
async def list_jobs(
    user: RequireJobRead, db: DB,
    workspace_id: UUID | None = Query(None),
) -> list[FlinkJobOut]:
    svc = JobService(db)
    jobs = await svc.list_for_org(user.org_id, workspace_id)
    return [FlinkJobOut.model_validate(j) for j in jobs]


def _require_dq_for_artifacts(user, dq_artifacts, job_kind: str) -> None:
    """X-004: which DQ artifact a quality job came from is DQ's to assert. A user (or Workspace on a user's behalf) cannot
    claim it, and a job that is not a quality job has none."""
    if dq_artifacts is None:
        return
    if getattr(user, "actor", None) != get_settings().dq_delegation_issuer:
        raise HTTPException(status_code=422, detail={
            "code": "DQ_ARTIFACTS_NOT_ACCEPTED",
            "message": "dq_artifacts is only accepted from the Data Quality service"})
    if job_kind != "quality":
        raise HTTPException(status_code=422, detail={
            "code": "DQ_ARTIFACTS_NOT_APPLICABLE", "message": "dq_artifacts only describes a quality job"})


@router.post("", response_model=FlinkJobOut, status_code=status.HTTP_201_CREATED)
async def create_job(payload: FlinkJobCreate, user: RequireFlinkEdit, db: DB) -> FlinkJobOut:
    payload.workspace_id = authorize_workspace(user, payload.workspace_id)   # FL-003: a label Workspace vouched for
    _require_dq_for_artifacts(user, payload.dq_artifacts, payload.job_kind)
    svc = JobService(db)
    job = await svc.create(org_id=user.org_id, user_id=user.user_id, req=payload)
    return FlinkJobOut.model_validate(job)


@router.get("/{job_id}", response_model=FlinkJobOut)
async def get_job(job_id: UUID, user: RequireJobRead, db: DB) -> FlinkJobOut:
    return FlinkJobOut.model_validate(await JobService(db).get(user.org_id, job_id))


@router.patch("/{job_id}", response_model=FlinkJobOut)
async def update_job(job_id: UUID, payload: FlinkJobUpdate, user: RequireFlinkEdit, db: DB) -> FlinkJobOut:
    if payload.dq_artifacts is not None:
        _require_dq_for_artifacts(user, payload.dq_artifacts, (await JobService(db).get(user.org_id, job_id)).job_kind)
    return FlinkJobOut.model_validate(await JobService(db).update(user.org_id, job_id, payload))


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: UUID, user: RequireFlinkEdit, db: DB) -> None:
    await JobService(db).delete(user.org_id, job_id)


@router.post("/{job_id}/validate", response_model=FlinkJobOut)
async def validate_job(job_id: UUID, user: RequireFlinkEdit, db: DB) -> FlinkJobOut:
    return FlinkJobOut.model_validate(await JobService(db).validate(user.org_id, job_id))


@router.post("/{job_id}/deploy", response_model=FlinkDeploymentOut, status_code=status.HTTP_202_ACCEPTED)
async def deploy_job(job_id: UUID, payload: DeployRequest, user: RequireFlinkDeploy, db: DB) -> FlinkDeploymentOut:
    dep = await JobService(db).deploy(user.org_id, job_id, user.user_id, payload)
    return FlinkDeploymentOut.model_validate(dep)


@router.post("/{job_id}/stop", response_model=FlinkDeploymentOut, status_code=status.HTTP_202_ACCEPTED)
async def stop_job(job_id: UUID, payload: StopRequest, user: RequireFlinkDeploy, db: DB) -> FlinkDeploymentOut:
    dep = await JobService(db).stop(user.org_id, job_id, payload)
    return FlinkDeploymentOut.model_validate(dep)


@router.post("/{job_id}/cancel", response_model=FlinkDeploymentOut, status_code=status.HTTP_202_ACCEPTED)
async def cancel_job(job_id: UUID, user: RequireFlinkDeploy, db: DB) -> FlinkDeploymentOut:
    dep = await JobService(db).cancel(user.org_id, job_id)
    return FlinkDeploymentOut.model_validate(dep)


@router.post("/{job_id}/savepoint", status_code=status.HTTP_202_ACCEPTED)
async def trigger_savepoint(job_id: UUID, user: RequireFlinkDeploy, db: DB) -> dict:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.core.config import get_settings
    from app.core.errors import validation_failed
    from app.models import FlinkDeployment, FlinkSavepoint
    svc = JobService(db)
    job = await svc.get(user.org_id, job_id, for_update=True)   # read-modify-write of savepoint_path
    dep = (await db.execute(
        select(FlinkDeployment).where(FlinkDeployment.job_id == job.id,
                                      FlinkDeployment.status == "running")
        .order_by(FlinkDeployment.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if job.status != "running" or not dep or not dep.flink_job_id:
        raise validation_failed("deployment", "Job is not running")
    settings = get_settings()
    path = await get_flink_client().trigger_savepoint(dep.flink_job_id, settings.checkpoint_storage_path)
    # RUN-FLINK-013: the savepoint is tied to this generation + SQL artifact.
    job.savepoint_path = path
    job.savepoint_generation = dep.generation
    job.savepoint_sql_hash = dep.sql_hash
    await db.execute(pg_insert(FlinkSavepoint).values(
        job_id=job.id, deployment_id=dep.id, generation=dep.generation, sql_hash=dep.sql_hash,
        path=path, kind="savepoint",
    ).on_conflict_do_nothing(constraint="uq_flink_savepoint_job_path"))
    await db.flush()
    return {"savepoint_path": path, "job_id": str(job_id), "generation": dep.generation}


@router.get("/{job_id}/status", response_model=JobStatusOut)
async def get_job_status(job_id: UUID, user: RequireJobRead, db: DB) -> JobStatusOut:
    from sqlalchemy import select
    from app.models import FlinkDeployment, FlinkMetricSnapshot
    svc = JobService(db)
    job = await svc.get(user.org_id, job_id)
    dep_result = await db.execute(
        select(FlinkDeployment).where(FlinkDeployment.job_id == job.id)
        .order_by(FlinkDeployment.created_at.desc()).limit(1)
    )
    dep = dep_result.scalar_one_or_none()
    snap_result = await db.execute(
        select(FlinkMetricSnapshot).where(FlinkMetricSnapshot.job_id == job.id)
        .order_by(FlinkMetricSnapshot.captured_at.desc()).limit(1)
    )
    snap = snap_result.scalar_one_or_none()

    flink_job_id = dep.flink_job_id if dep else None
    runtime_ms: int | None = None
    if dep and dep.started_at:
        import datetime as dt
        runtime_ms = int((dt.datetime.now(dt.timezone.utc) - dep.started_at).total_seconds() * 1000)

    # Same windowed evaluation the poller alerts on (RUN-FLINK-015) — not a
    # single reading, and restarts counted within the window.
    from app.services.metrics_poller import evaluate_alerts
    alerts: list[str] = await evaluate_alerts(job.id, snap) if snap else []

    return JobStatusOut(
        job_id=job.id, flink_job_id=flink_job_id, status=job.status,
        flink_state=dep.flink_state if dep else None,
        runtime_ms=runtime_ms,
        num_restarts=snap.num_restarts if snap else None,
        throughput=ThroughputMetrics(
            records_in_per_sec=snap.records_in_per_sec if snap else None,
            records_out_per_sec=snap.records_out_per_sec if snap else None,
            bytes_in_per_sec=snap.bytes_in_per_sec if snap else None,
            bytes_out_per_sec=snap.bytes_out_per_sec if snap else None,
        ),
        kafka_lag=_kafka_lag_info(snap) if snap else None,
        checkpoint=CheckpointInfo(
            id=snap.last_checkpoint_id,
            status=snap.checkpoint_status,
            size_bytes=snap.last_checkpoint_size_bytes,
            duration_ms=snap.last_checkpoint_duration_ms,
            savepoint_path=job.savepoint_path,
            completed=snap.checkpoints_completed or 0,
            failed=snap.checkpoints_failed or 0,
            in_progress=0,
        ) if snap else None,
        backpressure=BackpressureInfo(
            max_ratio=snap.backpressure_max if snap else None,
            per_operator=snap.backpressure_detail if snap else {},
        ),
        watermark_lag_ms=snap.watermark_lag_ms if snap else None,
        heap_used_bytes=snap.heap_used_bytes if snap else None,
        heap_max_bytes=snap.heap_max_bytes if snap else None,
        last_polled_at=dep.last_polled_at if dep else None,
        alerts=alerts,
    )


@router.get("/{job_id}/metrics/history", response_model=MetricHistoryOut)
async def get_metric_history(
    job_id: UUID, user: RequireJobRead, db: DB,
    hours: int = Query(default=3, ge=1, le=168),
) -> MetricHistoryOut:
    from sqlalchemy import select
    from app.models import FlinkMetricSnapshot
    import datetime as dt
    svc = JobService(db)
    await svc.get(user.org_id, job_id)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    result = await db.execute(
        select(FlinkMetricSnapshot)
        .where(FlinkMetricSnapshot.job_id == job_id,
               FlinkMetricSnapshot.captured_at >= since)
        .order_by(FlinkMetricSnapshot.captured_at)
    )
    snaps = result.scalars().all()
    return MetricHistoryOut(
        job_id=job_id,
        points=[
            MetricHistoryPoint(
                ts=s.captured_at,
                records_in_per_sec=s.records_in_per_sec,
                records_out_per_sec=s.records_out_per_sec,
                kafka_consumer_lag=s.kafka_consumer_lag,
                checkpoint_duration_ms=s.last_checkpoint_duration_ms,
                backpressure_max=s.backpressure_max,
                watermark_lag_ms=s.watermark_lag_ms,
            ) for s in snaps
        ],
    )


@router.get("/{job_id}/deployments", response_model=list[FlinkDeploymentOut])
async def list_deployments(job_id: UUID, user: RequireJobRead, db: DB) -> list[FlinkDeploymentOut]:
    from sqlalchemy import select
    from app.models import FlinkDeployment
    svc = JobService(db)
    await svc.get(user.org_id, job_id)
    result = await db.execute(
        select(FlinkDeployment).where(FlinkDeployment.job_id == job_id)
        .order_by(FlinkDeployment.created_at.desc())
    )
    return [FlinkDeploymentOut.model_validate(d) for d in result.scalars()]


@router.get("/{job_id}/deployments/{deployment_id}/manifest", response_model=DeploymentManifestOut)
async def get_deployment_manifest(job_id: UUID, deployment_id: UUID, user: RequireJobRead, db: DB) -> DeploymentManifestOut:
    """The audit record of what this deployment executed: the frozen SQL, name, engine, Git source, subject and
    versions, and their hash (X-004). Contains no credential."""
    from app.core.errors import not_found
    from app.models import FlinkDeployment
    await JobService(db).get(user.org_id, job_id)
    dep = await db.get(FlinkDeployment, deployment_id)
    if dep is None or dep.job_id != job_id:
        raise not_found("deployment", str(deployment_id))
    return DeploymentManifestOut.model_validate(dep)


def _kafka_lag_info(snap) -> KafkaLagInfo:
    """Snapshot -> API lag view. Handles both the current detail shape
    ({"per_partition": {topic: {partition: lag}}}) and the legacy flat
    {"topic:partition": lag} rows written before RUN-FLINK-015."""
    detail = snap.kafka_lag_detail or {}
    if "per_partition" in detail or "status" in detail:
        per_partition = {t: {str(p): int(v) for p, v in parts.items()}
                         for t, parts in (detail.get("per_partition") or {}).items()}
    else:
        per_partition = {}
        for key, lag in detail.items():
            topic, _, partition = str(key).rpartition(":")
            per_partition.setdefault(topic or str(key), {})[partition or "0"] = int(lag or 0)
    available = snap.kafka_consumer_lag is not None
    return KafkaLagInfo(
        available=available,
        total_lag=snap.kafka_consumer_lag,
        consumer_groups=list(detail.get("consumer_groups") or []),
        per_topic={t: sum(parts.values()) for t, parts in per_partition.items()},
        per_partition=per_partition,
        reason=None if available else (detail.get("reason") or "consumer lag unavailable"),
    )
