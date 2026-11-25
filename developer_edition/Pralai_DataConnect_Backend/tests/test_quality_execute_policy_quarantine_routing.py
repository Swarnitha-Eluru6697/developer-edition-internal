from uuid import uuid4

import pytest

import app.services.quality_service as quality_service_module
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quality_service import QualityService


class _FakeResultRow:
    def __init__(self, values: dict) -> None:
        self.id = None
        self.__dict__.update(values)


class _FakeQualityResultRepository:
    def __init__(self, session) -> None:
        pass

    async def create(self, values: dict) -> _FakeResultRow:
        values = dict(values)
        values.setdefault("id", str(uuid4()))
        return _FakeResultRow(values)


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        pass


PIPELINE = {
    "id": "pipe_1",
    "org_id": "org_1001",
    "domain": "db",
    "source_system": "postgres",
    "entity": "products",
    "version": "v1",
    "topic_name": "raw__db__postgres__products__batch__v1",
}
POLICY = {
    "id": "dqp_1",
    "on_failure": "route_to_quarantine",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}],
}


def _patch_db(monkeypatch) -> None:
    monkeypatch.setattr(quality_service_module, "QualityResultRepository", _FakeQualityResultRepository)
    monkeypatch.setattr(quality_service_module, "AsyncSessionLocal", lambda: _FakeSession())


@pytest.mark.asyncio
async def test_execute_policy_quarantines_failures_by_default(monkeypatch) -> None:
    _patch_db(monkeypatch)
    producer = InMemoryKafkaProducer()
    service = QualityService()

    result = await service.execute_policy(
        org_id="org_1001", pipeline=PIPELINE, policy=POLICY,
        records=[{"stock_quantity": 5}], producer=producer,
    )

    assert result["failed_count"] == 1
    assert len(producer.records) == 1
    assert producer.records[0].topic == "quarantine-db-postgres-products-validation-v1"


@pytest.mark.asyncio
async def test_execute_policy_can_skip_quarantining_while_still_persisting_result(monkeypatch) -> None:
    """PipelineService.autorun_quality_from_source's one-time create-time
    sample check passes route_failures_to_quarantine=False so it doesn't
    duplicate QualityStreamWorker's own continuous quarantine writes for the
    same pipeline — the QualityResult summary (still shown in the Quality
    tab) must persist regardless, only the Kafka produce is skipped."""
    _patch_db(monkeypatch)
    producer = InMemoryKafkaProducer()
    service = QualityService()

    result = await service.execute_policy(
        org_id="org_1001", pipeline=PIPELINE, policy=POLICY,
        records=[{"stock_quantity": 5}], producer=producer,
        route_failures_to_quarantine=False,
    )

    assert result["failed_count"] == 1
    assert result["status"] == "failed"
    assert producer.records == []
