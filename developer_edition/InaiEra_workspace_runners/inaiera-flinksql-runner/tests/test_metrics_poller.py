"""RUN-FLINK-015 — metrics_poller.py had three problems:

  1. Kafka consumer lag was a `pass` placeholder — kafka_consumer_lag was
     always persisted as 0, so lag-based alerting was silently dead.
  2. alert_backpressure_window_minutes / alert_restart_window_minutes /
     alert_checkpoint_failure_window_minutes were defined in config but
     never read — every alert fired off a single instantaneous snapshot.
  3. main.py starts this same background loop in every replica, and Helm
     defaults to 2 replicas, so every poll interval was collected (and
     alerted) once per replica — duplicated metric rows and duplicated
     alert events.

These tests prove, against a real Postgres database (advisory locks are
a genuine Postgres feature — nothing to fake here) and a fake Flink/Kafka
client:
  * Only one of two concurrent _tick() calls (simulating two replicas'
    independent timers landing at the same moment) actually polls/writes
    — the other's pg_try_advisory_xact_lock attempt fails and it returns
    immediately, so metrics are collected exactly once per tick.
  * Kafka lag is now actually queried and persisted.
  * Backpressure/restart/checkpoint-failure alerts only fire once the
    condition has held for its whole configured window, not on a single
    reading — and stop firing once a value inside the window drops back
    under threshold.
  * Old snapshots are purged once they're outside METRICS_RETENTION_DAYS.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import FlinkDeployment, FlinkJob, FlinkMetricSnapshot
from app.services import metrics_poller


@pytest.fixture
def flink_db(pg_db):
    """The throwaway, migrated test database (tests/conftest.py pg_db —
    TEST_DATABASE_URL only, refuses anything not named *_test)."""
    return pg_db


KAFKA_SQL = """
CREATE TABLE src (id STRING) WITH (
  'connector' = 'kafka', 'topic' = 'raw.orders',
  'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
  'properties.group.id' = 'orders-etl-group', 'format' = 'json'
);
CREATE TABLE snk (id STRING) WITH ('connector' = 'blackhole');
INSERT INTO snk SELECT id FROM src;
"""


async def _make_running_job(flink_db, *, num_restarts: int = 0, sql: str = KAFKA_SQL,
                            consumer_groups=None) -> tuple[uuid.UUID, uuid.UUID]:
    async with flink_db() as db:
        job = FlinkJob(org_id="org1", name=f"job-{uuid.uuid4().hex[:8]}",
                       sql=sql, status="running")
        db.add(job)
        await db.flush()
        dep = FlinkDeployment(job_id=job.id, trigger="manual", status="running",
                              flink_job_id="flink-job-1", consumer_groups=consumer_groups or [])
        db.add(dep)
        await db.commit()
        return job.id, dep.id


class _FakeFlinkClient:
    def __init__(self, *, backpressure: float = 0.0, num_restarts: int = 0,
                 checkpoint_status: str | None = "COMPLETED"):
        self.get_job_metrics_calls = 0
        self._backpressure = backpressure
        self._num_restarts = num_restarts
        self._checkpoint_status = checkpoint_status

    async def get_job_metrics(self, flink_job_id):
        self.get_job_metrics_calls += 1
        return {
            "records_in_per_sec": 10.0, "records_out_per_sec": 10.0,
            "bytes_in_per_sec": 100.0, "bytes_out_per_sec": 100.0,
            "num_restarts": self._num_restarts,
            "backpressure": {"vertex-1": self._backpressure},
            "heap_used_bytes": 3_000_000_000, "heap_max_bytes": 4_000_000_000,
            "watermark_lag_ms": 1500,
        }

    async def get_checkpoint_info(self, flink_job_id):
        return {"id": 1, "status": self._checkpoint_status, "size_bytes": 100,
                "duration_ms": 50, "completed": 1, "failed": 0}

    async def get_job(self, flink_job_id):
        return {"state": "RUNNING"}


class _FakeKafkaClient:
    def __init__(self, *, total_lag: int = 500, available: bool = True):
        self._total_lag = total_lag
        self._available = available
        self.calls = 0
        self.groups: list[str] = []

    async def get_consumer_group_lag(self, group_id):
        self.calls += 1
        self.groups.append(group_id)
        if not self._available:
            return {}
        return {"total_lag": self._total_lag, "per_partition": {"raw.orders": {"0": self._total_lag}}}


def _patch_clients(monkeypatch, flink_client, kafka_client):
    monkeypatch.setattr("app.services.metrics_poller.get_flink_client", lambda: flink_client)
    monkeypatch.setattr("app.kafka.client.get_kafka_client", lambda: kafka_client)


async def _insert_snapshot(flink_db, job_id, dep_id, *, captured_at, backpressure_max=None,
                            checkpoint_status=None, num_restarts=None):
    async with flink_db() as db:
        db.add(FlinkMetricSnapshot(
            job_id=job_id, deployment_id=dep_id, captured_at=captured_at,
            backpressure_max=backpressure_max, checkpoint_status=checkpoint_status,
            num_restarts=num_restarts,
        ))
        await db.commit()


# ---------------------------------------------------------------------
# Leader election — one tick, one collector
# ---------------------------------------------------------------------
async def test_tick_collects_metrics_and_persists_a_snapshot(flink_db, monkeypatch):
    job_id, _dep_id = await _make_running_job(flink_db)
    flink_client = _FakeFlinkClient()
    kafka_client = _FakeKafkaClient(total_lag=42)
    _patch_clients(monkeypatch, flink_client, kafka_client)

    await metrics_poller._tick()

    assert flink_client.get_job_metrics_calls == 1
    # RUN-FLINK-015: the group the SQL declares — not a guessed flink-{name}.
    assert kafka_client.groups == ["orders-etl-group"]
    async with flink_db() as db:
        snaps = (await db.execute(
            select(FlinkMetricSnapshot).where(FlinkMetricSnapshot.job_id == job_id)
        )).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].kafka_consumer_lag == 42
    assert snaps[0].kafka_lag_detail["per_partition"] == {"raw.orders": {"0": 42}}
    assert snaps[0].kafka_lag_detail["consumer_groups"] == ["orders-etl-group"]
    assert snaps[0].watermark_lag_ms == 1500
    assert snaps[0].heap_used_bytes == 3_000_000_000   # BIGINT — would overflow int4


async def test_persisted_deployment_consumer_groups_are_used(flink_db, monkeypatch):
    await _make_running_job(flink_db, consumer_groups=["g-a", "g-b"])
    kafka_client = _FakeKafkaClient(total_lag=10)
    _patch_clients(monkeypatch, _FakeFlinkClient(), kafka_client)
    await metrics_poller._tick()
    assert kafka_client.groups == ["g-a", "g-b"]


async def test_unavailable_lag_is_recorded_as_null_not_zero(flink_db, monkeypatch):
    job_id, _ = await _make_running_job(flink_db)
    _patch_clients(monkeypatch, _FakeFlinkClient(), _FakeKafkaClient(available=False))
    await metrics_poller._tick()
    async with flink_db() as db:
        snap = (await db.execute(select(FlinkMetricSnapshot)
                                 .where(FlinkMetricSnapshot.job_id == job_id))).scalar_one()
    assert snap.kafka_consumer_lag is None
    assert snap.kafka_lag_detail["status"] == "unavailable"


async def test_lag_is_null_when_no_consumer_group_is_declared(flink_db, monkeypatch):
    job_id, _ = await _make_running_job(flink_db, sql="INSERT INTO t SELECT 1;")
    kafka_client = _FakeKafkaClient()
    _patch_clients(monkeypatch, _FakeFlinkClient(), kafka_client)
    await metrics_poller._tick()
    assert kafka_client.calls == 0   # nothing guessed
    async with flink_db() as db:
        snap = (await db.execute(select(FlinkMetricSnapshot)
                                 .where(FlinkMetricSnapshot.job_id == job_id))).scalar_one()
    assert snap.kafka_consumer_lag is None
    assert snap.kafka_lag_detail["status"] == "unknown"


async def test_status_api_reports_unknown_lag_as_unavailable_not_zero(flink_db):
    from app.api.jobs import _kafka_lag_info
    unknown = FlinkMetricSnapshot(kafka_consumer_lag=None,
                                  kafka_lag_detail={"status": "unavailable", "reason": "proxy down"})
    info = _kafka_lag_info(unknown)
    assert info.available is False and info.total_lag is None and info.reason == "proxy down"

    known = FlinkMetricSnapshot(kafka_consumer_lag=7, kafka_lag_detail={
        "status": "ok", "consumer_groups": ["g"],
        "per_partition": {"t1": {"0": 3, "1": 4}}})
    info = _kafka_lag_info(known)
    assert info.available and info.total_lag == 7
    assert info.per_partition == {"t1": {"0": 3, "1": 4}} and info.per_topic == {"t1": 7}

    legacy = FlinkMetricSnapshot(kafka_consumer_lag=5, kafka_lag_detail={"t1:0": 2, "t1:1": 3})
    info = _kafka_lag_info(legacy)
    assert info.per_partition == {"t1": {"0": 2, "1": 3}} and info.per_topic == {"t1": 5}


async def test_only_the_leader_replica_collects_and_keeps_collecting(flink_db, monkeypatch):
    """Lifetime leader lock: replica B never collects while A is alive —
    not just when their ticks happen to overlap (the old per-tick lock)."""
    import asyncio
    from app.services.coordination import LeaderElector

    job_id, _dep_id = await _make_running_job(flink_db)
    flink_client = _FakeFlinkClient()
    _patch_clients(monkeypatch, flink_client, _FakeKafkaClient())

    a = LeaderElector(metrics_poller.LEADER_LOCK_NAME)
    b = LeaderElector(metrics_poller.LEADER_LOCK_NAME)
    try:
        ran = await asyncio.gather(metrics_poller.tick_if_leader(a), metrics_poller.tick_if_leader(b))
        assert sorted(ran) == [False, True]
        leader, follower = (a, b) if ran[0] else (b, a)
        # Sequential, non-overlapping ticks: still only the leader collects.
        assert await metrics_poller.tick_if_leader(follower) is False
        assert await metrics_poller.tick_if_leader(leader) is True
        assert flink_client.get_job_metrics_calls == 2
        async with flink_db() as db:
            snaps = (await db.execute(
                select(FlinkMetricSnapshot).where(FlinkMetricSnapshot.job_id == job_id)
            )).scalars().all()
        assert len(snaps) == 2
        # Leader goes away -> the follower takes over.
        await leader.release()
        assert await metrics_poller.tick_if_leader(follower) is True
    finally:
        await a.release()
        await b.release()


# ---------------------------------------------------------------------
# Windowed alerts
# ---------------------------------------------------------------------
async def test_backpressure_alert_fires_only_after_sustained_for_the_full_window(flink_db, monkeypatch):
    job_id, dep_id = await _make_running_job(flink_db)
    now = datetime.now(timezone.utc)
    # Prior snapshots inside the (default 5-minute) window, all high —
    # sustained backpressure, not a single blip.
    await _insert_snapshot(flink_db, job_id, dep_id, captured_at=now - timedelta(minutes=4, seconds=30),
                           backpressure_max=0.95)
    await _insert_snapshot(flink_db, job_id, dep_id, captured_at=now - timedelta(minutes=2),
                           backpressure_max=0.9)

    snap = FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id, captured_at=now,
                                backpressure_max=0.85)
    published = []
    monkeypatch.setattr(metrics_poller, "get_redis", lambda: _FakeRedis(published))

    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap)

    assert len(published) == 1
    assert any(a.startswith("backpressure_high") for a in published[0]["alerts"])


async def test_backpressure_alert_does_not_fire_if_it_recovered_within_the_window(flink_db, monkeypatch):
    job_id, dep_id = await _make_running_job(flink_db)
    now = datetime.now(timezone.utc)
    # One earlier reading inside the window was BELOW threshold — not
    # sustained for the whole window, so this must not alert yet.
    await _insert_snapshot(flink_db, job_id, dep_id, captured_at=now - timedelta(minutes=3),
                           backpressure_max=0.1)

    snap = FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id, captured_at=now,
                                backpressure_max=0.95)
    published = []
    monkeypatch.setattr(metrics_poller, "get_redis", lambda: _FakeRedis(published))

    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap)

    assert published == []


async def test_frequent_restarts_alert_uses_delta_within_window_not_lifetime_total(flink_db, monkeypatch):
    job_id, dep_id = await _make_running_job(flink_db)
    now = datetime.now(timezone.utc)
    settings = metrics_poller.get_settings()
    # Lifetime total is already high (10), but only 1 restart happened
    # inside the alert_restart_window_minutes window — must not alert.
    await _insert_snapshot(
        flink_db, job_id, dep_id,
        captured_at=now - timedelta(minutes=settings.alert_restart_window_minutes - 1),
        num_restarts=9,
    )
    snap = FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id, captured_at=now, num_restarts=10)
    published = []
    monkeypatch.setattr(metrics_poller, "get_redis", lambda: _FakeRedis(published))

    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap)
    assert published == []

    # Now enough restarts happened inside the window to cross the threshold.
    snap2 = FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id, captured_at=now,
                                num_restarts=9 + settings.alert_restart_threshold + 1)
    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap2)
    assert len(published) == 1
    assert any(a.startswith("frequent_restarts") for a in published[0]["alerts"])


async def test_single_high_lag_reading_does_not_alert_but_sustained_lag_does(flink_db, monkeypatch):
    job_id, dep_id = await _make_running_job(flink_db)
    settings = metrics_poller.get_settings()
    now = datetime.now(timezone.utc)
    published = []
    monkeypatch.setattr(metrics_poller, "get_redis", lambda: _FakeRedis(published))
    high = settings.alert_kafka_lag_threshold + 1

    snap = FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id, captured_at=now,
                               kafka_consumer_lag=high)
    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap)
    assert published == []   # one reading is not a window

    for minutes in (4.5, 3, 1.5):
        async with flink_db() as db:
            db.add(FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id,
                                       captured_at=now - timedelta(minutes=minutes),
                                       kafka_consumer_lag=high))
            await db.commit()
    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap)
    assert len(published) == 1
    assert any(a.startswith("kafka_lag_high") for a in published[0]["alerts"])


async def test_unknown_lag_never_alerts(flink_db, monkeypatch):
    job_id, dep_id = await _make_running_job(flink_db)
    published = []
    monkeypatch.setattr(metrics_poller, "get_redis", lambda: _FakeRedis(published))
    snap = FlinkMetricSnapshot(job_id=job_id, deployment_id=dep_id,
                               captured_at=datetime.now(timezone.utc), kafka_consumer_lag=None)
    await metrics_poller._check_alerts(await _get_job(flink_db, job_id), snap)
    assert published == []


class _FakeRedis:
    def __init__(self, sink: list):
        self._sink = sink

    async def publish(self, channel, payload):
        import json
        self._sink.append(json.loads(payload))


async def _get_job(flink_db, job_id) -> FlinkJob:
    async with flink_db() as db:
        return await db.get(FlinkJob, job_id)


# ---------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------
async def test_retention_purges_snapshots_older_than_configured_days(flink_db, monkeypatch):
    job_id, dep_id = await _make_running_job(flink_db)
    settings = metrics_poller.get_settings()
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=settings.metrics_retention_days + 1)
    recent = now - timedelta(hours=1)
    await _insert_snapshot(flink_db, job_id, dep_id, captured_at=old)
    await _insert_snapshot(flink_db, job_id, dep_id, captured_at=recent)

    await metrics_poller._maybe_purge_old_snapshots()

    async with flink_db() as db:
        remaining = (await db.execute(
            select(FlinkMetricSnapshot).where(FlinkMetricSnapshot.job_id == job_id)
        )).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].captured_at.replace(tzinfo=timezone.utc) - recent < timedelta(seconds=1)
