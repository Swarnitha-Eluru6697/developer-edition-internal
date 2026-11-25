import pytest

from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quarantine_sink import (
    KafkaQuarantineSink,
    ObjectStorageQuarantineSink,
    get_quarantine_sink,
)

PIPELINE = {
    "id": "pipe_1",
    "org_id": "org_1001",
    "domain": "crm",
    "source_system": "salesforce",
    "entity": "accounts",
    "version": "v1",
}
ENVELOPES = [
    {
        "failure_type": "quality_rule_failed",
        "original_record": {"id": "1"},
    },
    {
        "failure_type": "quality_rule_failed",
        "original_record": {"id": "2"},
    },
]


@pytest.mark.asyncio
async def test_kafka_quarantine_sink_sends_every_envelope_to_the_quarantine_topic() -> None:
    producer = InMemoryKafkaProducer()
    sink = KafkaQuarantineSink(producer)

    await sink.send(envelopes=ENVELOPES, pipeline=PIPELINE)

    assert len(producer.records) == 2
    assert all(
        record.topic == "quarantine-crm-salesforce-accounts-validation-v1"
        for record in producer.records
    )
    assert [record.key for record in producer.records] == ["1", "2"]


@pytest.mark.asyncio
async def test_object_storage_quarantine_sink_raises_until_implemented() -> None:
    sink = ObjectStorageQuarantineSink()

    with pytest.raises(NotImplementedError):
        await sink.send(envelopes=ENVELOPES, pipeline=PIPELINE)


def test_get_quarantine_sink_defaults_to_kafka() -> None:
    producer = InMemoryKafkaProducer()

    assert isinstance(get_quarantine_sink({}, producer), KafkaQuarantineSink)
    assert isinstance(get_quarantine_sink({"quarantine_sink": "kafka"}, producer), KafkaQuarantineSink)
    assert isinstance(
        get_quarantine_sink({"quarantine_sink": "object_storage"}, producer),
        ObjectStorageQuarantineSink,
    )
