import pytest

from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.streaming_quality_processor import StreamingQualityProcessor


@pytest.mark.asyncio
async def test_streaming_quality_processor_quarantines_invalid_record() -> None:
    producer = InMemoryKafkaProducer()
    processor = StreamingQualityProcessor(producer)
    pipeline = {
        "id": "pipe_1",
        "org_id": "org_1001",
        "topic_name": "raw-crm-salesforce-accounts-api-v1",
        "domain": "crm",
        "source_system": "salesforce",
        "entity": "accounts",
        "version": "v1",
    }
    policy = {"id": "dqp_1", "rules": [{"name": "id_required", "type": "required", "field": "id"}]}

    result = await processor.process_record(record={"id": ""}, pipeline=pipeline, policy=policy)

    assert result["status"] == "quarantined"
    assert producer.records[0].topic == "quarantine-crm-salesforce-accounts-validation-v1"
    assert producer.records[0].value["failed_rules"] == ["id_required"]


@pytest.mark.asyncio
async def test_streaming_quality_processor_allows_valid_record() -> None:
    producer = InMemoryKafkaProducer()
    processor = StreamingQualityProcessor(producer)
    pipeline = {
        "id": "pipe_1",
        "org_id": "org_1001",
        "topic_name": "raw-crm-salesforce-accounts-api-v1",
        "domain": "crm",
        "source_system": "salesforce",
        "entity": "accounts",
        "version": "v1",
    }
    policy = {"id": "dqp_1", "rules": [{"name": "id_required", "type": "required", "field": "id"}]}

    result = await processor.process_record(record={"id": "1"}, pipeline=pipeline, policy=policy)

    assert result["status"] == "valid"
    assert result["destination_topic"] == "prepared-crm-salesforce-accounts-api-v1"
    # A "valid" result must mean the record was actually produced somewhere —
    # not silently dropped.
    assert len(producer.records) == 1
    assert producer.records[0].topic == "prepared-crm-salesforce-accounts-api-v1"
    assert producer.records[0].value == {"id": "1"}
    assert result["delivery"]["topic"] == "prepared-crm-salesforce-accounts-api-v1"

