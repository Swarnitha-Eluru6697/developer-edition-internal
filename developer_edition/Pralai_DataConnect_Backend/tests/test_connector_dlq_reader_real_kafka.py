"""
NEW-06 against a REAL Kafka broker: `limit=200` on a topic with 20 partitions returns no more than 200 records, and they are the newest 200.

A throwaway 20-partition topic with 600 records (30 per partition, a quarter of them sharing a timestamp so the tie-breaker is exercised) is created
and removed afterwards. Skipped unless DATACONNECT_REAL_KAFKA_TESTS=1 (the broker is DATACONNECT_KAFKA_BOOTSTRAP, default localhost:9092).
"""
import os
import time
import uuid

import pytest
from kafka import KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic

import app.services.connector_dlq_reader as reader

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_KAFKA_TESTS"), reason="needs a real Kafka (DATACONNECT_REAL_KAFKA_TESTS=1)")

PARTITIONS, PER_PARTITION = 20, 30


@pytest.fixture
def topic(monkeypatch):
    bootstrap = os.environ.get("DATACONNECT_KAFKA_BOOTSTRAP", "localhost:9092")
    monkeypatch.setattr(reader.settings, "kafka_bootstrap", bootstrap)
    name = f"dlqreader-{uuid.uuid4().hex[:10]}"
    admin = KafkaAdminClient(bootstrap_servers=bootstrap)
    admin.create_topics([NewTopic(name, num_partitions=PARTITIONS, replication_factor=1)])
    time.sleep(2)
    base = int(time.time() * 1000) - 60_000
    producer = KafkaProducer(bootstrap_servers=bootstrap)
    expected = []
    for partition in range(PARTITIONS):
        for offset in range(PER_PARTITION):
            # Unique increasing timestamps, except the last quarter of every partition, which all share ONE timestamp (a batch failing together).
            stamp = base + offset * 1000 + partition if offset < PER_PARTITION * 3 // 4 else base + 10_000_000
            producer.send(name, value=b"x", key=f"{partition}:{offset}".encode(), partition=partition, timestamp_ms=stamp,
                          headers=[("id", f"{partition}:{offset}".encode())])
            expected.append((stamp, partition, offset))
    producer.flush()
    producer.close()
    try:
        yield name, expected
    finally:
        try:
            admin.delete_topics([name])
        except Exception:  # noqa: BLE001
            pass
        admin.close()


def _newest(expected, n):
    return sorted(expected, key=lambda e: (-e[0], e[1], -e[2]))[:n]


def test_limit_200_on_20_partitions_returns_no_more_than_200_and_they_are_the_newest(topic) -> None:
    name, expected = topic

    result = reader._read_recent_sync(name, 3600, 200)

    assert len(expected) == 600                                           # the topic holds three times the limit
    assert len(result) == 200                                             # the old per-partition cap would have returned all 600
    assert [(r["timestamp_ms"], r["partition"], r["offset"]) for r in result] == _newest(expected, 200)


def test_the_order_is_the_same_on_every_read_and_a_smaller_limit_is_its_prefix(topic) -> None:
    name, _ = topic

    first = [r["headers"]["id"] for r in reader._read_recent_sync(name, 3600, 200)]
    second = [r["headers"]["id"] for r in reader._read_recent_sync(name, 3600, 200)]
    top_50 = [r["headers"]["id"] for r in reader._read_recent_sync(name, 3600, 50)]

    assert first == second                                                # ties (150 records share a timestamp) are broken the same way each time
    assert top_50 == first[:50]
    assert first[0] == "0:29"                                             # lowest partition, newest offset, among the shared newest timestamp
