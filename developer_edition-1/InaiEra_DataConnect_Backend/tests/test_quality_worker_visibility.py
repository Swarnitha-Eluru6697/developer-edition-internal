"""
GAP-03: a dead-letter outage stalls a partition (correct: BUG-03 never commits past a record it could not dead-letter), and that stall
is now visible — retry / DLQ-failure counters, blocked-partitions and oldest-uncommitted-age gauges, and a DEGRADED pipeline once the
stall passes a configured age. None of it may ever change the commit decision.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY
from test_quality_stream_dead_letter import (
    DEAD_LETTER_TOPIC,
    PIPELINE,
    POLICY,
    FakeConsumer,
    _record,
    _run_until,
)

import app.services.runtime_reconciler as reconciler_module
from app.core.config import settings
from app.services import pipeline_service as pipeline_service_module
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quality_stream_worker import QualityStreamWorker
from app.services.quality_worker_health import QualityWorkerHealth, quality_stall

SINK = dict(PIPELINE, direction="SINK")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_max_handle_attempts", 2)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_handle_backoff_seconds", 0.0)


@pytest.fixture
def kafka(monkeypatch):
    class Box:
        producer = InMemoryKafkaProducer()

    box = Box()

    async def _ensure(topic, **kwargs):
        return None

    async def _producer():
        return box.producer

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", _ensure)
    monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", _producer)
    return box


@pytest.fixture
def stats(monkeypatch):
    """Replaces the stats write; `fail_times` makes the first N calls raise."""
    class Stats:
        calls = 0
        fail_times = 0

    box = Stats()

    async def _record_outcome(self, **kwargs):
        box.calls += 1
        if box.calls <= box.fail_times:
            raise RuntimeError("the stats database is down")

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    return box


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, {"service": "dataconnect", **labels}) or 0.0


def _worker() -> QualityStreamWorker:
    return QualityStreamWorker(org_id="org_1001", pipeline=SINK, policy=POLICY)


class FlakyDeadLetter:
    """Wraps the in-memory producer: every write to the dead-letter topic fails until `heal()`."""

    def __init__(self, producer) -> None:
        self.producer = producer
        self.down = True
        self.failures = 0

    async def produce(self, *, topic, **kwargs):
        if self.down and topic == DEAD_LETTER_TOPIC:
            self.failures += 1
            raise ConnectionError("broker unavailable")
        return await self.producer.produce(topic=topic, **kwargs)

    def heal(self) -> None:
        self.down = False


@pytest.fixture
def blocked_db(monkeypatch):
    """Records what the stuck worker writes to its runtime-state row."""
    class Box:
        writes: list[tuple[datetime, datetime]] = []
        clears = 0

    box = Box()
    box.writes, box.clears = [], 0

    async def _write(self, since, heartbeat_at):
        box.writes.append((since, heartbeat_at))

    async def _clear(self):
        box.clears += 1

    monkeypatch.setattr(QualityStreamWorker, "_write_blocked_state", _write)
    monkeypatch.setattr(QualityStreamWorker, "_clear_blocked_state", _clear)
    return box


def _outage(kafka, monkeypatch) -> FlakyDeadLetter:
    flaky = FlakyDeadLetter(kafka.producer)

    async def _producer():
        return flaky

    monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", _producer)
    return flaky


# ── a dead-letter outage: counted, visible, and still never committed ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_dead_letter_outage_is_counted_visible_and_never_committed(kafka, stats, blocked_db, monkeypatch) -> None:
    stats.fail_times = 10                                            # the stats step keeps failing -> record must be dead-lettered
    flaky = _outage(kafka, monkeypatch)
    consumer = FakeConsumer([_record({"stock_quantity": 5})])
    before_dlq = _sample("dataconnect_quality_dlq_failures_total", connector_type="sink")
    before_retry = _sample("dataconnect_quality_retries_total", connector_type="sink", operation="stats")
    seen = {}

    def _until() -> bool:
        if flaky.failures >= 3:
            seen["blocked"] = _sample("dataconnect_quality_blocked_partitions")
            seen["age"] = _sample("dataconnect_quality_oldest_uncommitted_age_seconds")
            seen["commits"] = consumer.commits
            return True
        return False

    await _run_until(_worker(), consumer, monkeypatch, until=_until)

    assert seen["commits"] == 0                                      # NEVER commit while the dead-letter write is failing
    assert seen["blocked"] == 1.0                                    # one stuck record -> one blocked partition
    assert seen["age"] > 0.0                                         # and it has been uncommitted for a while
    assert _sample("dataconnect_quality_dlq_failures_total", connector_type="sink") - before_dlq >= 3
    assert _sample("dataconnect_quality_retries_total", connector_type="sink", operation="stats") - before_retry >= 1
    assert blocked_db.writes, "the pipeline's runtime-state row was never told the worker is stuck"
    since, heartbeat = blocked_db.writes[0]
    assert since <= heartbeat


@pytest.mark.asyncio
async def test_when_the_dead_letter_topic_recovers_the_record_is_committed_once_and_the_stall_clears(
    kafka, stats, blocked_db, monkeypatch,
) -> None:
    stats.fail_times = 10
    flaky = _outage(kafka, monkeypatch)
    consumer = FakeConsumer([_record({"stock_quantity": 5})])
    commits_while_down: list[int] = []

    def _until() -> bool:
        if flaky.failures >= 2 and flaky.down:
            commits_while_down.append(consumer.commits)
            flaky.heal()
        return consumer.commits == 1

    await _run_until(_worker(), consumer, monkeypatch, until=_until)

    assert commits_while_down == [0]
    assert consumer.commits == 1
    assert len([r for r in kafka.producer.records if r.topic == DEAD_LETTER_TOPIC]) == 1   # the record was written, not dropped
    assert _sample("dataconnect_quality_blocked_partitions") == 0.0
    assert _sample("dataconnect_quality_oldest_uncommitted_age_seconds") == 0.0
    assert blocked_db.clears >= 1                                    # the database flag is cleared


@pytest.mark.asyncio
async def test_a_healthy_record_never_looks_blocked(kafka, stats, blocked_db, monkeypatch) -> None:
    consumer = FakeConsumer([_record({"stock_quantity": 50})])
    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert consumer.commits == 1
    assert blocked_db.writes == []
    assert blocked_db.clears == 0
    assert _sample("dataconnect_quality_blocked_partitions") == 0.0


@pytest.mark.asyncio
async def test_the_heartbeat_keeps_beating_through_a_long_backoff(kafka, stats, blocked_db, monkeypatch) -> None:
    """One backoff much longer than the heartbeat interval must not let the heartbeat go stale and make a stuck worker look dead."""
    stats.fail_times = 10
    flaky = _outage(kafka, monkeypatch)
    monkeypatch.setattr(settings, "quality_stream_blocked_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "quality_stream_handle_backoff_seconds", 0.4)
    monkeypatch.setattr(settings, "quality_stream_dead_letter_backoff_max_seconds", 0.4)
    consumer = FakeConsumer([_record({"stock_quantity": 5})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: len(blocked_db.writes) >= 8, timeout=3.0)

    assert len(blocked_db.writes) >= 8                               # many beats, within what is only a couple of backoffs
    assert flaky.failures <= 3
    assert consumer.commits == 0


@pytest.mark.asyncio
async def test_a_worker_that_stops_while_stuck_does_not_leave_the_pipeline_reported_stuck(kafka, stats, blocked_db, monkeypatch) -> None:
    stats.fail_times = 10
    flaky = _outage(kafka, monkeypatch)
    consumer = FakeConsumer([_record({"stock_quantity": 5})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: flaky.failures >= 2)

    assert consumer.commits == 0                                     # stopping is not a reason to commit
    assert blocked_db.clears >= 1
    assert _sample("dataconnect_quality_blocked_partitions") == 0.0


def test_blocked_partitions_count_every_partition_the_stuck_worker_owns() -> None:
    health = QualityWorkerHealth()
    health.begin("a")
    health.mark_blocked("a", partitions=3)
    health.begin("b")                                                # in flight but not stuck
    assert health.blocked_partitions() == 3.0
    health.end("a")
    assert health.blocked_partitions() == 0.0


def test_the_oldest_uncommitted_age_is_the_longest_wait_across_workers() -> None:
    health = QualityWorkerHealth()
    assert health.oldest_uncommitted_age(now=100.0) == 0.0
    health.begin("a", now=10.0)
    health.begin("b", now=60.0)
    assert health.oldest_uncommitted_age(now=100.0) == 90.0


# ── DEGRADED after a configured age ──────────────────────────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _state(*, blocked_for: float | None, beat_age: float = 1.0):
    return SimpleNamespace(
        quality_blocked_since=None if blocked_for is None else NOW - timedelta(seconds=blocked_for),
        quality_blocked_heartbeat_at=None if blocked_for is None else NOW - timedelta(seconds=beat_age),
    )


def test_nothing_stuck_means_no_stall() -> None:
    assert quality_stall(_state(blocked_for=None), now=NOW) is None
    assert quality_stall(SimpleNamespace(), now=NOW) is None         # a row from before the columns existed


def test_a_stall_younger_than_the_threshold_is_reported_but_not_degraded(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_stream_degraded_after_seconds", 300.0)
    stall = quality_stall(_state(blocked_for=120), now=NOW)
    assert stall is not None
    assert stall.degraded is False


def test_a_stall_past_the_threshold_is_degraded_with_a_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_stream_degraded_after_seconds", 300.0)
    stall = quality_stall(_state(blocked_for=600), now=NOW)
    assert stall.degraded is True
    assert "10 min" in stall.message
    assert "No data is lost" in stall.message


def test_a_stall_whose_worker_stopped_reporting_no_longer_counts(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_stream_blocked_heartbeat_seconds", 10.0)
    assert quality_stall(_state(blocked_for=3600, beat_age=45), now=NOW) is None      # three beats missed: the worker is gone
    assert quality_stall(_state(blocked_for=3600, beat_age=25), now=NOW) is not None  # still within three beats


# ── the reconciler applies it, without touching the connector ───────────────────────────────────────────────────────

class _Session:
    async def scalar(self, *a, **k):
        return 1

    async def flush(self) -> None:
        pass


class _Repo:
    def __init__(self, session):
        self.session = session

    async def delete(self, row) -> None:
        pass

    async def fence(self, row_id, worker_id, *, lock=False):
        return 1, True


def _runtime_row(*, blocked_for, observed="running", desired="running"):
    stamps = _state(blocked_for=blocked_for)
    return SimpleNamespace(
        id="row-1", org_id="org-1", pipeline_id="pipe-1", connector_name="conn-1", desired_state=desired, desired_generation=1,
        config_hash="hash-1", observed_state=observed, observed_generation=1, last_reconciled_at=None, last_observed_at=None,
        last_runtime_error=None, **stamps.__dict__,
    )


async def _tick(monkeypatch, row, *, connector_state="RUNNING", pipeline_status="running"):
    statuses: list[str] = []
    pipeline = {"id": "pipe-1", "org_id": "org-1", "domain": "d", "source_system": "s", "entity": "e", "version": "v1",
                "status": pipeline_status, "error_handling_policy": "DLQ_AND_CONTINUE"}

    async def _get_pipeline(org_id, pipeline_id):
        return dict(pipeline)

    async def _set_status(org_id, pipeline_id, status):
        statuses.append(status)

    async def _resolve(p):
        return {"connector.class": "x"}, "hash-1"

    async def _status(client, name):
        return {"connector": {"state": connector_state}, "tasks": [{"state": "RUNNING"}]}

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _get_pipeline)
    monkeypatch.setattr(pipeline_service_module.pipeline_service, "set_status", _set_status)
    monkeypatch.setattr(reconciler_module, "_resolve_config", _resolve)
    monkeypatch.setattr(reconciler_module, "get_status_retryable", _status)
    monkeypatch.setattr(reconciler_module.settings, "quality_stream_degraded_after_seconds", 300.0)
    session = _Session()
    result = await reconciler_module.RuntimeReconciler()._reconcile_row(session, _Repo(session), client=None, existing={"conn-1"}, row=row)
    return statuses, result


@pytest.mark.asyncio
async def test_a_pipeline_whose_quality_worker_is_stuck_past_the_threshold_becomes_degraded(monkeypatch) -> None:
    now = datetime.now(UTC)
    row = _runtime_row(blocked_for=None)
    row.quality_blocked_since, row.quality_blocked_heartbeat_at = now - timedelta(seconds=900), now

    statuses, result = await _tick(monkeypatch, row)

    assert statuses == ["degraded"]
    assert "stuck for 15 min" in row.last_runtime_error
    assert result["observed_state"] == "running"                    # Connect is fine: the connector is NOT restarted over this


@pytest.mark.asyncio
async def test_a_stall_below_the_threshold_leaves_the_pipeline_running(monkeypatch) -> None:
    now = datetime.now(UTC)
    row = _runtime_row(blocked_for=None)
    row.quality_blocked_since, row.quality_blocked_heartbeat_at = now - timedelta(seconds=30), now

    statuses, _ = await _tick(monkeypatch, row)

    assert statuses == []
    assert row.last_runtime_error is None


@pytest.mark.asyncio
async def test_a_degraded_pipeline_recovers_to_running_once_the_stall_clears(monkeypatch) -> None:
    row = _runtime_row(blocked_for=None)

    statuses, _ = await _tick(monkeypatch, row, pipeline_status="degraded")

    assert statuses == ["running"]
    assert row.last_runtime_error is None


@pytest.mark.asyncio
async def test_a_stall_does_not_override_a_paused_or_failed_connector(monkeypatch) -> None:
    now = datetime.now(UTC)
    for connector_state in ("PAUSED", "FAILED"):
        row = _runtime_row(blocked_for=None, desired="running")
        row.quality_blocked_since, row.quality_blocked_heartbeat_at = now - timedelta(seconds=900), now
        statuses, _ = await _tick(monkeypatch, row, connector_state=connector_state)
        assert "degraded" not in statuses, connector_state


@pytest.mark.asyncio
async def test_a_stall_with_a_dead_worker_does_not_degrade_the_pipeline(monkeypatch) -> None:
    now = datetime.now(UTC)
    row = _runtime_row(blocked_for=None)
    row.quality_blocked_since, row.quality_blocked_heartbeat_at = now - timedelta(seconds=3600), now - timedelta(seconds=600)

    statuses, _ = await _tick(monkeypatch, row)

    assert statuses == []
