"""A record's id is very often a NUMBER (a database primary key). The producer's key serializer only accepts text, so a numeric id used as the
Kafka key made the produce raise: a failing record with an integer id was never quarantined — it was dead-lettered after its retries and the
quarantine topic stayed empty. Found by running the real worker against a real broker (NEW-01); the in-memory test producer was more lenient
than the real one and is now as strict (it raises on a non-text key)."""
import pytest

from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quarantine_sink import KafkaQuarantineSink
from app.services.record_key import record_key
from app.services.streaming_quality_processor import StreamingQualityProcessor

PIPELINE = {"id": "p1", "org_id": "org_1001", "domain": "db", "source_system": "pg", "entity": "orders", "version": "v1", "zone": "raw",
            "mode": "batch", "topic_name": "raw-db-pg-orders-batch-v1", "direction": "SOURCE"}
POLICY = {"id": "dqp_1", "rules": [{"name": "stock", "type": "min_max_numeric", "field": "stock_quantity", "min": 20, "max": 150}]}


@pytest.mark.parametrize(("record", "expected"), [
    ({"id": 42}, "42"),
    ({"id": 0}, "0"),                                    # 0 is a real id; the old `or` skipped it
    ({"id": "abc"}, "abc"),
    ({"id": 7.5}, "7.5"),
    ({"customer_id": 9}, "9"),
    ({"id": None, "customer_id": 9}, "9"),
    ({"id": "", "customer_id": "c1"}, "c1"),             # an empty id identifies nothing
    ({"id": 1, "customer_id": 2}, "1"),                  # id wins
    ({"id": True}, None),                                # a bool is not an id
    ({"id": {"nested": 1}}, None),
    ({"id": ["a"]}, None),
    ({"id": None}, None),
    ({}, None),
])
def test_the_key_is_the_id_as_text_or_none(record, expected) -> None:
    assert record_key(record) == expected


def test_the_in_memory_producer_rejects_a_non_text_key_like_the_real_one() -> None:
    import asyncio

    with pytest.raises(TypeError, match="must be text"):
        asyncio.run(InMemoryKafkaProducer().produce(topic="t", key=42, value={}))


def _failing_envelope(record: dict) -> dict:
    return {"failure_type": "quality_validation_failed", "original_record": record, "failed_rules": ["stock"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("record_id", [1, 0, 12345678901, "k-1"])
async def test_a_failing_record_with_a_numeric_id_is_quarantined_not_lost(record_id) -> None:
    producer = InMemoryKafkaProducer()

    await KafkaQuarantineSink(producer).send(envelopes=[_failing_envelope({"id": record_id, "stock_quantity": 1})], pipeline=PIPELINE)

    (sent,) = producer.records
    assert sent.key == str(record_id)
    assert sent.topic == "quarantine-db-pg-orders-validation-v1"


@pytest.mark.asyncio
async def test_the_streaming_processor_routes_numeric_id_records_both_ways() -> None:
    producer = InMemoryKafkaProducer()
    processor = StreamingQualityProcessor(producer)

    valid = await processor.process_record(record={"id": 5, "stock_quantity": 50}, pipeline=PIPELINE, policy=POLICY)
    invalid = await processor.process_record(record={"id": 6, "stock_quantity": 1}, pipeline=PIPELINE, policy=POLICY)

    assert (valid["status"], invalid["status"]) == ("valid", "quarantined")
    assert [r.key for r in producer.records] == ["5", "6"]
