"""BUG-03: the quality stream worker commits a record's Kafka offset ONLY once the record has been
handled or safely dead-lettered. Before, it logged a failure and committed anyway, so a record whose
stats write or quarantine copy kept failing simply vanished from the quality picture.

    handled -> commit | step fails -> retry, no commit | retries exhausted -> acked dead letter
    -> commit | dead-letter topic down -> never commit
"""
import asyncio
import json

import pytest

from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality_stream_worker import QualityStreamWorker
from app.services.topic_naming import build_worker_quarantine_topic_name

PIPELINE = {
    "id": "pipe_1", "org_id": "org_1001", "topic_name": "raw-db-postgres-products-batch-v1",
    "domain": "db", "source_system": "postgres", "entity": "products", "version": "v1",
    "schema_version_id": None, "connector_config": {}, "quality_policy_id": "dqp_1",
}
POLICY = {
    "id": "dqp_1", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}
VALIDATION_TOPIC = "quarantine-db-postgres-products-validation-v1"
DEAD_LETTER_TOPIC = "quarantine-db-postgres-products-quality_worker-v1"


def _record(payload) -> KafkaRecord:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return KafkaRecord(topic=PIPELINE["topic_name"], key="k1", value={"_raw": raw})


class FakeConsumer:
    """Hands out the queued records, then idles; counts commits so a test can see WHEN they happen."""

    def __init__(self, records: list[KafkaRecord]) -> None:
        self.records = list(records)
        self.commits = 0
        self.closed = False

    async def poll(self):
        if self.records:
            return self.records.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_max_handle_attempts", 2)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_handle_backoff_seconds", 0.0)


@pytest.fixture
def kafka(monkeypatch):
    class Box:
        producer = InMemoryKafkaProducer()
        ensured: list[str] = []

    box = Box()
    box.producer = InMemoryKafkaProducer()
    box.ensured = []

    async def _ensure(topic, **kwargs):
        box.ensured.append(topic)

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
            raise RuntimeError("postgres://svc:hunter2@db.internal:5432/x is down")

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    return box


async def _run_until(worker: QualityStreamWorker, consumer: FakeConsumer, monkeypatch, *, until, timeout=3.0):
    """Runs the real _run loop against the fake consumer until `until()` holds, then stops it."""
    monkeypatch.setattr("app.services.quality_stream_worker.NativeKafkaConsumer", lambda **kw: consumer)
    task = asyncio.create_task(worker._run())
    try:
        for _ in range(int(timeout / 0.01)):
            if until():
                break
            await asyncio.sleep(0.01)
    finally:
        worker._stopping = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _worker() -> QualityStreamWorker:
    return QualityStreamWorker(org_id="org_1001", pipeline=PIPELINE, policy=POLICY)


def test_the_worker_dead_letter_topic_is_its_own_third_topic() -> None:
    # Every derived topic uses the same "-" separator as the pipeline's own.
    topic = build_worker_quarantine_topic_name(
        domain="db", source_system="postgres", entity="products", version="v1",
    )
    assert topic == DEAD_LETTER_TOPIC
    assert topic not in (VALIDATION_TOPIC, "quarantine-db-postgres-products-connector-v1")


@pytest.mark.asyncio
async def test_a_handled_record_is_committed_and_nothing_is_dead_lettered(monkeypatch, kafka, stats) -> None:
    consumer = FakeConsumer([_record({"stock_quantity": 50})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert consumer.commits == 1
    assert kafka.producer.records == []


@pytest.mark.asyncio
async def test_a_transient_stats_failure_is_retried_and_the_record_is_not_lost_or_dead_lettered(monkeypatch, kafka, stats) -> None:
    stats.fail_times = 1
    consumer = FakeConsumer([_record({"stock_quantity": 50})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert stats.calls == 2  # failed once, succeeded on the retry
    assert consumer.commits == 1
    assert kafka.producer.records == []


@pytest.mark.asyncio
async def test_exhausted_stats_retries_dead_letter_the_record_before_committing(monkeypatch, kafka, stats) -> None:
    stats.fail_times = 99
    commits_when_dead_lettered = []
    consumer = FakeConsumer([_record({"stock_quantity": 50, "sku": "A"})])
    original_produce = kafka.producer.produce

    async def _spy(**kw):
        commits_when_dead_lettered.append(consumer.commits)  # must still be 0: acked FIRST, committed after
        return await original_produce(**kw)

    kafka.producer.produce = _spy
    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert commits_when_dead_lettered == [0]
    assert consumer.commits == 1
    (sent,) = kafka.producer.records
    assert sent.topic == DEAD_LETTER_TOPIC
    assert sent.key == "k1"
    assert sent.value["failure_type"] == "quality_worker_failed"
    assert sent.value["reason"] == "stats_write_failed"
    assert sent.value["stats_recorded"] is False
    assert sent.value["original_record"] == {"stock_quantity": 50, "sku": "A"}
    assert "hunter2" not in json.dumps(sent.value)  # the error text is sanitized, not raw
    assert DEAD_LETTER_TOPIC in kafka.ensured


@pytest.mark.asyncio
async def test_a_failed_quarantine_copy_is_dead_lettered_and_the_stats_are_counted_exactly_once(monkeypatch, kafka, stats) -> None:
    class FailingValidationProducer(InMemoryKafkaProducer):
        async def produce(self, *, topic, **kw):
            if topic == VALIDATION_TOPIC:
                raise ConnectionError("broker rejected the quarantine write")
            return await super().produce(topic=topic, **kw)

    kafka.producer = FailingValidationProducer()
    consumer = FakeConsumer([_record({"stock_quantity": 5})])  # fails the rule -> wants a quarantine copy

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert stats.calls == 1  # the quarantine retries did NOT repeat the stats write
    (sent,) = kafka.producer.records
    assert sent.topic == DEAD_LETTER_TOPIC
    assert sent.value["reason"] == "quarantine_produce_failed"
    assert sent.value["stats_recorded"] is True  # a replay must not count it again
    assert sent.value["failed_rules"] == ["stock_range"]
    assert consumer.commits == 1


@pytest.mark.asyncio
async def test_an_unreadable_record_is_dead_lettered_with_its_raw_bytes_not_dropped(monkeypatch, kafka, stats) -> None:
    consumer = FakeConsumer([_record("this is not json {")])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    (sent,) = kafka.producer.records
    assert sent.topic == DEAD_LETTER_TOPIC
    assert sent.value["reason"] == "undecodable_record"
    assert sent.value["original_record"] == "this is not json {"
    assert stats.calls == 0
    assert consumer.commits == 1


@pytest.mark.asyncio
async def test_an_evaluation_error_is_dead_lettered(monkeypatch, kafka, stats) -> None:
    def _boom(payload, policy):
        raise ValueError("rule engine blew up")

    monkeypatch.setattr("app.services.quality_stream_worker.evaluate_record", _boom)
    consumer = FakeConsumer([_record({"stock_quantity": 50})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    (sent,) = kafka.producer.records
    assert sent.value["reason"] == "evaluation_error"
    assert consumer.commits == 1


@pytest.mark.asyncio
async def test_if_the_dead_letter_topic_is_down_the_offset_is_never_committed_until_it_recovers(monkeypatch, kafka, stats) -> None:
    stats.fail_times = 99
    outage = {"down": True, "attempts": 0}

    class FlakyDeadLetterProducer(InMemoryKafkaProducer):
        async def produce(self, **kw):
            outage["attempts"] += 1
            if outage["down"]:
                raise ConnectionError("brokers unreachable")
            return await super().produce(**kw)

    kafka.producer = FlakyDeadLetterProducer()
    consumer = FakeConsumer([_record({"stock_quantity": 50}), _record({"stock_quantity": 60})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: outage["attempts"] >= 4)
    # While the dead-letter topic was unreachable: retried again and again, committed NOTHING, and the
    # second record behind it was not even polled (the partition stalls instead of dropping data).
    assert outage["attempts"] >= 4
    assert consumer.commits == 0
    assert len(consumer.records) == 1
    assert kafka.producer.records == []

    # It comes back: the very same record is now dead-lettered, THEN committed.
    outage["down"] = False
    consumer2 = FakeConsumer([_record({"stock_quantity": 50})])
    await _run_until(_worker(), consumer2, monkeypatch, until=lambda: consumer2.commits == 1)
    assert consumer2.commits == 1
    assert [r.topic for r in kafka.producer.records] == [DEAD_LETTER_TOPIC]


@pytest.mark.asyncio
async def test_shutting_down_mid_record_does_not_commit_it(monkeypatch, kafka, stats) -> None:
    started = asyncio.Event()

    async def _hang(self, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _hang)
    consumer = FakeConsumer([_record({"stock_quantity": 50})])

    await _run_until(_worker(), consumer, monkeypatch, until=lambda: started.is_set())

    assert consumer.commits == 0  # cancelled mid-record: redelivered after restart, not lost
    assert consumer.closed is True
