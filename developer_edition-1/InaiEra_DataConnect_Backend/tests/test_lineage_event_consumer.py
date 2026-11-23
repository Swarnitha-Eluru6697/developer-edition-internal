import pytest

from app.services.kafka_runtime import KafkaRecord
from app.services.lineage_event_consumer import LineageEventConsumer


class OneRecordConsumer:
    def __init__(self, record: KafkaRecord):
        self.record = record

    async def poll(self) -> KafkaRecord | None:
        record = self.record
        self.record = None
        return record


class FakeLineageService:
    """A minimal stand-in for LineageService — LineageEventConsumer's own
    job is just "poll, then forward to the service", which shouldn't need a
    real (Postgres-backed) LineageService to verify. See
    tests/test_lineage_service.py for the service's own behavior."""

    def __init__(self):
        self.received: list[dict] = []

    async def consume_runtime_event(self, event: dict) -> dict:
        self.received.append(event)
        return {"nodes": [], "edges": [{"pipeline_id": event.get("pipeline_id")}]}


def runtime_event() -> dict:
    return {
        "org_id": "org_lineage_test",
        "event_type": "pipeline_deployed",
        "from_asset": {
            "asset_type": "connector",
            "name": "conn",
            "qualified_name": "org_lineage_test:connector:conn",
            "metadata": {},
        },
        "to_asset": {
            "asset_type": "kafka_topic",
            "name": "topic",
            "qualified_name": "org_lineage_test:topic:topic",
            "metadata": {},
        },
        "edge_type": "EMITS_TO",
        "pipeline_id": "pipe_lineage",
    }


@pytest.mark.asyncio
async def test_lineage_event_consumer_forwards_the_polled_event_to_the_service() -> None:
    record = KafkaRecord(topic="lineage", key=None, value=runtime_event())
    service = FakeLineageService()

    result = await LineageEventConsumer(OneRecordConsumer(record), service).consume_once()

    assert result is not None
    assert result["edges"][0]["pipeline_id"] == "pipe_lineage"
    assert service.received == [runtime_event()]


@pytest.mark.asyncio
async def test_lineage_event_consumer_returns_none_when_nothing_polled() -> None:
    class EmptyConsumer:
        async def poll(self) -> KafkaRecord | None:
            return None

    service = FakeLineageService()

    result = await LineageEventConsumer(EmptyConsumer(), service).consume_once()

    assert result is None
    assert service.received == []
