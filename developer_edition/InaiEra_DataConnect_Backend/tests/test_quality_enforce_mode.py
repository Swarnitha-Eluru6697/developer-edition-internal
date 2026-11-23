"""NEW-01: quality is applied in a stated mode — OBSERVE (count, copy failures to quarantine, the data topic unchanged) or ENFORCE (passing
records to the prepared topic, failures to quarantine) — and ENFORCE is only accepted where the whole chain can honour it.

The headline check: 10 records, 3 invalid. ENFORCE -> prepared 7, quarantine 3, and nothing written to the data topic. OBSERVE -> prepared 0,
quarantine 3, and the data topic still carries all 10 (the worker never touches it).
"""
import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

import app.services.pipeline_spec_validator as validator
from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.connect_config import _sink_topic
from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality_mode import (
    ENFORCE,
    OBSERVE,
    build_prepared_topic,
    enforcement_problem,
    prepared_topic_for,
)
from app.services.quality_stream_worker import QualityStreamWorker
from app.services.topic_naming import to_prepared_topic

DATA_TOPIC = "raw-db-postgres-products-batch-v1"
PREPARED_TOPIC = "prepared-db-postgres-products-batch-v1"
VALIDATION_TOPIC = "quarantine-db-postgres-products-validation-v1"
DEAD_LETTER_TOPIC = "quarantine-db-postgres-products-quality_worker-v1"

SOURCE = {
    "id": "pipe_1", "org_id": "org_1001", "direction": "SOURCE", "topic_name": DATA_TOPIC, "zone": "raw", "domain": "db",
    "source_system": "postgres", "entity": "products", "mode": "batch", "version": "v1", "schema_version_id": None,
    "connector_config": {}, "quality_policy_id": "dqp_1", "quality_mode": ENFORCE,
}
POLICY = {
    "id": "dqp_1", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}
# Ten records, three of them outside 20..150.
STOCK = [50, 60, 5, 70, 80, 200, 90, 100, 110, 1]
INVALID = {5, 200, 1}


def _envelope(stock: int, i: int) -> dict[str, Any]:
    return {"schema": {"type": "struct", "fields": []}, "payload": {"id": i, "stock_quantity": stock}}


def _kafka_record(envelope: dict[str, Any]) -> KafkaRecord:
    return KafkaRecord(topic=DATA_TOPIC, key="k1", value={"_raw": json.dumps(envelope)})


class FakeConsumer:
    def __init__(self, records: list[KafkaRecord]) -> None:
        self.records = list(records)
        self.commits = 0

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
    class Box:
        producer = InMemoryKafkaProducer()
        ensured: list[str] = []

        def on(self, topic: str) -> list[KafkaRecord]:
            return [r for r in self.producer.records if r.topic == topic]

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
    class Stats:
        outcomes: list[bool] = []

    box = Stats()
    box.outcomes = []

    async def _record_outcome(self, *, valid, **kwargs):
        box.outcomes.append(valid)

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    return box


async def _run_until(worker: QualityStreamWorker, consumer: FakeConsumer, monkeypatch, *, until, timeout=3.0):
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


def _worker(mode: str = ENFORCE, policy: dict | None = None) -> QualityStreamWorker:
    return QualityStreamWorker(org_id="org_1001", pipeline={**SOURCE, "quality_mode": mode}, policy=policy or POLICY)


def _ten_records() -> tuple[list[dict[str, Any]], FakeConsumer]:
    envelopes = [_envelope(stock, i) for i, stock in enumerate(STOCK)]
    return envelopes, FakeConsumer([_kafka_record(e) for e in envelopes])


# ── The worker ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enforce_ten_records_three_invalid_gives_seven_prepared_and_three_quarantined(monkeypatch, kafka, stats) -> None:
    envelopes, consumer = _ten_records()

    await _run_until(_worker(ENFORCE), consumer, monkeypatch, until=lambda: consumer.commits == 10)

    prepared, quarantined = kafka.on(PREPARED_TOPIC), kafka.on(VALIDATION_TOPIC)
    assert (len(prepared), len(quarantined)) == (7, 3)
    assert [r.value["payload"]["stock_quantity"] for r in prepared] == [s for s in STOCK if s not in INVALID]
    assert {r.value["original_record"]["stock_quantity"] for r in quarantined} == INVALID
    # A record is in exactly one place: nothing that failed was forwarded, nothing that passed was quarantined.
    assert not {r.value["payload"]["id"] for r in prepared} & {r.value["original_record"]["id"] for r in quarantined}
    assert kafka.on(DATA_TOPIC) == []                       # the worker never writes to the data topic
    assert stats.outcomes.count(True) == 7
    assert stats.outcomes.count(False) == 3
    assert consumer.commits == 10


@pytest.mark.asyncio
async def test_a_forwarded_record_is_exactly_what_connect_wrote_with_its_key(monkeypatch, kafka, stats) -> None:
    envelopes, consumer = _ten_records()

    await _run_until(_worker(ENFORCE), consumer, monkeypatch, until=lambda: consumer.commits == 10)

    first = kafka.on(PREPARED_TOPIC)[0]
    assert first.value == envelopes[0]                      # the Connect envelope (schema + payload), not an unwrapped copy
    assert first.key == "k1"
    assert first.headers == {"inai.pipeline_id": "pipe_1", "inai.quality_mode": "ENFORCE", "inai.source_topic": DATA_TOPIC}


@pytest.mark.asyncio
async def test_observe_leaves_the_data_topic_alone_and_forwards_nothing(monkeypatch, kafka, stats) -> None:
    envelopes, consumer = _ten_records()

    await _run_until(_worker(OBSERVE), consumer, monkeypatch, until=lambda: consumer.commits == 10)

    assert kafka.on(PREPARED_TOPIC) == []                   # no prepared output
    assert len(kafka.on(VALIDATION_TOPIC)) == 3             # failures are still copied to quarantine
    assert kafka.on(DATA_TOPIC) == []                       # the worker writes nothing to the data topic, so all 10 stay there
    assert kafka.ensured == [VALIDATION_TOPIC]              # and does not even create a prepared topic
    assert stats.outcomes.count(True) == 7
    assert stats.outcomes.count(False) == 3


@pytest.mark.asyncio
async def test_a_pipeline_without_a_mode_is_observe(monkeypatch, kafka, stats) -> None:
    """A pipeline dict from before the column existed has no quality_mode: it must behave exactly as it always did."""
    legacy = {k: v for k, v in SOURCE.items() if k != "quality_mode"}
    envelopes, consumer = _ten_records()

    await _run_until(QualityStreamWorker(org_id="org_1001", pipeline=legacy, policy=POLICY), consumer, monkeypatch, until=lambda: consumer.commits == 10)

    assert kafka.on(PREPARED_TOPIC) == []
    assert len(kafka.on(VALIDATION_TOPIC)) == 3


@pytest.mark.asyncio
async def test_enforce_creates_the_prepared_topic_once_before_the_first_write(monkeypatch, kafka, stats) -> None:
    envelopes, consumer = _ten_records()
    timeline: list[tuple[str, str]] = []
    original = kafka.producer.produce

    async def _ensure(topic, **kwargs):
        timeline.append(("ensure", topic))

    async def _produce(**kw):
        timeline.append(("produce", kw["topic"]))
        return await original(**kw)

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", _ensure)
    kafka.producer.produce = _produce
    await _run_until(_worker(ENFORCE), consumer, monkeypatch, until=lambda: consumer.commits == 10)

    assert timeline.count(("ensure", PREPARED_TOPIC)) == 1                       # once, not once per record
    assert timeline.index(("ensure", PREPARED_TOPIC)) < timeline.index(("produce", PREPARED_TOPIC))   # and before the first write


@pytest.mark.asyncio
async def test_enforce_still_quarantines_when_the_policy_is_later_edited_to_not_route_failures(monkeypatch, kafka, stats) -> None:
    """The only alternatives to quarantining a failing record in ENFORCE are dropping it or forwarding it; both break the guarantee."""
    envelopes, consumer = _ten_records()

    await _run_until(_worker(ENFORCE, {**POLICY, "on_failure": "log"}), consumer, monkeypatch, until=lambda: consumer.commits == 10)

    assert (len(kafka.on(PREPARED_TOPIC)), len(kafka.on(VALIDATION_TOPIC))) == (7, 3)


@pytest.mark.asyncio
async def test_a_transient_prepared_failure_is_retried_without_double_counting(monkeypatch, kafka, stats) -> None:
    envelopes = [_envelope(50, 0)]
    consumer = FakeConsumer([_kafka_record(e) for e in envelopes])
    original, failures = kafka.producer.produce, []

    async def _flaky(**kw):
        if kw["topic"] == PREPARED_TOPIC and not failures:
            failures.append(1)
            raise RuntimeError("broker not available")
        return await original(**kw)

    kafka.producer.produce = _flaky
    await _run_until(_worker(ENFORCE), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert len(kafka.on(PREPARED_TOPIC)) == 1               # delivered once, after the retry
    assert kafka.on(DEAD_LETTER_TOPIC) == []
    assert stats.outcomes == [True]                          # counted once, not once per attempt


@pytest.mark.asyncio
async def test_an_unforwardable_record_is_dead_lettered_with_the_full_message_before_the_commit(monkeypatch, kafka, stats) -> None:
    envelope = _envelope(50, 0)
    consumer = FakeConsumer([_kafka_record(envelope)])
    original, commits_at_dead_letter = kafka.producer.produce, []

    async def _prepared_is_down(**kw):
        if kw["topic"] == PREPARED_TOPIC:
            raise RuntimeError("broker not available")
        if kw["topic"] == DEAD_LETTER_TOPIC:
            commits_at_dead_letter.append(consumer.commits)   # the dead letter must be acknowledged BEFORE the commit
        return await original(**kw)

    kafka.producer.produce = _prepared_is_down
    await _run_until(_worker(ENFORCE), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert kafka.on(PREPARED_TOPIC) == []
    (dead,) = kafka.on(DEAD_LETTER_TOPIC)
    assert dead.value["reason"] == "prepared_produce_failed"
    assert dead.value["stats_recorded"] is True              # already counted: a replay must forward it, not count it again
    assert dead.value["original_record"] == envelope         # the whole Connect message, so it can be replayed as it was
    assert commits_at_dead_letter == [0]
    assert consumer.commits == 1
    assert kafka.on(VALIDATION_TOPIC) == []                  # a passing record is never quarantined


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_read_is_never_forwarded_to_prepared(monkeypatch, kafka, stats) -> None:
    consumer = FakeConsumer([KafkaRecord(topic=DATA_TOPIC, key="k1", value={"_raw": "not json"})])

    await _run_until(_worker(ENFORCE), consumer, monkeypatch, until=lambda: consumer.commits == 1)

    assert kafka.on(PREPARED_TOPIC) == []
    assert [r.value["reason"] for r in kafka.on(DEAD_LETTER_TOPIC)] == ["undecodable_record"]


# ── The prepared topic ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_prepared_topic_is_the_pipelines_own_name_with_the_prepared_zone() -> None:
    assert build_prepared_topic(SOURCE) == PREPARED_TOPIC
    assert prepared_topic_for(SOURCE) == PREPARED_TOPIC
    assert prepared_topic_for({**SOURCE, "quality_mode": OBSERVE}) is None


def test_for_a_raw_source_it_matches_the_zone_swap_the_rest_of_the_platform_already_uses() -> None:
    assert build_prepared_topic(SOURCE) == to_prepared_topic(DATA_TOPIC)


# ── Which pipelines may be ENFORCE ─────────────────────────────────────────────────────────────────────────────────────

SINK = {
    **SOURCE, "id": "pipe_sink", "direction": "SINK", "topic_name": None, "entity": "products_copy",
    "connector_config": {"topics": DATA_TOPIC},
}


def test_observe_is_never_a_problem() -> None:
    assert enforcement_problem({**SOURCE, "quality_mode": OBSERVE, "quality_policy_id": None}, None, DATA_TOPIC) is None


def test_a_valid_source_and_a_valid_sink_can_enforce() -> None:
    assert enforcement_problem(SOURCE, POLICY, DATA_TOPIC) is None
    assert enforcement_problem(SINK, POLICY, None) is None


@pytest.mark.parametrize(("pipeline", "policy", "fragment"), [
    ({**SOURCE, "quality_policy_id": None}, None, "needs a quality policy"),
    (SOURCE, {**POLICY, "on_failure": "log"}, "route failures to quarantine"),
    (SOURCE, {**POLICY, "on_failure": "block"}, "route failures to quarantine"),
    (SOURCE, {**POLICY, "quarantine_sink": "object_storage"}, "quarantined to Kafka"),
    ({**SOURCE, "zone": "quarantine"}, POLICY, "not checked by the quality worker"),
], ids=["no-policy", "log", "block", "other-sink", "quarantine-zone"])
def test_what_cannot_be_enforced_is_refused_with_a_reason(pipeline, policy, fragment) -> None:
    assert fragment in (enforcement_problem(pipeline, policy, DATA_TOPIC) or "")


def test_a_source_whose_own_topic_is_its_prepared_topic_cannot_enforce() -> None:
    prepared_zone = {**SOURCE, "zone": "prepared", "topic_name": PREPARED_TOPIC}
    assert "own data topic" in (enforcement_problem(prepared_zone, POLICY, PREPARED_TOPIC) or "")


def test_a_sink_that_reads_its_own_prepared_topic_cannot_enforce() -> None:
    sink = {**SINK, "connector_config": {"topics": build_prepared_topic(SINK)}}   # its own hand-off topic as its input
    assert "input topic" in (enforcement_problem(sink, POLICY, None) or "")


# ── A sink must not bypass the gate ────────────────────────────────────────────────────────────────────────────────────

def _payload(**over) -> PipelineCreate:
    base = {
        "name": "p", "template_id": "t", "connection_profile_id": "c", "zone": "raw", "domain": "db", "source_system": "postgres",
        "entity": "products", "mode": "batch", "version": "v1", "quality_policy_id": "dqp_1", "quality_mode": ENFORCE,
    }
    return PipelineCreate(**{**base, **over})


@pytest.fixture
def existing(monkeypatch):
    """The org's other pipelines, as pipeline_service.list_pipelines would return them."""
    rows: list[dict[str, Any]] = []

    async def _list(org_id):
        return rows

    monkeypatch.setattr("app.services.pipeline_service.pipeline_service.list_pipelines", _list)
    return rows


@pytest.mark.asyncio
async def test_a_sink_cannot_read_the_data_topic_of_an_enforce_source(existing) -> None:
    existing.append({**SOURCE, "name": "Products source", "status": "running"})
    sink = _payload(entity="products_copy", quality_mode=OBSERVE, quality_policy_id=None, connector_config={"topics": DATA_TOPIC})

    with pytest.raises(ValidationError) as exc:
        await validator._validate_quality_mode("org_1001", sink, "SINK", None)

    assert "ENFORCE" in str(exc.value)
    assert PREPARED_TOPIC in str(exc.value)
    assert exc.value.field_errors[0].field == "quality_mode"


@pytest.mark.asyncio
async def test_a_sink_may_read_the_prepared_topic_of_an_enforce_source(existing) -> None:
    existing.append({**SOURCE, "name": "Products source", "status": "running"})
    sink = _payload(entity="products_copy", quality_mode=OBSERVE, quality_policy_id=None, connector_config={"topics": PREPARED_TOPIC})

    await validator._validate_quality_mode("org_1001", sink, "SINK", None)


@pytest.mark.asyncio
async def test_a_sink_may_read_the_data_topic_of_an_observe_source(existing) -> None:
    existing.append({**SOURCE, "name": "Products source", "status": "running", "quality_mode": OBSERVE})
    sink = _payload(entity="products_copy", quality_mode=OBSERVE, quality_policy_id=None, connector_config={"topics": DATA_TOPIC})

    await validator._validate_quality_mode("org_1001", sink, "SINK", None)


@pytest.mark.asyncio
async def test_a_source_cannot_switch_to_enforce_while_a_sink_still_reads_its_data_topic(existing) -> None:
    existing.append({**SINK, "name": "Products copy", "status": "running", "quality_mode": OBSERVE})

    with pytest.raises(ValidationError) as exc:
        await validator._validate_quality_mode("org_1001", _payload(), "SOURCE", POLICY)

    assert "Products copy" in str(exc.value)
    assert PREPARED_TOPIC in str(exc.value)


@pytest.mark.asyncio
async def test_a_source_can_enforce_once_its_sinks_read_the_prepared_topic(existing) -> None:
    existing.append({**SINK, "name": "Products copy", "status": "running", "quality_mode": OBSERVE,
                     "connector_config": {"topics": PREPARED_TOPIC}})

    await validator._validate_quality_mode("org_1001", _payload(), "SOURCE", POLICY)


@pytest.mark.asyncio
async def test_a_pipeline_being_deleted_does_not_block_enforcement(existing) -> None:
    existing.append({**SINK, "name": "Old copy", "status": "deleting", "quality_mode": OBSERVE})

    await validator._validate_quality_mode("org_1001", _payload(), "SOURCE", POLICY)


@pytest.mark.asyncio
async def test_enforce_without_a_policy_is_refused_up_front(existing) -> None:
    with pytest.raises(ValidationError):
        await validator._validate_quality_mode("org_1001", _payload(quality_policy_id=None), "SOURCE", None)


# ── What a sink connector reads ────────────────────────────────────────────────────────────────────────────────────────

def test_an_enforce_sink_connector_reads_the_prepared_topic_not_the_one_the_worker_consumes() -> None:
    assert _sink_topic(SINK) == build_prepared_topic(SINK) != DATA_TOPIC


def test_an_observe_sink_connector_reads_exactly_the_topic_it_was_given() -> None:
    assert _sink_topic({**SINK, "quality_mode": OBSERVE}) == DATA_TOPIC


def test_an_enforce_sink_still_cannot_be_pointed_at_something_that_is_not_a_topic_name_the_platform_builds() -> None:
    with pytest.raises(ValueError, match="INVALID_TOPIC_NAME"):
        _sink_topic({**SINK, "connector_config": {"topics": "not.a.platform.topic"}})


# ── The API shape ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_mode_defaults_to_observe() -> None:
    assert _payload(quality_mode=OBSERVE).quality_mode == OBSERVE
    assert PipelineCreate(
        name="p", template_id="t", connection_profile_id="c", zone="raw", domain="db", source_system="postgres",
        entity="products", mode="batch",
    ).quality_mode == OBSERVE


@pytest.mark.parametrize("bad", ["enforce", "BLOCK", "BLOCK_ON_FAILURE", "", "OFF"])
def test_only_observe_and_enforce_are_accepted(bad) -> None:
    with pytest.raises(PydanticValidationError):
        _payload(quality_mode=bad)
    with pytest.raises(PydanticValidationError):
        PipelineUpdate(quality_mode=bad)


def test_an_update_may_leave_the_mode_alone_or_set_it() -> None:
    assert PipelineUpdate().quality_mode is None
    assert PipelineUpdate(quality_mode=ENFORCE).quality_mode == ENFORCE


def test_an_enforce_sink_hands_off_through_an_internal_topic_and_a_source_keeps_its_prepared_topic() -> None:
    """A sink's passing records go to its table; the topic between its quality worker and its connector is internal: the "_" system prefix
    (hidden from the catalog) and a dotted name (Gravitino lists none). A source's prepared topic is a real data topic other pipelines read."""
    from app.services.quality_mode import is_sink_handoff

    sink = {**SINK, "quality_mode": ENFORCE}
    source = {"org_id": "org_1001", "direction": "SOURCE", "domain": "db", "source_system": "postgres", "entity": "products",
              "mode": "batch", "version": "v1", "quality_mode": ENFORCE}
    assert prepared_topic_for(sink) == f"_dataconnect.gate.{SINK['domain']}-{SINK['source_system']}-{SINK['entity']}-{SINK['mode']}-{SINK['version']}"
    assert is_sink_handoff(prepared_topic_for(sink))
    assert prepared_topic_for(source) == PREPARED_TOPIC
    assert not is_sink_handoff(prepared_topic_for(source))
    assert not is_sink_handoff(None)
