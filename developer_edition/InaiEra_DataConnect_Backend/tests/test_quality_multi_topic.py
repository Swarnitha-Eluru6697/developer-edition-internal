"""NEW-02: a sink may read several topics, and the quality worker must check ALL of them and say which topic, partition and offset every result
came from.

What was wrong: the worker handed `connector_config["topics"]` — "a,b,c" — to the Kafka consumer as ONE topic name. Kafka rejects that as an
illegal name on every poll; the worker logged it and retried every second, so a sink with two or more input topics had NONE of them
evaluated or quarantined (not just the later ones).
"""
import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord, NativeKafkaConsumer
from app.services.pipeline_topics import consumed_topics, data_topic, input_topics
from app.services.quality import build_quarantine_envelope
from app.services.quality_stream_worker import QualityStreamWorker, _input_topics

ORG = "org_1001"
T1, T2, T3 = (f"raw-ext-feed-{n}-batch-v1" for n in ("alpha", "beta", "gamma"))
TOPICS = [T1, T2, T3]
QUARANTINE = "quarantine-sales-orders_app-orders_copy-validation_sink-v1"
# An ENFORCE sink hands its passing records to its own connector through an internal topic (quality_mode.SINK_HANDOFF_PREFIX).
PREPARED = "_dataconnect.gate.sales-orders_app-orders_copy-batch-v1"
DEAD_LETTER = "quarantine-sales-orders_app-orders_copy-quality_worker-v1"

SINK = {
    "id": "pipe_sink", "org_id": ORG, "direction": "SINK", "zone": "raw", "domain": "sales", "source_system": "orders_app",
    "entity": "orders_copy", "mode": "batch", "version": "v1", "topic_name": None, "schema_version_id": None,
    "connector_config": {"topics": ",".join(TOPICS)}, "quality_policy_id": "dqp_1", "quality_mode": "OBSERVE",
}
POLICY = {
    "id": "dqp_1", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}


def _record(topic: str, partition: int, offset: int, stock: int) -> KafkaRecord:
    envelope = {"schema": {"type": "struct", "fields": []}, "payload": {"id": offset, "stock_quantity": stock, "from": topic}}
    return KafkaRecord(topic=topic, key=f"k-{topic.split('-')[3]}-{offset}", value={"_raw": json.dumps(envelope)}, partition=partition, offset=offset)


def _feed() -> list[KafkaRecord]:
    """Four records per topic, interleaved the way a consumer group delivers them; the LAST one of each topic is invalid (stock 1)."""
    out = []
    for offset, stock in enumerate([50, 60, 70, 1]):
        for partition, topic in enumerate(TOPICS):
            out.append(_record(topic, partition, offset, stock))
    return out


class FakeConsumer:
    def __init__(self, records: list[KafkaRecord]) -> None:
        self.records, self.commits = list(records), 0

    async def poll(self):
        if self.records:
            return self.records.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_max_handle_attempts", 2)
    monkeypatch.setattr("app.services.quality_stream_worker.settings.quality_stream_handle_backoff_seconds", 0.0)


@pytest.fixture
def kafka(monkeypatch):
    box = SimpleNamespace(producer=InMemoryKafkaProducer(), outcomes=[], consumer_args=None)
    box.on = lambda topic: [r for r in box.producer.records if r.topic == topic]

    async def _ensure(topic, **kwargs):
        return None

    async def _producer():
        return box.producer

    async def _record_outcome(self, *, valid, failed_rules, original_record, origin=None):
        box.outcomes.append((valid, origin.topic if origin else None, origin.partition if origin else None, origin.offset if origin else None))

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", _ensure)
    monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", _producer)
    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    return box


async def _run(worker: QualityStreamWorker, records: list[KafkaRecord], kafka, monkeypatch, *, timeout: float = 3.0) -> FakeConsumer:
    consumer = FakeConsumer(records)

    def _factory(**kw):
        kafka.consumer_args = kw
        return consumer

    monkeypatch.setattr("app.services.quality_stream_worker.NativeKafkaConsumer", _factory)
    task = asyncio.create_task(worker._run())
    try:
        for _ in range(int(timeout / 0.01)):
            if consumer.commits == len(records):
                break
            await asyncio.sleep(0.01)
    finally:
        worker._stopping = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return consumer


def _worker(pipeline: dict | None = None, policy: dict | None = None) -> QualityStreamWorker:
    return QualityStreamWorker(org_id=ORG, pipeline=pipeline or SINK, policy=policy or POLICY)


# ── The worker subscribes to every input topic ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_worker_subscribes_to_all_three_input_topics_not_one_comma_joined_name(monkeypatch, kafka) -> None:
    await _run(_worker(), _feed(), kafka, monkeypatch)

    assert kafka.consumer_args["topics"] == TOPICS
    assert "topic" not in kafka.consumer_args                 # never the "a,b,c" string


@pytest.mark.asyncio
async def test_quality_is_evaluated_and_quarantined_on_all_three_topics(monkeypatch, kafka) -> None:
    consumer = await _run(_worker(), _feed(), kafka, monkeypatch)

    assert consumer.commits == 12
    assert len(kafka.outcomes) == 12
    assert sum(1 for valid, *_ in kafka.outcomes if not valid) == 3
    quarantined = kafka.on(QUARANTINE)
    assert len(quarantined) == 3
    assert sorted(r.value["original_topic"] for r in quarantined) == sorted(TOPICS)        # one failure from EACH topic
    assert all(r.value["original_record"]["stock_quantity"] == 1 for r in quarantined)
    assert kafka.on(PREPARED) == []                                                         # OBSERVE: nothing forwarded


@pytest.mark.asyncio
async def test_a_failure_says_which_topic_partition_and_offset_it_was_read_from(monkeypatch, kafka) -> None:
    await _run(_worker(), _feed(), kafka, monkeypatch)

    by_topic = {r.value["original_topic"]: r.value for r in kafka.on(QUARANTINE)}
    for partition, topic in enumerate(TOPICS):
        assert (by_topic[topic]["original_partition"], by_topic[topic]["original_offset"]) == (partition, 3)


@pytest.mark.asyncio
async def test_the_stats_write_knows_each_records_origin(monkeypatch, kafka) -> None:
    await _run(_worker(), _feed(), kafka, monkeypatch)

    assert {(topic, partition, offset) for _, topic, partition, offset in kafka.outcomes} == {
        (topic, partition, offset) for offset in range(4) for partition, topic in enumerate(TOPICS)}


@pytest.mark.asyncio
async def test_a_list_form_topics_setting_is_read_the_same_way(monkeypatch, kafka) -> None:
    pipeline = {**SINK, "connector_config": {"topics": TOPICS}}

    await _run(_worker(pipeline), _feed(), kafka, monkeypatch)

    assert kafka.consumer_args["topics"] == TOPICS
    assert len(kafka.on(QUARANTINE)) == 3


@pytest.mark.asyncio
async def test_a_single_topic_sink_and_a_source_still_read_exactly_their_one_topic(monkeypatch, kafka) -> None:
    one = {**SINK, "connector_config": {"topics": T1}}
    await _run(_worker(one), [_record(T1, 0, 0, 1)], kafka, monkeypatch)
    assert kafka.consumer_args["topics"] == [T1]

    source = {**SINK, "direction": "SOURCE", "topic_name": T2, "connector_config": {}}
    await _run(_worker(source), [_record(T2, 0, 0, 1)], kafka, monkeypatch)
    assert kafka.consumer_args["topics"] == [T2]


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_dead_lettered_with_its_origin(monkeypatch, kafka) -> None:
    bad = KafkaRecord(topic=T3, key="k", value={"_raw": "not json"}, partition=2, offset=41)

    await _run(_worker(), [bad], kafka, monkeypatch)

    (dead,) = kafka.on(DEAD_LETTER)
    assert (dead.value["original_topic"], dead.value["original_partition"], dead.value["original_offset"]) == (T3, 2, 41)


@pytest.mark.asyncio
async def test_a_dead_lettered_failure_keeps_the_topic_it_came_from(monkeypatch, kafka) -> None:
    original = kafka.producer.produce

    async def _quarantine_is_down(**kw):
        if kw["topic"] == QUARANTINE:
            raise RuntimeError("broker not available")
        return await original(**kw)

    kafka.producer.produce = _quarantine_is_down
    await _run(_worker(), [_record(T2, 1, 7, 1)], kafka, monkeypatch)

    (dead,) = kafka.on(DEAD_LETTER)
    assert dead.value["reason"] == "quarantine_produce_failed"
    assert (dead.value["original_topic"], dead.value["original_partition"], dead.value["original_offset"]) == (T2, 1, 7)


@pytest.mark.asyncio
async def test_enforce_forwards_the_passing_records_of_all_three_topics_with_their_origin(monkeypatch, kafka) -> None:
    enforcing = {**SINK, "quality_mode": "ENFORCE"}

    await _run(_worker(enforcing), _feed(), kafka, monkeypatch)

    prepared, quarantined = kafka.on(PREPARED), kafka.on(QUARANTINE)
    assert (len(prepared), len(quarantined)) == (9, 3)
    assert {r.headers["inai.source_topic"] for r in prepared} == set(TOPICS)                 # all three feed the one prepared topic
    for record in prepared:
        sent = json.loads(next(r for r in _feed() if r.key == record.key).value["_raw"])
        assert record.value == sent                                                          # exactly the message that was read
        assert record.headers["inai.source_topic"] == sent["payload"]["from"]
    assert {(r.headers["inai.source_partition"], r.headers["inai.source_offset"]) for r in prepared} == {
        (str(p), str(o)) for p in range(3) for o in range(3)}


# ── Which topics ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("setting", "expected"), [
    (",".join(TOPICS), TOPICS),
    (f" {T1} , {T2},{T3} ", TOPICS),                    # whitespace is not part of a name
    (f"{T1},{T2},{T1}", [T1, T2]),                      # a repeat is read once
    (TOPICS, TOPICS),
    ((T1, T2), [T1, T2]),
    (f"{T1},,{T2},", [T1, T2]),                         # empty pieces are ignored
    (T1, [T1]),
])
def test_every_configured_input_topic_is_consumed_in_order_without_repeats(setting, expected) -> None:
    pipeline = {**SINK, "connector_config": {"topics": setting}}

    assert _input_topics(pipeline) == expected
    assert consumed_topics(pipeline) == expected


def test_a_source_consumes_its_own_topic_and_a_quarantine_zone_pipeline_consumes_nothing() -> None:
    source = {**SINK, "direction": "SOURCE", "topic_name": T1, "connector_config": {}}
    assert _input_topics(source) == [T1]
    assert _input_topics({**source, "zone": "quarantine"}) == []
    assert _input_topics({**SINK, "connector_config": {}}) == []                              # a sink with no input reads nothing


def test_data_topic_is_only_the_first_topic_which_is_why_it_must_not_choose_what_to_consume() -> None:
    assert data_topic(SINK) == T1                       # a LABEL: the first of three
    assert input_topics(SINK) == TOPICS
    assert consumed_topics(SINK) == TOPICS              # what decides coverage


# ── The consumer ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_kafka_client(monkeypatch):
    """Stands in for kafka-python's KafkaConsumer: records how it is built, hands out one record per topic."""
    made: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *topics, **kw):
            made["topics"], made["kw"] = topics, kw

        def poll(self, timeout_ms, max_records):
            rec = SimpleNamespace(topic=T3, partition=2, offset=9, key="k", value=b'{"a": 1}', headers=[("h", b"v")])
            return {("tp",): [rec]}

        def close(self):
            made["closed"] = True

    monkeypatch.setattr("kafka.KafkaConsumer", FakeClient)
    return made


@pytest.mark.asyncio
async def test_the_native_consumer_subscribes_to_every_topic_it_is_given(fake_kafka_client) -> None:
    consumer = NativeKafkaConsumer(bootstrap_servers="b:9092", topics=TOPICS, group_id="g")

    record = await consumer.poll()

    assert fake_kafka_client["topics"] == tuple(TOPICS)                                      # one subscription, three topics
    assert fake_kafka_client["kw"]["group_id"] == "g"
    assert (record.topic, record.partition, record.offset) == (T3, 2, 9)                    # the record says where it came from
    assert consumer.topic == ",".join(TOPICS)                                               # for logs only
    await consumer.close()


@pytest.mark.asyncio
async def test_the_native_consumer_still_takes_a_single_topic(fake_kafka_client) -> None:
    consumer = NativeKafkaConsumer(bootstrap_servers="b:9092", topic=T1, group_id="g")
    await consumer.poll()

    assert fake_kafka_client["topics"] == (T1,)
    await consumer.close()


def test_the_native_consumer_drops_repeated_topics() -> None:
    assert NativeKafkaConsumer(bootstrap_servers="b", topics=[T1, T2, T1], group_id="g").topics == (T1, T2)


@pytest.mark.parametrize("bad", [",".join(TOPICS), f"{T1}, {T2}", "a b", "", "t/1", "x" * 250])
def test_a_comma_joined_or_otherwise_illegal_name_is_refused_up_front_not_retried_forever(bad) -> None:
    with pytest.raises(ValueError, match="single Kafka topic name"):
        NativeKafkaConsumer(bootstrap_servers="b", topic=bad, group_id="g")


def test_a_consumer_with_no_topic_is_refused() -> None:
    for kwargs in ({}, {"topics": []}):
        with pytest.raises(ValueError, match="at least one topic"):
            NativeKafkaConsumer(bootstrap_servers="b", group_id="g", **kwargs)


# ── The envelope ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _envelope(**origin) -> dict[str, Any]:
    return build_quarantine_envelope(
        original_topic=T1, pipeline_id="p", schema_id=None, quality_policy_id="q", failed_rules=["r"], original_record={"a": 1}, **origin)


def test_the_envelope_carries_partition_and_offset_when_they_are_known() -> None:
    env = _envelope(original_partition=3, original_offset=120)

    assert (env["original_topic"], env["original_partition"], env["original_offset"]) == (T1, 3, 120)


def test_partition_zero_and_offset_zero_are_real_values_not_missing() -> None:
    env = _envelope(original_partition=0, original_offset=0)

    assert env["original_partition"] == 0
    assert env["original_offset"] == 0


def test_a_manual_check_with_no_origin_keeps_the_envelope_exactly_as_before() -> None:
    env = _envelope()

    assert "original_partition" not in env
    assert "original_offset" not in env
