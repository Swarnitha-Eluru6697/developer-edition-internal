"""
Hardening: sustained quality-stream failure and recovery.

tests/test_quality_stream_dead_letter.py proves each rule once; tests/test_quality_worker_visibility.py proves one outage is visible. This runs the
worker through MANY records with repeated, deterministic outage/recovery cycles — a broker that goes away three times, one of them long — against a
consumer that models real offsets, and checks the invariants that must hold over the whole run:

  * nothing is committed past a record that has not been handled or durably dead-lettered (checked at the moment of every single failure);
  * no record is lost and none is dead-lettered twice;
  * every offset ends up committed, in order, exactly once;
  * the retry / DLQ-failure counters equal what actually happened; the gauges and the registry return to zero; the stuck flag is cleared;
  * a crash in the middle of a long outage loses nothing: the replacement worker resumes from the committed offset.
"""

import asyncio

import pytest
from prometheus_client import REGISTRY

from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality_stream_worker import QualityStreamWorker
from app.services.quality_worker_health import quality_worker_health

PIPELINE = {
    "id": "pipe_s", "org_id": "org_s", "topic_name": "raw-d-s-e-batch-v1", "domain": "d", "source_system": "s", "entity": "e",
    "version": "v1", "direction": "SINK", "schema_version_id": None, "connector_config": {}, "quality_policy_id": "p",
}
POLICY = {"id": "p", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
          "rules": [{"name": "stock", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}]}
DEAD_LETTER = "quarantine-d-s-e-quality_worker-v1"
VALIDATION = "quarantine-d-s-e-validation_sink-v1"


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, {"service": "dataconnect", **labels}) or 0.0


class OffsetConsumer:
    """A consumer with REAL offset semantics: poll() returns the next record from its position, commit() records that position as committed, and a
    new consumer built from `committed` is redelivered everything not yet committed (at-least-once)."""

    def __init__(self, records: list[KafkaRecord], start: int = 0) -> None:
        self.records, self.position, self.committed = records, start, start
        self.commit_log: list[int] = []
        self.closed = 0

    async def poll(self):
        if self.position < len(self.records):
            record = self.records[self.position]
            self.position += 1
            return record
        await asyncio.sleep(0.005)
        return None

    async def commit(self) -> None:
        self.committed = self.position
        self.commit_log.append(self.position)

    async def close(self) -> None:
        self.closed += 1


class ScheduledBroker:
    """Wraps the in-memory producer; the dead-letter topic is unavailable for the attempts listed in `outages` ((first, last), 1-based,
    counting every attempt to write it). Records the commit count at each failure so a test can prove nothing committed during an outage."""

    def __init__(self, outages: list[tuple[int, int]], consumer_ref: list[OffsetConsumer]) -> None:
        self.producer = InMemoryKafkaProducer()
        self.outages = outages
        self.attempts = 0
        self.failed_attempts = 0
        self.commits_at_failure: list[int] = []
        self.consumer_ref = consumer_ref

    def _down(self) -> bool:
        return any(first <= self.attempts <= last for first, last in self.outages)

    async def produce(self, *, topic, **kwargs):
        if topic == DEAD_LETTER:
            self.attempts += 1
            if self._down():
                self.failed_attempts += 1
                self.commits_at_failure.append(len(self.consumer_ref[-1].commit_log))
                raise ConnectionError("broker unavailable")
        return await self.producer.produce(topic=topic, **kwargs)

    def landed(self, topic: str) -> list[KafkaRecord]:
        return [r for r in self.producer.records if r.topic == topic]


def _records(n: int) -> list[KafkaRecord]:
    import json

    # odd ids break the rule (-> a quarantine copy); the payload carries its id so a record can be traced end to end
    return [KafkaRecord(topic=PIPELINE["topic_name"], key=f"k{i}", value={"_raw": json.dumps({"id": i, "stock_quantity": 5 if i % 2 else 50})}) for i in range(n)]


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_max_handle_attempts", 2)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_handle_backoff_seconds", 0.0)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_dead_letter_backoff_max_seconds", 0.01)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_blocked_heartbeat_seconds", 0.02)
    state = {"stats_calls": 0, "stats_failing": set(), "blocked_writes": 0, "clears": 0, "consumers": [], "broker": None}

    async def ensure(topic, **kwargs):
        return None

    async def record_outcome(self, *, valid, failed_rules, original_record, origin=None):
        state["stats_calls"] += 1
        if original_record["id"] in state["stats_failing"]:
            raise RuntimeError("the stats database rejected this record")

    async def write(self, since, heartbeat_at):
        state["blocked_writes"] += 1

    async def clear(self):
        state["clears"] += 1

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", ensure)
    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", record_outcome)
    monkeypatch.setattr(QualityStreamWorker, "_write_blocked_state", write)
    monkeypatch.setattr(QualityStreamWorker, "_clear_blocked_state", clear)

    def install(broker: ScheduledBroker) -> None:
        async def producer():
            return broker

        monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", producer)
        state["broker"] = broker

    def consumer_factory(records, start=0):
        consumer = OffsetConsumer(records, start)
        state["consumers"].append(consumer)
        monkeypatch.setattr("app.services.quality_stream_worker.NativeKafkaConsumer", lambda **kw: consumer)
        return consumer

    state["install"], state["consumer_factory"] = install, consumer_factory
    return state


async def _run_until(worker: QualityStreamWorker, done, timeout: float = 20.0) -> None:
    worker.start()
    for _ in range(int(timeout / 0.01)):
        if done():
            break
        await asyncio.sleep(0.01)
    await worker.stop()


def _worker() -> QualityStreamWorker:
    return QualityStreamWorker(org_id="org_s", pipeline=PIPELINE, policy=POLICY)


@pytest.mark.asyncio
async def test_many_records_through_repeated_outages_lose_nothing_commit_nothing_early_and_end_clean(harness) -> None:
    total = 42
    records = _records(total)
    holder: list[OffsetConsumer] = []
    broker = ScheduledBroker(outages=[(1, 5), (9, 10), (14, 30)], consumer_ref=holder)   # three outages, the last one long
    harness["install"](broker)
    harness["stats_failing"].update(range(0, total, 6))                                  # these can never be recorded -> must be dead-lettered
    consumer = harness["consumer_factory"](records)
    holder.append(consumer)
    dlq_before = _sample("dataconnect_quality_dlq_failures_total", connector_type="sink")
    retry_before = _sample("dataconnect_quality_retries_total", connector_type="sink", operation="stats")

    await _run_until(_worker(), lambda: consumer.committed == total)

    must_dead_letter = sorted(harness["stats_failing"])
    # every offset committed, in order, exactly once
    assert consumer.committed == total
    assert consumer.commit_log == list(range(1, total + 1))
    # no record lost, none dead-lettered twice, original payload intact
    landed = [r.value["original_record"]["id"] for r in broker.landed(DEAD_LETTER)]
    assert sorted(landed) == must_dead_letter
    assert len(landed) == len(set(landed))
    # a record that breaks the rule AND could not be recorded is dead-lettered, not quarantined (its stats were never counted)
    quarantined = {r.value["original_record"]["id"] for r in broker.landed(VALIDATION)} if broker.landed(VALIDATION) else set()
    assert quarantined == {i for i in range(total) if i % 2 and i not in harness["stats_failing"]}
    # NOTHING was committed during an outage. Record i is the i-th (0-based), so while it is stuck exactly i records have been committed: every
    # failed attempt must have seen a commit count equal to the id of a record that could not be recorded — never one more.
    assert broker.failed_attempts == 5 + 2 + 17
    assert set(broker.commits_at_failure) <= harness["stats_failing"]
    assert broker.commits_at_failure == sorted(broker.commits_at_failure)               # and it only ever moves forward, between stuck records
    # the counters equal what really happened
    assert _sample("dataconnect_quality_dlq_failures_total", connector_type="sink") - dlq_before == broker.failed_attempts
    assert _sample("dataconnect_quality_retries_total", connector_type="sink", operation="stats") - retry_before == len(must_dead_letter)
    # the gauges and the registry are clean; the stuck flag was cleared once per stuck record
    assert quality_worker_health.blocked_partitions() == 0
    assert quality_worker_health.oldest_uncommitted_age() == 0
    assert harness["blocked_writes"] >= 1
    assert harness["clears"] == len(must_dead_letter)
    assert consumer.closed == 1


@pytest.mark.asyncio
async def test_a_commit_never_happens_while_the_record_it_would_cover_is_still_stuck(harness) -> None:
    """The invariant stated directly: with the dead-letter topic down for a long stretch on ONE record, the committed offset does not move."""
    records = _records(10)
    holder: list[OffsetConsumer] = []
    broker = ScheduledBroker(outages=[(1, 10_000)], consumer_ref=holder)               # never recovers
    harness["install"](broker)
    harness["stats_failing"].add(4)                                                    # records 0-3 are fine; record 4 gets stuck
    consumer = harness["consumer_factory"](records)
    holder.append(consumer)

    await _run_until(_worker(), lambda: broker.failed_attempts >= 8, timeout=10)

    assert consumer.committed == 4                                                     # 0-3 handled and committed; 4 is NOT
    assert consumer.commit_log == [1, 2, 3, 4]
    assert set(broker.commits_at_failure) == {4}                                       # every one of the failed attempts saw exactly 4 commits
    assert consumer.position == 5                                                      # nothing beyond the stuck record was even looked at
    assert broker.landed(DEAD_LETTER) == []
    assert quality_worker_health.blocked_partitions() == 0                              # stopped worker: nothing left registered
    assert harness["clears"] >= 1                                                       # and its stuck flag was cleared on stop


@pytest.mark.asyncio
async def test_a_crash_in_the_middle_of_a_long_outage_loses_nothing_the_replacement_resumes_from_the_committed_offset(harness) -> None:
    total = 20
    records = _records(total)
    harness["stats_failing"].add(7)
    # worker A: the dead-letter topic is down for good; it gets stuck on record 7 and is then killed
    holder_a: list[OffsetConsumer] = []
    broker_a = ScheduledBroker(outages=[(1, 10_000)], consumer_ref=holder_a)
    harness["install"](broker_a)
    consumer_a = harness["consumer_factory"](records)
    holder_a.append(consumer_a)
    await _run_until(_worker(), lambda: broker_a.failed_attempts >= 4, timeout=10)
    committed_at_crash = consumer_a.committed
    assert committed_at_crash == 7
    assert broker_a.landed(DEAD_LETTER) == []

    # worker B: a healthy broker, and a consumer that resumes from the COMMITTED offset (the stuck record is redelivered)
    holder_b: list[OffsetConsumer] = []
    broker_b = ScheduledBroker(outages=[], consumer_ref=holder_b)
    harness["install"](broker_b)
    consumer_b = harness["consumer_factory"](records, start=committed_at_crash)
    holder_b.append(consumer_b)
    await _run_until(_worker(), lambda: consumer_b.committed == total)

    assert consumer_b.committed == total
    assert consumer_b.commit_log == list(range(committed_at_crash + 1, total + 1))   # exactly the records A never finished
    dead = [r.value["original_record"]["id"] for r in broker_b.landed(DEAD_LETTER)]
    assert dead == [7]                                                               # the stuck record was dead-lettered once, by B
    handled_by_a = set(range(7))
    quarantined_by_b = {r.value["original_record"]["id"] for r in broker_b.landed(VALIDATION)}
    assert quarantined_by_b == {i for i in range(7, total) if i % 2 and i != 7}      # nothing handled twice, nothing skipped
    assert quarantined_by_b.isdisjoint(handled_by_a)


@pytest.mark.asyncio
async def test_transient_stats_failures_that_recover_within_the_retries_never_reach_the_dead_letter_topic(harness) -> None:
    records = _records(12)
    holder: list[OffsetConsumer] = []
    broker = ScheduledBroker(outages=[], consumer_ref=holder)
    harness["install"](broker)
    consumer = harness["consumer_factory"](records)
    holder.append(consumer)
    seen: dict[int, int] = {}

    original = QualityStreamWorker._record_outcome

    async def flaky(self, *, valid, failed_rules, original_record, origin=None):
        n = seen[original_record["id"]] = seen.get(original_record["id"], 0) + 1
        if original_record["id"] % 3 == 0 and n == 1:
            raise RuntimeError("blip")                                               # fails once, succeeds on the retry
        return await original(self, valid=valid, failed_rules=failed_rules, original_record=original_record)

    QualityStreamWorker._record_outcome = flaky
    retry_before = _sample("dataconnect_quality_retries_total", connector_type="sink", operation="stats")
    try:
        await _run_until(_worker(), lambda: consumer.committed == 12)
    finally:
        QualityStreamWorker._record_outcome = original

    assert consumer.commit_log == list(range(1, 13))
    assert broker.landed(DEAD_LETTER) == []
    assert _sample("dataconnect_quality_retries_total", connector_type="sink", operation="stats") - retry_before == 4   # ids 0,3,6,9
    assert all(count == (2 if i % 3 == 0 else 1) for i, count in seen.items())                                          # no record counted twice
