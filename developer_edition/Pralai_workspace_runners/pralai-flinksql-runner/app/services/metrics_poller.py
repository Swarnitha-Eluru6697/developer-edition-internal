"""Background metrics poller (RUN-FLINK-015).

Single collector: the loop holds a Postgres session-level advisory lock
(app.services.coordination.LeaderElector) for its whole lifetime, so
exactly one replica polls. (The previous per-tick transaction lock only
prevented two ticks from OVERLAPPING — two replicas whose timers didn't
coincide both collected, duplicating snapshots and alerts.) If the leader
dies its connection closes, Postgres drops the lock, and another replica's
next ensure() takes over.

Each tick, for every running deployment:
  1. JobManager metrics (throughput, backpressure, heap, watermark lag —
     now - slowest operator's currentInputWatermark, NULL when no watermark).
  2. Checkpoint state (FAILED when the latest failure is newer than the
     latest completion).
  3. Kafka consumer lag for the consumer group(s) the deployed SQL really
     uses (persisted at deploy from 'properties.group.id'; never guessed).
     Lag that can't be obtained is stored as NULL (unknown) — never 0.
  4. Persist a FlinkMetricSnapshot (tied to the deployment, i.e. to its
     generation + SQL artifact hash).
  5. Windowed alerts (every alert, lag included, must hold across a window
     the snapshots actually cover) published to Redis.
  6. Retention purge.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
import httpx
import structlog
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.executors.flink_gateway import get_flink_client
from app.models import FlinkDeployment, FlinkJob, FlinkMetricSnapshot
from app.services.coordination import LeaderElector

log = structlog.get_logger()
_redis: aioredis.Redis | None = None

LEADER_LOCK_NAME = "flinksql_metrics_poller"
_elector: LeaderElector | None = None

# Leader-local; a new leader simply purges on its own schedule.
_last_retention_sweep: datetime | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def get_elector() -> LeaderElector:
    global _elector
    if _elector is None:
        _elector = LeaderElector(LEADER_LOCK_NAME)
    return _elector


async def metrics_poll_loop() -> None:
    settings = get_settings()
    elector = get_elector()
    log.info("metrics_poller_starting", interval=settings.metrics_poll_interval_seconds)
    try:
        while True:
            try:
                await tick_if_leader(elector)
            except Exception:
                log.exception("metrics_poll_failed")
            await asyncio.sleep(settings.metrics_poll_interval_seconds)
    finally:
        await elector.release()


async def tick_if_leader(elector: LeaderElector) -> bool:
    """Run one collection tick iff `elector` holds (or can take) the
    lifetime leader lock. Returns whether it collected."""
    if not await elector.ensure():
        log.debug("metrics_poller_not_leader")
        return False
    await _tick()
    return True


async def _tick() -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(FlinkJob, FlinkDeployment)
            .join(FlinkDeployment, FlinkDeployment.job_id == FlinkJob.id)
            .where(FlinkJob.status == "running", FlinkDeployment.status == "running")
        )).fetchall()

    for job, dep in rows:
        try:
            await _poll_job(job, dep)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                # The Flink job is gone (e.g. cluster restart). The deployment
                # reconciler marks it failed; don't traceback every tick.
                log.warning("metrics_poll_job_not_found", job_id=str(job.id),
                            flink_job_id=dep.flink_job_id)
            else:
                log.exception("metrics_poll_job_failed", job_id=str(job.id))
        except Exception:
            log.exception("metrics_poll_job_failed", job_id=str(job.id))

    await _maybe_purge_old_snapshots()


async def _consumer_lag(job: FlinkJob, dep: FlinkDeployment) -> tuple[int | None, dict]:
    """(total_lag | None, detail). None = unknown — no declared consumer
    group, REST Proxy unavailable, or the group not found."""
    groups = list(dep.consumer_groups or [])
    if not groups:
        # Deployments from before consumer_groups was persisted: derive from
        # the SQL that was actually submitted (it can't change while running).
        from app.services.job_service import resolved_consumer_groups
        groups = resolved_consumer_groups(job)
    if not groups:
        return None, {"status": "unknown", "reason": "no Kafka source declares properties.group.id"}

    from app.kafka.client import get_kafka_client
    kc = get_kafka_client()
    total = 0
    per_partition: dict[str, dict[str, int]] = {}
    for group in groups:
        try:
            info = await kc.get_consumer_group_lag(group)
        except Exception as e:  # noqa: BLE001
            log.warning("kafka_lag_poll_failed", job_id=str(job.id), group=group, error=str(e))
            info = {}
        if not info or info.get("total_lag") is None:
            return None, {"status": "unavailable", "consumer_groups": groups,
                          "reason": f"lag for consumer group {group!r} unavailable"}
        total += int(info["total_lag"])
        for topic, parts in (info.get("per_partition") or {}).items():
            bucket = per_partition.setdefault(topic, {})
            for partition, lag in parts.items():
                bucket[str(partition)] = bucket.get(str(partition), 0) + int(lag)
    return total, {"status": "ok", "consumer_groups": groups, "per_partition": per_partition}


async def _poll_job(job: FlinkJob, dep: FlinkDeployment) -> None:
    if not dep.flink_job_id:
        return
    client = get_flink_client()
    now = datetime.now(timezone.utc)

    metrics = await client.get_job_metrics(dep.flink_job_id)
    checkpoint = await client.get_checkpoint_info(dep.flink_job_id)
    flink_state = (await client.get_job(dep.flink_job_id)).get("state", "RUNNING")
    kafka_lag, kafka_lag_detail = await _consumer_lag(job, dep)

    bp = metrics.get("backpressure", {}) or {}
    bp_values = [v for v in bp.values() if isinstance(v, (int, float))]
    bp_max = max(bp_values) if bp_values else None

    snap = FlinkMetricSnapshot(
        job_id=job.id, deployment_id=dep.id, captured_at=now,
        records_in_per_sec=metrics.get("records_in_per_sec"),
        records_out_per_sec=metrics.get("records_out_per_sec"),
        bytes_in_per_sec=metrics.get("bytes_in_per_sec"),
        bytes_out_per_sec=metrics.get("bytes_out_per_sec"),
        kafka_consumer_lag=kafka_lag,
        kafka_lag_detail=kafka_lag_detail,
        last_checkpoint_id=checkpoint.get("id"),
        last_checkpoint_size_bytes=checkpoint.get("size_bytes"),
        last_checkpoint_duration_ms=checkpoint.get("duration_ms"),
        checkpoint_status=checkpoint.get("status"),
        checkpoints_completed=checkpoint.get("completed"),
        checkpoints_failed=checkpoint.get("failed"),
        backpressure_max=bp_max,
        backpressure_detail=bp,
        heap_used_bytes=metrics.get("heap_used_bytes"),
        heap_max_bytes=metrics.get("heap_max_bytes"),
        num_restarts=metrics.get("num_restarts"),
        watermark_lag_ms=metrics.get("watermark_lag_ms"),
    )

    async with AsyncSessionLocal() as db:
        db.add(snap)
        dep_row = await db.get(FlinkDeployment, dep.id)
        if dep_row:
            dep_row.last_polled_at = now
            dep_row.flink_state = flink_state
        if flink_state in ("FAILED", "CANCELED", "FINISHED"):
            # Only a job we still believe is running is moved — a concurrent
            # stop/cancel owns the transition otherwise (row-locked CAS).
            job_row = (await db.execute(
                select(FlinkJob).where(FlinkJob.id == job.id).with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if job_row and job_row.status == "running" and dep_row and dep_row.status == "running":
                new = {"FAILED": "failed", "CANCELED": "cancelled", "FINISHED": "completed"}[flink_state]
                dep_row.status = new
                dep_row.stopped_at = now
                job_row.status = new
        await db.commit()

    await _check_alerts(job, snap)


async def _snapshots_since(job_id: uuid.UUID, since: datetime) -> list[FlinkMetricSnapshot]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(FlinkMetricSnapshot)
            .where(FlinkMetricSnapshot.job_id == job_id, FlinkMetricSnapshot.captured_at >= since)
            .order_by(FlinkMetricSnapshot.captured_at.asc())
        )
        return list(result.scalars())


def _sustained(window: list, snap: FlinkMetricSnapshot, minutes: int, predicate) -> bool:
    """True iff every snapshot in `window` with a known value satisfies
    `predicate`, and those snapshots span at least
    alert_window_min_coverage of the window (so one reading — or a
    freshly-started job — never fires a windowed alert)."""
    settings = get_settings()
    known = [s for s in window if predicate(s) is not None]
    if not known or not all(predicate(s) for s in known):
        return False
    span = snap.captured_at - min(s.captured_at for s in known)
    return span >= timedelta(minutes=minutes) * settings.alert_window_min_coverage


async def evaluate_alerts(job_id: uuid.UUID, snap: FlinkMetricSnapshot) -> list[str]:
    settings = get_settings()
    alerts: list[str] = []

    if snap.kafka_consumer_lag is not None and snap.kafka_consumer_lag > settings.alert_kafka_lag_threshold:
        since = snap.captured_at - timedelta(minutes=settings.alert_kafka_lag_window_minutes)
        window = await _snapshots_since(job_id, since)
        if _sustained(window + [snap], snap, settings.alert_kafka_lag_window_minutes,
                      lambda s: None if s.kafka_consumer_lag is None
                      else s.kafka_consumer_lag > settings.alert_kafka_lag_threshold):
            alerts.append(f"kafka_lag_high:{snap.kafka_consumer_lag}")

    if snap.backpressure_max is not None and snap.backpressure_max > settings.alert_backpressure_threshold:
        since = snap.captured_at - timedelta(minutes=settings.alert_backpressure_window_minutes)
        window = await _snapshots_since(job_id, since)
        if _sustained(window + [snap], snap, settings.alert_backpressure_window_minutes,
                      lambda s: None if s.backpressure_max is None
                      else s.backpressure_max > settings.alert_backpressure_threshold):
            alerts.append(f"backpressure_high:{snap.backpressure_max:.2f}")

    if snap.checkpoint_status == "FAILED":
        since = snap.captured_at - timedelta(minutes=settings.alert_checkpoint_failure_window_minutes)
        window = await _snapshots_since(job_id, since)
        if _sustained(window + [snap], snap, settings.alert_checkpoint_failure_window_minutes,
                      lambda s: None if s.checkpoint_status is None else s.checkpoint_status == "FAILED"):
            alerts.append("checkpoint_failed")

    if snap.num_restarts is not None:
        since = snap.captured_at - timedelta(minutes=settings.alert_restart_window_minutes)
        window = [s for s in await _snapshots_since(job_id, since) if s.num_restarts is not None]
        # Cumulative counter — alert on the increase WITHIN the window.
        baseline = window[0].num_restarts if window else snap.num_restarts
        restarts_in_window = snap.num_restarts - baseline
        if restarts_in_window > settings.alert_restart_threshold:
            alerts.append(f"frequent_restarts:{restarts_in_window}")
    return alerts


async def _check_alerts(job: FlinkJob, snap: FlinkMetricSnapshot) -> None:
    alerts = await evaluate_alerts(job.id, snap)
    if alerts:
        r = get_redis()
        await r.publish(
            f"flink:alerts:{job.id}",
            json.dumps({"job_id": str(job.id), "name": job.name, "alerts": alerts,
                        "ts": snap.captured_at.isoformat()}),
        )
        log.warning("flink_alerts_fired", job_id=str(job.id), alerts=alerts)


async def _maybe_purge_old_snapshots() -> None:
    """Delete snapshots older than METRICS_RETENTION_DAYS (at most hourly)."""
    global _last_retention_sweep
    settings = get_settings()
    now = datetime.now(timezone.utc)
    if _last_retention_sweep is not None and (now - _last_retention_sweep) < timedelta(hours=1):
        return
    _last_retention_sweep = now

    cutoff = now - timedelta(days=settings.metrics_retention_days)
    async with AsyncSessionLocal() as db:
        result = await db.execute(delete(FlinkMetricSnapshot).where(FlinkMetricSnapshot.captured_at < cutoff))
        await db.commit()
        if result.rowcount:
            log.info("metrics_retention_purged", deleted=result.rowcount, cutoff=cutoff.isoformat())
