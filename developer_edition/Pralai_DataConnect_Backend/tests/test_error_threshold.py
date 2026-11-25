"""
GAP-04's THRESHOLD enforcement — count_recent_connector_errors counts a
connector-DLQ topic's message volume in a rolling window by offset delta
(offsets_for_times/end_offsets), not by consuming and deserializing every
record. No live Kafka broker in this sandbox, so KafkaConsumer itself is
faked the same way tests/test_kafka_admin.py fakes KafkaAdminClient.
"""

import pytest

import app.services.error_threshold as error_threshold_module
from app.services.error_threshold import count_recent_connector_errors


class _OffsetAndTimestamp:
    def __init__(self, offset: int) -> None:
        self.offset = offset


class _FakeConsumer:
    def __init__(self, *, partitions, start_offsets, end_offsets) -> None:
        self._partitions = partitions
        self._start_offsets = start_offsets  # {partition_num: offset | None}
        self._end_offsets = end_offsets  # {partition_num: offset}
        self.closed = False

    def partitions_for_topic(self, topic):
        return self._partitions

    def offsets_for_times(self, targets):
        result = {}
        for tp in targets:
            start = self._start_offsets.get(tp.partition)
            result[tp] = _OffsetAndTimestamp(start) if start is not None else None
        return result

    def end_offsets(self, topic_partitions):
        return {tp: self._end_offsets.get(tp.partition, 0) for tp in topic_partitions}

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeConsumer) -> None:
    monkeypatch.setattr(error_threshold_module, "KafkaConsumer", lambda **kwargs: fake)


@pytest.mark.asyncio
async def test_counts_messages_produced_since_window_start(monkeypatch) -> None:
    fake = _FakeConsumer(partitions={0}, start_offsets={0: 10}, end_offsets={0: 25})
    _install(monkeypatch, fake)

    count = await count_recent_connector_errors("org.quarantine.crm.salesforce.accounts.connector.v1", 300)

    assert count == 15
    assert fake.closed


@pytest.mark.asyncio
async def test_sums_across_every_partition(monkeypatch) -> None:
    fake = _FakeConsumer(partitions={0, 1}, start_offsets={0: 10, 1: 5}, end_offsets={0: 20, 1: 8})
    _install(monkeypatch, fake)

    count = await count_recent_connector_errors("some-dlq-topic", 300)

    assert count == (20 - 10) + (8 - 5)


@pytest.mark.asyncio
async def test_a_partition_with_nothing_in_window_counts_as_zero_not_its_full_history(monkeypatch) -> None:
    """offsets_for_times returns None for a partition when no message on it
    has a timestamp >= window_start — either it's empty, or everything on
    it predates the window. Either way that's zero NEW messages, not
    "count from the beginning of the partition" (which would wildly
    overcount however much history exists before the window)."""
    fake = _FakeConsumer(partitions={0}, start_offsets={0: None}, end_offsets={0: 500})
    _install(monkeypatch, fake)

    count = await count_recent_connector_errors("some-dlq-topic", 300)

    assert count == 0


@pytest.mark.asyncio
async def test_returns_zero_when_the_topic_does_not_exist_yet(monkeypatch) -> None:
    """No connector error has ever landed on this pipeline's DLQ topic yet
    — not a failure, just zero."""
    fake = _FakeConsumer(partitions=None, start_offsets={}, end_offsets={})
    _install(monkeypatch, fake)

    count = await count_recent_connector_errors("never-created-topic", 300)

    assert count == 0
