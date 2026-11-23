"""FE-BUG-17: reads a bounded sample of the most recent records off a
connector-DLQ topic, headers only, so the Quality screen can say *why* records
landed there (see connector_error_classifier.py). error_threshold.py counts by
offset delta and never reads a record; this does read them, so it is capped
(`limit`) and run in a worker thread, like that module's own calls.
"""

import asyncio
import heapq
from datetime import UTC, datetime, timedelta

from kafka import KafkaConsumer, TopicPartition

from app.core.config import settings

_POLL_TIMEOUT_MS = 1500
_POLL_MAX_RECORDS = 500
#: A hard ceiling on one request, whatever the caller asks for. Candidates are read per partition, so this also bounds the work: at most
#: _MAX_LIMIT records per partition are ever read.
_MAX_LIMIT = 1000


def _decode(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


def _rank(timestamp_ms: int, partition: int, offset: int) -> tuple[int, int, int]:
    """Where a record stands among the newest: a LARGER rank is newer. Newest timestamp first, then the lower partition, then the higher offset —
    a total order, so two records with the same timestamp (common: a batch fails in one millisecond) always come out in the same order and which
    of them is cut at the limit never depends on how the partitions happened to be polled."""
    return (timestamp_ms, -partition, offset)


def _read_recent_sync(topic: str, window_seconds: int, limit: int) -> list[dict]:
    limit = max(0, min(int(limit), _MAX_LIMIT))
    if limit == 0:
        return []
    consumer = KafkaConsumer(
        bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-dlq-reader",
        enable_auto_commit=False, group_id=None, auto_offset_reset="earliest",
    )
    try:
        partitions = consumer.partitions_for_topic(topic)
        if not partitions:
            return []
        topic_partitions = [TopicPartition(topic, p) for p in sorted(partitions)]
        consumer.assign(topic_partitions)
        window_start_ms = int((datetime.now(UTC) - timedelta(seconds=window_seconds)).timestamp() * 1000)
        starts = consumer.offsets_for_times(dict.fromkeys(topic_partitions, window_start_ms))
        ends = consumer.end_offsets(topic_partitions)
        remaining: dict[TopicPartition, int] = {}
        for tp in topic_partitions:
            found = starts.get(tp)
            if found is None or ends.get(tp, 0) <= found.offset:
                continue
            # The newest `limit` records of the window on this partition: the CANDIDATES. The overall newest `limit` records can only come from
            # these (a partition cannot contribute more than `limit` of them), so nothing older is ever read.
            consumer.seek(tp, max(found.offset, ends[tp] - limit))
            remaining[tp] = ends[tp]
        if not remaining:
            return []

        # The newest `limit` candidates SEEN SO FAR, as a min-heap on rank: the oldest of them is on top and is the one dropped when a newer
        # candidate arrives. At most `limit` records are ever held, however many partitions there are.
        newest: list[tuple[tuple[int, int, int], int, dict]] = []
        sequence = 0
        while remaining:
            batch = consumer.poll(timeout_ms=_POLL_TIMEOUT_MS, max_records=_POLL_MAX_RECORDS)
            if not batch:
                break
            for tp, records in batch.items():
                end = remaining.get(tp)
                if end is None:
                    continue
                for record in records:
                    if record.offset >= end:
                        break
                    timestamp_ms = record.timestamp if record.timestamp is not None else -1
                    entry = {
                        "timestamp_ms": record.timestamp, "partition": tp.partition, "offset": record.offset,
                        "headers": {k: _decode(v) for k, v in (record.headers or [])},
                    }
                    sequence += 1
                    item = (_rank(timestamp_ms, tp.partition, record.offset), sequence, entry)
                    if len(newest) < limit:
                        heapq.heappush(newest, item)
                    elif item[0] > newest[0][0]:
                        heapq.heapreplace(newest, item)
                if records and records[-1].offset + 1 >= end:
                    remaining.pop(tp)
        return [entry for _rank_key, _seq, entry in sorted(newest, key=lambda item: item[0], reverse=True)]
    finally:
        consumer.close()


async def read_recent_connector_errors(topic: str, window_seconds: int, limit: int = 200) -> list[dict]:
    """At most `limit` records IN TOTAL — across all of the topic's partitions, not per partition — the newest ones within the window, newest first,
    each as {"timestamp_ms", "partition", "offset", "headers"}. Records with the same timestamp are ordered by lower partition, then higher offset,
    so the result is the same every time. `limit` is capped at 1000 and a limit of 0 or less returns []. [] if the topic doesn't exist yet.

    "Newest" is decided in two steps, which is what keeps the read bounded: the CANDIDATES are the newest `limit` records of each partition by log
    position, and the answer is the `limit` newest of those by timestamp. Kafka Connect stamps a DLQ record when it writes it, so log position and
    time agree and this is exactly the newest `limit` of the window; a producer that stamped records more than `limit` positions out of order within
    one partition could have such a record missed."""
    return await asyncio.to_thread(_read_recent_sync, topic, window_seconds, limit)
