"""
THRESHOLD error-handling policy enforcement (GAP-04) — the one policy Kafka
Connect has no native equivalent for. errors.tolerance is a binary
none/all; there's no "fail after N errors in a W-minute window" setting to
hand Connect. A pipeline configured with error_handling_policy="THRESHOLD"
still runs with errors.tolerance=all under the hood (see
app/services/pipeline_config.py's error_handling_connect_config) — an
individual bad record never crashes the task — so this module is what
actually enforces the threshold: counting how many records landed on this
pipeline's own connector-DLQ topic (see app/services/topic_naming.py's
build_connector_quarantine_topic_name — GAP-03 split that from the quality-
validation quarantine specifically so it's a clean, connector-errors-only
signal) within the policy's rolling window. app/services/runtime_reconciler.py
calls this once per tick, per THRESHOLD-policy pipeline, and pauses the
connector when the count exceeds max_errors.

Counts by offset delta (Kafka's own offsetsForTimes/endOffsets), not by
consuming and deserializing every record in the window — a single small
blocking round trip per pipeline per tick, run in a worker thread the same
way kafka_admin.py's AdminClient calls already are, so it never blocks the
reconciler's event loop.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from kafka import KafkaConsumer, TopicPartition

from app.core.config import settings


def _count_recent_messages_sync(topic: str, window_seconds: int) -> int:
    consumer = KafkaConsumer(
        bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-error-threshold",
        # Never actually joins a consumer group or commits an offset — this
        # only ever calls partitions_for_topic/offsets_for_times/end_offsets,
        # none of which touch group coordination.
        enable_auto_commit=False,
    )
    try:
        partitions = consumer.partitions_for_topic(topic)
        if not partitions:
            return 0  # topic doesn't exist yet (no error has ever landed here) — zero, not an error
        topic_partitions = [TopicPartition(topic, p) for p in partitions]
        window_start_ms = int((datetime.now(UTC) - timedelta(seconds=window_seconds)).timestamp() * 1000)
        start_offsets = consumer.offsets_for_times(dict.fromkeys(topic_partitions, window_start_ms))
        end_offsets = consumer.end_offsets(topic_partitions)

        total = 0
        for tp in topic_partitions:
            end = end_offsets.get(tp, 0)
            found = start_offsets.get(tp)
            if found is None:
                # No message on this partition has a timestamp >= window_start
                # — either the partition is empty, or every message on it is
                # older than the window. Either way, zero of ITS messages
                # fall inside the window; using the partition's earliest
                # offset instead would overcount by however much history
                # predates the window.
                continue
            total += max(0, end - found.offset)
        return total
    finally:
        consumer.close()


async def count_recent_connector_errors(topic: str, window_seconds: int) -> int:
    """How many records landed on `topic` (a connector-DLQ topic) in the
    last `window_seconds`. 0 if the topic doesn't exist yet or is empty —
    never raises for that; a genuine broker-connectivity failure still
    propagates, since the caller (runtime_reconciler) treats "couldn't find
    out" very differently from "found out and it's zero"."""
    return await asyncio.to_thread(_count_recent_messages_sync, topic, window_seconds)
