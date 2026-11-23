"""NEW-05: what the quality worker does when a record is delivered to it AGAIN after a crash between recording it and committing its Kafka offset.

  * every message it publishes carries a stable identity (`inai.dedupe_key` = <pipeline>:<topic>:<partition>:<offset>, and `event_id` in the payload),
    the same on every delivery of the same source record, so a consumer can discard the repeat;
  * a replay is not counted a second time (the claim itself is tested against a real PostgreSQL in test_quality_stream_offsets_real_db.py; here
    the worker's behaviour around it is tested with a stand-in that has the same contract: True the first time, False on a repeat).
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

from app.core.metrics import SERVICE_NAME
from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality import build_quarantine_envelope
from app.services.quality_stream_worker import QualityStreamWorker, _Origin

ORG = "org_1001"
TOPIC = "raw-ext-feed-alpha-batch-v1"
PIPE = {
    "id": "pipe_sink", "org_id": ORG, "direction": "SINK", "zone": "raw", "domain": "sales", "source_system": "orders_app",
    "entity": "orders_copy", "mode": "batch", "version": "v1", "topic_name": None, "schema_version_id": None,
    "connector_config": {"topics": TOPIC}, "quality_policy_id": "dqp_1", "quality_mode": "ENFORCE",
}
POLICY = {
    "id": "dqp_1", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}
QUARANTINE = "quarantine-sales-orders_app-orders_copy-validation_sink-v1"
# An ENFORCE sink hands its passing records to its own connector through an internal topic (quality_mode.SINK_HANDOFF_PREFIX).
PREPARED = "_dataconnect.gate.sales-orders_app-orders_copy-batch-v1"
DEAD_LETTER = "quarantine-sales-orders_app-orders_copy-quality_worker-v1"
STOCK = [50, 60, 5, 70, 80, 200, 90, 100, 110, 1]            # three invalid: offsets 2, 5, 9


def _records() -> list[KafkaRecord]:
    return [
        KafkaRecord(topic=TOPIC, key=f"k{o}", partition=o % 2, offset=o // 2,
                    value={"_raw": json.dumps({"schema": {"type": "struct", "fields": []}, "payload": {"id": o, "stock_quantity": s}})})
        for o, s in enumerate(STOCK)
    ]


class FakeConsumer:
    """Serves its records, then idles. `die_on_commit` makes the commit of the Nth record raise, standing in for the process dying there."""

    def __init__(self, records, die_on_commit: int | None = None):
        self.records, self.commits, self.die_on_commit = list(records), 0, die_on_commit

    async def poll(self):
        if self.records:
            return self.records.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def commit(self) -> None:
        self.commits += 1
        if self.die_on_commit is not None and self.commits == self.die_on_commit:
            raise RuntimeError("the process died before the Kafka commit")

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_max_handle_attempts", 2)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_handle_backoff_seconds", 0.0)


@pytest.fixture
def world(monkeypatch):
    """One Kafka (in memory) that outlives the worker's "process", and a statistics claim with the contract of the real one."""
    box = SimpleNamespace(producer=InMemoryKafkaProducer(), claimed=set(), counted=[], replays=[])
    box.on = lambda topic: [r for r in box.producer.records if r.topic == topic]

    async def _ensure(topic, **kwargs):
        return None

    async def _producer():
        return box.producer

    async def _record_outcome(self, *, valid, failed_rules, original_record, origin=None):
        identity = (self.pipeline["id"], origin.topic, origin.partition, origin.offset)
        if identity in box.claimed:
            return False
        box.claimed.add(identity)
        box.counted.append(valid)
        return True

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", _ensure)
    monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", _producer)
    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    return box


async def _run(records, monkeypatch, *, die_on_commit: int | None = None, pipeline: dict | None = None, until: int | None = None):
    consumer = FakeConsumer(records, die_on_commit)
    monkeypatch.setattr("app.services.quality_stream_worker.NativeKafkaConsumer", lambda **kw: consumer)
    worker = QualityStreamWorker(org_id=ORG, pipeline=pipeline or PIPE, policy=POLICY)
    task = asyncio.create_task(worker._run())
    target = until if until is not None else len(records)
    for _ in range(300):
        if consumer.commits >= target or task.done():
            break
        await asyncio.sleep(0.01)
    worker._stopping = True
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass
    return consumer


def _replays() -> float:
    return REGISTRY.get_sample_value("dataconnect_quality_replays_skipped_total", {"service": SERVICE_NAME, "connector_type": "sink"}) or 0.0


# ── Identity on what is published ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_quarantine_message_carries_the_source_records_stable_identity(monkeypatch, world) -> None:
    await _run(_records(), monkeypatch)

    quarantined = world.on(QUARANTINE)
    assert len(quarantined) == 3
    for message in quarantined:
        assert message.headers["inai.dedupe_key"] == f"pipe_sink:{message.value['event_id']}"
        topic, partition, offset = message.value["event_id"].rsplit(":", 2)
        assert (topic, int(partition), int(offset)) == (TOPIC, message.value["original_partition"], message.value["original_offset"])
    assert len({m.headers["inai.dedupe_key"] for m in quarantined}) == 3                 # distinct records, distinct identities


@pytest.mark.asyncio
async def test_every_prepared_message_carries_its_identity_too(monkeypatch, world) -> None:
    await _run(_records(), monkeypatch)

    prepared = world.on(PREPARED)
    assert len(prepared) == 7
    assert len({m.headers["inai.dedupe_key"] for m in prepared}) == 7
    for message in prepared:
        assert message.headers["inai.dedupe_key"] == "pipe_sink:" + ":".join(
            [message.headers["inai.source_topic"], message.headers["inai.source_partition"], message.headers["inai.source_offset"]])


@pytest.mark.asyncio
async def test_a_dead_letter_carries_the_identity_of_the_record_it_could_not_process(monkeypatch, world) -> None:
    bad = KafkaRecord(topic=TOPIC, key="k", value={"_raw": "not json"}, partition=1, offset=41)

    await _run([bad], monkeypatch)

    (dead,) = world.on(DEAD_LETTER)
    assert dead.value["event_id"] == f"{TOPIC}:1:41"
    assert dead.headers["inai.dedupe_key"] == f"pipe_sink:{TOPIC}:1:41"


def test_two_pipelines_reading_one_topic_have_distinct_dedupe_keys_for_the_same_source_record() -> None:
    origin = _Origin(topic=TOPIC, partition=3, offset=9)

    assert origin.event_id == f"{TOPIC}:3:9"
    assert origin.dedupe_key("pipe-a") != origin.dedupe_key("pipe-b")
    assert origin.dedupe_key("pipe-a") == origin.dedupe_key("pipe-a")                    # but stable for one pipeline


@pytest.mark.parametrize("origin", [_Origin(topic=TOPIC), _Origin(topic=TOPIC, partition=1), _Origin(topic=TOPIC, offset=1)])
def test_a_record_without_a_kafka_position_has_no_identity(origin) -> None:
    assert origin.event_id is None
    assert origin.dedupe_key("p") is None


def test_partition_zero_and_offset_zero_are_a_real_position() -> None:
    assert _Origin(topic=TOPIC, partition=0, offset=0).event_id == f"{TOPIC}:0:0"


def test_the_envelope_has_an_event_id_only_when_one_is_given() -> None:
    kwargs = {"original_topic": TOPIC, "pipeline_id": "p", "schema_id": None, "quality_policy_id": "q", "failed_rules": [], "original_record": {}}

    assert build_quarantine_envelope(**kwargs, event_id="t:0:0")["event_id"] == "t:0:0"
    assert "event_id" not in build_quarantine_envelope(**kwargs)


# ── The post-publish crash ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_after_a_crash_the_replay_is_recognisable_and_the_statistics_are_stable(monkeypatch, world) -> None:
    """The review's scenario. Everything is recorded and published; the process dies on the Kafka commit of the LAST record; the restarted
    worker is handed ALL ten records again (nothing was committed after the crash)."""
    first = await _run(_records(), monkeypatch, die_on_commit=10)
    counted_after_first = list(world.counted)
    published_first = len(world.producer.records)

    await _run(_records(), monkeypatch)                                                    # the restart

    assert first.commits == 10
    assert (len(counted_after_first), sum(1 for v in counted_after_first if not v)) == (10, 3)
    assert world.counted == counted_after_first                                           # the replay counted NOTHING
    assert len(world.producer.records) == 2 * published_first                             # published again: at-least-once ...
    for topic in (QUARANTINE, PREPARED):
        keys = [m.headers["inai.dedupe_key"] for m in world.on(topic)]
        assert len(keys) == 2 * len(set(keys))                                           # ... each repeat has the SAME key as the original
    assert len({m.headers["inai.dedupe_key"] for m in world.on(QUARANTINE)}) == 3
    assert len({m.headers["inai.dedupe_key"] for m in world.on(PREPARED)}) == 7


@pytest.mark.asyncio
async def test_a_consumer_that_discards_repeated_dedupe_keys_sees_each_record_exactly_once(monkeypatch, world) -> None:
    await _run(_records(), monkeypatch, die_on_commit=4)             # dies early: only 4 offsets were committed
    await _run(_records(), monkeypatch)                              # and the restart replays everything

    for topic, expected in ((QUARANTINE, 3), (PREPARED, 7)):
        seen: dict[str, KafkaRecord] = {}
        for message in world.on(topic):
            seen.setdefault(message.headers["inai.dedupe_key"], message)                  # what a deduplicating consumer does
        assert len(seen) == expected


@pytest.mark.asyncio
async def test_a_replay_is_counted_in_the_metric_not_in_the_statistics(monkeypatch, world) -> None:
    before = _replays()
    await _run(_records(), monkeypatch, die_on_commit=10)
    assert _replays() == before                                                           # a first delivery is not a replay

    await _run(_records(), monkeypatch)

    assert _replays() - before == 10
