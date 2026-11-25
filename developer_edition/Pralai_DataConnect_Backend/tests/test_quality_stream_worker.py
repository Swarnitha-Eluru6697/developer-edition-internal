from types import SimpleNamespace

import pytest

from app.services.kafka_runtime import InMemoryKafkaProducer, KafkaRecord
from app.services.quality_stream_worker import (
    QualityStreamManager,
    QualityStreamWorker,
    _decode_record,
    _input_topics,
)

PIPELINE = {
    "id": "pipe_sink_1",
    "org_id": "org_1001",
    "topic_name": "raw__db__postgres__products_copy__batch__v1",
    "domain": "db",
    "source_system": "postgres",
    "entity": "products_copy",
    "version": "v1",
    "schema_version_id": None,
    "connector_config": {"topics": "raw__db__postgres__products__batch__v1"},
    "quality_policy_id": "dqp_1",
}

POLICY = {
    "id": "dqp_1",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}


def _worker() -> QualityStreamWorker:
    return QualityStreamWorker(org_id="org_1001", pipeline=PIPELINE, policy=POLICY)


@pytest.fixture(autouse=True)
def _no_real_db_for_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise _handle()'s evaluation logic only —
    _record_outcome's own persistence is covered separately in
    tests/test_quality_stream_stats.py, against a mocked repository rather
    than a real Postgres. Without this, every test here would attempt (and
    fail/raise on) a real DB connection, since _record_outcome now
    re-raises a stats-write failure rather than swallowing it (see
    test_quality_stream_stats.py's test_record_outcome_propagates_a_persistence_failure)."""

    async def _noop(self, **kwargs) -> None:
        pass

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _noop)


@pytest.fixture(autouse=True)
def kafka(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """No real broker: the producer is in-memory and topic creation is recorded."""
    fake = SimpleNamespace(producer=InMemoryKafkaProducer(), ensured=[])

    async def _ensure(topic: str, **kwargs) -> None:
        fake.ensured.append(topic)

    async def _producer():
        return fake.producer

    monkeypatch.setattr("app.services.quality_stream_worker.ensure_simple_topic", _ensure)
    monkeypatch.setattr("app.services.quality_stream_worker.get_kafka_producer", _producer)
    return fake


QUARANTINE_TOPIC = "quarantine-db-postgres-products_copy-validation-v1"


@pytest.mark.asyncio
async def test_handle_records_a_passing_outcome_without_producing_anywhere(monkeypatch, kafka) -> None:
    """A passing record is only evaluated and counted: nothing is produced,
    and the pipeline's own topic is never written to by this worker."""
    seen = []

    async def _record_outcome(self, *, valid: bool, **_kwargs) -> None:
        seen.append(valid)

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    envelope = {"schema": {"type": "struct", "fields": []}, "payload": {"stock_quantity": 50, "sku": "X-1"}}

    await _worker()._handle(envelope=envelope)

    assert seen == [True]
    assert kafka.producer.records == []
    assert kafka.ensured == []


@pytest.mark.asyncio
async def test_handle_records_a_failing_outcome_for_an_out_of_range_value(monkeypatch) -> None:
    seen = []

    async def _record_outcome(self, *, valid: bool, **_kwargs) -> None:
        seen.append(valid)

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)
    envelope = {"schema": {"type": "struct", "fields": []}, "payload": {"stock_quantity": 5, "sku": "X-2"}}

    await _worker()._handle(envelope=envelope)

    assert seen == [False]


@pytest.mark.asyncio
async def test_handle_record_without_envelope_is_evaluated_as_is(monkeypatch) -> None:
    seen = []

    async def _record_outcome(self, *, valid: bool, **_kwargs) -> None:
        seen.append(valid)

    monkeypatch.setattr(QualityStreamWorker, "_record_outcome", _record_outcome)

    await _worker()._handle(envelope={"stock_quantity": 200})

    assert seen == [False]


@pytest.mark.asyncio
async def test_failing_record_is_copied_to_the_validation_quarantine_topic(kafka) -> None:
    """The whole data set stays on the pipeline's topic; only a record that
    fails the policy is copied to <org>.quarantine.<domain>.<source>.<entity>
    .validation.<version> — the separate .connector. topic is Kafka Connect's."""
    envelope = {"schema": {"type": "struct", "fields": []}, "payload": {"stock_quantity": 5, "sku": "X-2"}}

    await _worker()._handle(envelope=envelope)

    assert [r.topic for r in kafka.producer.records] == [QUARANTINE_TOPIC]
    sent = kafka.producer.records[0].value
    assert sent["failure_type"] == "quality_rule_failed"
    assert sent["failed_rules"] == ["stock_range"]
    assert sent["original_record"] == {"stock_quantity": 5, "sku": "X-2"}
    assert sent["pipeline_id"] == "pipe_sink_1"


@pytest.mark.asyncio
async def test_quarantine_topic_is_created_explicitly_once_per_worker(kafka) -> None:
    worker = _worker()

    await worker._handle(envelope={"stock_quantity": 5})
    await worker._handle(envelope={"stock_quantity": 6})

    assert kafka.ensured == [QUARANTINE_TOPIC]
    assert len(kafka.producer.records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_extra", [
    {"on_failure": "log_only"},
    {"quarantine_sink": "object_storage"},
])
async def test_policy_that_does_not_route_to_kafka_produces_nothing(kafka, policy_extra) -> None:
    worker = QualityStreamWorker(org_id="org_1001", pipeline=PIPELINE, policy={**POLICY, **policy_extra})

    await worker._handle(envelope={"stock_quantity": 5})

    assert kafka.producer.records == []
    assert kafka.ensured == []


def test_decode_record_returns_full_envelope_unmodified() -> None:
    envelope = {"schema": {"type": "struct"}, "payload": {"a": 1}}
    record = KafkaRecord(topic="t", key=None, value={"_raw": '{"schema": {"type": "struct"}, "payload": {"a": 1}}'})

    assert _decode_record(record) == envelope


def test_decode_record_returns_none_for_malformed_json() -> None:
    record = KafkaRecord(topic="t", key=None, value={"_raw": "not json"})

    assert _decode_record(record) is None


def test_decode_record_returns_none_when_no_raw_value() -> None:
    record = KafkaRecord(topic="t", key=None, value={})

    assert _decode_record(record) is None


@pytest.mark.parametrize(
    ("pipeline", "expected"),
    [
        # Sink pipeline, policy attached, ordinary zone -> needs a worker.
        ({"quality_policy_id": "dqp_1", "zone": "raw", "connector_config": {"topics": "raw_topic"}}, True),
        # Source pipeline (no connector_config.topics at all) with a policy
        # attached also needs a worker — it just resolves its input
        # differently (its own topic_name directly; see _input_topics below).
        ({"quality_policy_id": "dqp_1", "zone": "raw", "connector_config": {}}, True),
        ({"quality_policy_id": "dqp_1", "zone": "curated", "connector_config": None}, True),
        # No policy attached -> no worker.
        ({"quality_policy_id": None, "zone": "raw", "connector_config": {"topics": "raw_topic"}}, False),
        # Policy attached but the pipeline's own zone IS "quarantine" ->
        # explicitly out of scope for now, left alone.
        ({"quality_policy_id": "dqp_1", "zone": "quarantine", "connector_config": {}}, False),
    ],
)
def test_needs_worker(pipeline: dict, expected: bool) -> None:
    assert QualityStreamManager._needs_worker(pipeline) is expected


def test_input_topics_for_sink_pipeline_are_its_configured_source_topics() -> None:
    pipeline = {
        "zone": "raw",
        "topic_name": "raw__db__postgres__products_copy__batch__v1",
        "connector_config": {"topics": "raw__db__postgres__products__batch__v1"},
    }
    assert _input_topics(pipeline) == ["raw__db__postgres__products__batch__v1"]


def test_input_topics_for_source_pipeline_are_its_own_topic_name() -> None:
    """No more "intake" indirection — a source pipeline's worker reads the
    exact same topic its own connector writes every record to."""
    pipeline = {
        "zone": "curated",
        "topic_name": "curated__db__postgres__accounts__batch__v1",
        "connector_config": {"tables": ["accounts"]},
    }
    assert _input_topics(pipeline) == ["curated__db__postgres__accounts__batch__v1"]


def test_input_topics_are_empty_for_quarantine_zone_pipeline() -> None:
    pipeline = {
        "zone": "quarantine",
        "topic_name": "quarantine__db__postgres__accounts__batch__v1",
        "connector_config": {},
    }
    assert _input_topics(pipeline) == []


@pytest.mark.asyncio
async def test_the_manager_rebuilds_a_running_worker_when_its_pipeline_or_policy_changes(monkeypatch) -> None:
    """A worker reads its mode, topics and policy once, when it is built. The manager used to skip any pipeline that already had a worker,
    so switching a running pipeline OBSERVE -> ENFORCE (or editing a rule) did nothing until the backend restarted: no prepared topic was
    written and failing records kept flowing."""
    from contextlib import asynccontextmanager

    import app.services.quality_stream_worker as qsw
    from app.services.quality_service import quality_service

    state = {
        "pipeline": {"id": "p1", "org_id": "org_1", "name": "src", "quality_policy_id": "dqp_1", "quality_mode": "OBSERVE",
                     "zone": "raw", "domain": "shop", "source_system": "pg", "entity": "products", "mode": "batch", "version": "v1",
                     "topic_name": "raw-shop-pg-products-batch-v1", "status": "running"},
        "policy": {"id": "dqp_1", "name": "checks", "rules": [{"name": "r1", "type": "required", "field": "a"}],
                   "on_failure": "route_to_quarantine", "quarantine_sink": "kafka"},
    }

    class _Result:
        def scalars(self):
            return self

        def all(self):
            from types import SimpleNamespace
            return [SimpleNamespace(id="p1")]

    class _Session:
        async def execute(self, statement):
            return _Result()

    @asynccontextmanager
    async def _session():
        yield _Session()

    async def _get_policy(org_id, policy_id):
        return dict(state["policy"])

    started: list[str | None] = []

    def _start(worker):
        started.append(worker.pipeline.get("quality_mode"))

    async def _stop(worker):
        pass
    monkeypatch.setattr(qsw, "AsyncSessionLocal", _session)
    monkeypatch.setattr(qsw, "_pipeline_row_to_dict", lambda row: dict(state["pipeline"]))
    monkeypatch.setattr(quality_service, "get_policy", _get_policy)
    monkeypatch.setattr(qsw.QualityStreamWorker, "start", _start)
    monkeypatch.setattr(qsw.QualityStreamWorker, "stop", _stop)
    manager = qsw.QualityStreamManager()

    await manager.reconcile_once()
    await manager.reconcile_once()                                   # nothing changed: the same worker keeps running
    assert started == ["OBSERVE"]

    state["pipeline"] = {**state["pipeline"], "quality_mode": "ENFORCE"}
    await manager.reconcile_once()
    assert started == ["OBSERVE", "ENFORCE"]
    assert manager._workers["p1"]._enforcing is True

    state["policy"] = {**state["policy"], "rules": [{"name": "r2", "type": "required", "field": "b"}]}
    await manager.reconcile_once()                                   # a rule edited: rebuilt with the new policy
    assert len(started) == 3
    assert manager._workers["p1"].policy["rules"][0]["name"] == "r2"
