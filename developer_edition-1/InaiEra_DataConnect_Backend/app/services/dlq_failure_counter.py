"""FE-BUG-17: counts every record that lands on a pipeline's connector-DLQ
topic, by cause, into one-minute buckets (see models/failure_count.py).

The Quality screen used to classify only a bounded sample of the newest DLQ
records, so on a large DLQ the per-cause numbers were approximate. This
consumes the DLQ continuously instead — one Kafka consumer group per pipeline
(`inai-dlqcount-<pipeline_id>`), headers only, batched — and classifies
each record with connector_error_classifier. Every record is counted once
(modulo at-least-once redelivery, see below), however large the DLQ.

Records are bucketed by their own Kafka timestamp, not by when they were
read, so the first run after this ships back-fills the DLQ's existing
contents into the right minutes (anything older than the retention window is
skipped). Kafka's group coordinator makes this safe with several app
replicas: a partition is read by one of them at a time.

Counting is exactly-once per Kafka record (NEW-03). Delivery from Kafka is at-least-once — offsets are committed only after the batch's
counts are written, so a crash in between redelivers that batch — and re-counting it would overstate failures. So the highest offset
counted for each partition is stored in PostgreSQL in the SAME transaction as the count increments (FailureCountOffset), and a redelivered
record whose offset is already covered is skipped. PostgreSQL is the authority on what has been counted; Kafka's committed offset only says
where to start reading. The same holds if the consumer group is reset, a partition moves to another replica, or two replicas are handed the
same records (the offsets rows are locked for the transaction).

Several pipelines can write one DLQ topic (NEW-04; see app/services/dlq_attribution.py). The topic has ONE consumer, and each record is counted
under the pipeline whose connector wrote it, read from the `__connect.errors.connector.name` header Kafka Connect stamps on it — never under
"the first pipeline that uses the topic". A record that cannot be attributed (no header on a shared topic, or a connector that is not one of
the topic's pipelines) is counted for no pipeline, logged, and visible in `dataconnect_dlq_unattributed_records_total`.

Same lifecycle shape as QualityStreamManager: a periodic reconcile keeps one
worker per DLQ topic and survives an app restart.
"""

import asyncio
import functools
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select

from app.core.config import settings
from app.core.error_policies import DLQ_POLICIES
from app.core.metrics import record_dlq_unattributed
from app.db.session import AsyncSessionLocal
from app.models.pipeline import Pipeline
from app.repositories.dataconnect import FailureCountOffsetRepository
from app.services.connector_error_classifier import classify_connector_error
from app.services.dlq_attribution import DlqAttribution, pipelines_by_dlq_topic
from app.services.failure_counters import (
    INGESTION,
    add_ingestion_counts,
    minute_bucket,
    prune_old_buckets,
)

logger = structlog.get_logger(__name__)

_POLL_TIMEOUT_MS = 1000
_BATCH_SIZE = 500
_MISSING_TOPIC_RETRY_SECONDS = 60.0

_executor: ThreadPoolExecutor | None = None


def _dlq_executor() -> ThreadPoolExecutor:
    """The counters' own thread pool, never the event loop's default one.

    Each worker blocks a thread in consumer.poll() for up to _POLL_TIMEOUT_MS every cycle, forever. There is one worker
    per DLQ topic — dozens on a stack with many sink pipelines — so on the shared default pool they hold every thread
    and every other asyncio.to_thread caller (dashboard/monitoring metrics, /runtime/flow, the reconciler's Kafka
    calls) waits behind them indefinitely. Confined to this pool, the workers just take turns (each topic is polled
    again after the others have had a go) and the rest of the app is unaffected."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=max(1, settings.dlq_counter_threads), thread_name_prefix="dlq-count")
    return _executor


async def _in_dlq_pool(fn, /, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(_dlq_executor(), functools.partial(fn, *args, **kwargs))


def _record_headers(record) -> dict[str, str]:
    return {key: (value or b"").decode("utf-8", "replace") for key, value in (record.headers or [])}


def _record_bucket(record, *, oldest: datetime) -> datetime | None:
    """The one-minute bucket a record belongs to (by its own Kafka timestamp), or None if it is older than the retention window."""
    timestamp_ms = getattr(record, "timestamp", None)
    when = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC) if timestamp_ms and timestamp_ms > 0 else datetime.now(UTC)
    return None if when < oldest else minute_bucket(when)


def count_batch(records, *, oldest: datetime) -> Counter[tuple[datetime, str]]:
    """{(minute bucket, category): n} for a batch of consumed DLQ records,
    skipping any older than `oldest`. Pure so it can be tested without Kafka."""
    counts: Counter[tuple[datetime, str]] = Counter()
    for record in records:
        bucket = _record_bucket(record, oldest=oldest)
        if bucket is not None:
            counts[(bucket, classify_connector_error(_record_headers(record)))] += 1
    return counts


def count_batch_by_pipeline(
    records, *, oldest: datetime, attribution: DlqAttribution,
) -> tuple[dict[str, Counter[tuple[datetime, str]]], int]:
    """({pipeline id: {(minute bucket, category): n}}, how many records could not be attributed to any pipeline). Each record is counted under
    the pipeline its connector-name header names (see DlqAttribution), never under another one. Pure, so it needs no Kafka or database."""
    per_pipeline: dict[str, Counter[tuple[datetime, str]]] = {}
    unattributed = 0
    for record in records:
        bucket = _record_bucket(record, oldest=oldest)
        if bucket is None:
            continue
        headers = _record_headers(record)
        pipeline_id = attribution.pipeline_id_for(headers)
        if pipeline_id is None:
            unattributed += 1
            continue
        per_pipeline.setdefault(pipeline_id, Counter())[(bucket, classify_connector_error(headers))] += 1
    return per_pipeline, unattributed


@dataclass(frozen=True)
class BatchOutcome:
    """What one batch did: `counted` records were new and attributed to a pipeline, `replayed` were already counted (redelivered after a crash, a
    group reset or a rebalance) and were skipped, `unattributed` were new but could not be tied to any one pipeline and so were counted for none."""

    counted: int
    replayed: int
    unattributed: int = 0


async def record_batch_once(
    *, org_id: str, topic: str, records, oldest: datetime, attribution: DlqAttribution,
) -> BatchOutcome:
    """Count one batch of consumed DLQ records, once, each under its own pipeline, and remember how far each partition has been counted — in
    ONE transaction.

    The offsets rows are locked first and the records are filtered against them afterwards, so a record is counted by exactly one committed
    transaction however often, and by whichever replica, it is delivered. Either the increments for EVERY pipeline AND the new offsets commit, or
    none of it does. Records are counted in offset order per partition, so "everything below next_offset is counted" is always true."""
    next_offsets: dict[int, int] = {}
    for record in records:
        next_offsets[record.partition] = max(next_offsets.get(record.partition, 0), record.offset + 1)
    async with AsyncSessionLocal() as session:
        offsets = FailureCountOffsetRepository(session)
        counted_up_to = await offsets.lock(org_id=org_id, kind=INGESTION, topic=topic, partitions=next_offsets)
        fresh = [r for r in records if r.offset >= counted_up_to.get(r.partition, 0)]
        per_pipeline, unattributed = count_batch_by_pipeline(fresh, oldest=oldest, attribution=attribution)
        for pipeline_id, counts in per_pipeline.items():
            await add_ingestion_counts(session, org_id=org_id, pipeline_id=pipeline_id, counts=counts)
        await offsets.advance(org_id=org_id, kind=INGESTION, topic=topic, next_offsets=next_offsets)
        await session.commit()
    counted = sum(sum(counts.values()) for counts in per_pipeline.values())
    return BatchOutcome(counted=counted, replayed=len(records) - len(fresh), unattributed=unattributed)


async def forget_offsets_past_topic_end(*, topic: str, end_offsets: dict[int, int]) -> list[int]:
    """A topic that was deleted and recreated starts again at offset 0. A stored offset BEYOND the topic's end can only mean that, and keeping
    it would skip every new record until the topic caught up. Drops those partitions' rows; returns which."""
    async with AsyncSessionLocal() as session:
        offsets = FailureCountOffsetRepository(session)
        stored = await offsets.stored(kind=INGESTION, topic=topic)
        stale = sorted(p for p, counted_up_to in stored.items() if p in end_offsets and counted_up_to > end_offsets[p])
        if stale:
            await offsets.forget(kind=INGESTION, topic=topic, partitions=stale)
            await session.commit()
    return stale


def _topic_end_offsets(consumer, topic: str) -> dict[int, int]:
    """Blocking. {partition: end offset} for every partition of `topic`."""
    from kafka import TopicPartition

    partitions = consumer.partitions_for_topic(topic) or set()
    ends = consumer.end_offsets([TopicPartition(topic, p) for p in partitions]) if partitions else {}
    return {tp.partition: int(end) for tp, end in ends.items()}


def _open_consumer(topic: str, group_id: str):
    """Blocking. None if the topic doesn't exist (yet)."""
    from kafka import KafkaConsumer

    probe = KafkaConsumer(bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-dlq-count-probe")
    try:
        if not probe.partitions_for_topic(topic):
            return None
    finally:
        probe.close()
    return KafkaConsumer(
        topic, bootstrap_servers=settings.kafka_bootstrap, group_id=group_id,
        enable_auto_commit=False, auto_offset_reset="earliest",
        # Raw bytes both ways: never deserialize a record body we don't read.
        key_deserializer=None, value_deserializer=None,
    )


class DlqCounterWorker:
    """One consumer for one DLQ topic. `pipeline_id` is the topic's first pipeline, and only names the consumer group
    (`inai-dlqcount-<pipeline_id>`); it does NOT decide whose failures a record is.
    `attribution` does, record by record, and is replaced as pipelines come and go (DlqCounterManager.reconcile_once)."""

    def __init__(self, *, org_id: str, pipeline_id: str, topic: str, attribution: DlqAttribution):
        self.org_id = org_id
        self.pipeline_id = pipeline_id
        self.topic = topic
        self.attribution = attribution
        self.topic_missing = False
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"dlq-count-{self.pipeline_id}")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        group_id = f"inai-dlqcount-{self.pipeline_id}"
        consumer = await _in_dlq_pool(_open_consumer, self.topic, group_id)
        if consumer is None:
            self.topic_missing = True
            return
        logger.info("dlq_count.started", pipeline_id=self.pipeline_id, topic=self.topic)
        try:
            await self._forget_offsets_if_topic_was_recreated(consumer)
            while not self._stopping:
                batches = await _in_dlq_pool(consumer.poll, timeout_ms=_POLL_TIMEOUT_MS, max_records=_BATCH_SIZE)
                records = [record for batch in batches.values() for record in batch]
                if not records:
                    continue
                oldest = datetime.now(UTC) - timedelta(days=settings.failure_count_retention_days)
                try:
                    outcome = await record_batch_once(
                        org_id=self.org_id, topic=self.topic, records=records, oldest=oldest, attribution=self.attribution,
                    )
                except Exception:
                    # Nothing was written (the counts and the offsets are one transaction) and the Kafka offset is not
                    # committed, so the batch is redelivered after the consumer is rebuilt from its last committed offset.
                    logger.warning("dlq_count.write_failed", pipeline_id=self.pipeline_id, exc_info=True)
                    return
                if outcome.unattributed:
                    record_dlq_unattributed(outcome.unattributed)
                    logger.warning(
                        "dlq_count.records_not_attributed", pipeline_id=self.pipeline_id, topic=self.topic, records=outcome.unattributed,
                        pipelines_on_topic=len(set(self.attribution.by_connector.values())),
                    )
                if outcome.replayed:
                    logger.info(
                        "dlq_count.replay_skipped", pipeline_id=self.pipeline_id, topic=self.topic,
                        replayed=outcome.replayed, counted=outcome.counted,
                    )
                # Only now: if the process dies between the database commit above and this commit, Kafka redelivers the
                # batch and record_batch_once recognises it as already counted.
                await _in_dlq_pool(consumer.commit)
        finally:
            await _in_dlq_pool(consumer.close)
            logger.info("dlq_count.stopped", pipeline_id=self.pipeline_id)


    async def _forget_offsets_if_topic_was_recreated(self, consumer) -> None:
        """See forget_offsets_past_topic_end. A failure here must not stop counting: the worst case is the pre-NEW-03 behaviour for one run."""
        try:
            ends = await _in_dlq_pool(_topic_end_offsets, consumer, self.topic)
            stale = await forget_offsets_past_topic_end(topic=self.topic, end_offsets=ends)
        except Exception:
            logger.warning("dlq_count.recreated_topic_check_failed", pipeline_id=self.pipeline_id, exc_info=True)
            return
        if stale:
            logger.warning(
                "dlq_count.topic_recreated_offsets_reset", pipeline_id=self.pipeline_id, topic=self.topic, partitions=stale,
            )


class DlqCounterManager:
    """One worker per DLQ TOPIC, not per pipeline: counting a shared topic once per pipeline would count every record several times. The
    worker attributes each record to the pipeline that wrote it (NEW-04), so its set of pipelines is refreshed on every tick — a pipeline
    added to or removed from a shared topic changes who gets the counts, without restarting the consumer."""

    def __init__(self) -> None:
        self._workers: dict[str, DlqCounterWorker] = {}  # keyed by topic
        self._retry_after: dict[str, float] = {}
        self._last_prune = 0.0

    async def reconcile_once(self) -> None:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    # Only a SINK pipeline with a dead-letter policy writes a DLQ topic (Kafka Connect gives a
                    # source connector none — app/core/error_policies.py).
                    select(Pipeline)
                    .where(
                        Pipeline.status != "draft",
                        func.upper(Pipeline.direction) == "SINK",
                        Pipeline.error_handling_policy.in_(sorted(DLQ_POLICIES)),
                    )
                    .order_by(Pipeline.created_at, Pipeline.id)
                )
            ).scalars().all()
            pipelines = [
                {
                    "id": row.id, "org_id": row.org_id, "connector_name": row.connector_name, "direction": row.direction,
                    "error_handling_policy": row.error_handling_policy, "domain": row.domain, "source_system": row.source_system,
                    "entity": row.entity, "version": row.version,
                }
                for row in rows
            ]
        # topic -> EVERY pipeline writing it, oldest first. The oldest only names the consumer group (see DlqCounterWorker).
        wanted = pipelines_by_dlq_topic(pipelines)

        now = time.monotonic()
        for topic, worker in list(self._workers.items()):
            if topic not in wanted or wanted[topic][0]["id"] != worker.pipeline_id:
                await self._workers.pop(topic).stop()
            elif worker.done:
                # Finished on its own: topic not created yet, or a failed write — rebuild after a pause.
                self._workers.pop(topic)
                self._retry_after[topic] = now + (_MISSING_TOPIC_RETRY_SECONDS if worker.topic_missing else 5.0)
            else:
                worker.attribution = DlqAttribution(wanted[topic])        # who writes this topic NOW

        for topic, topic_pipelines in wanted.items():
            if topic in self._workers or self._retry_after.get(topic, 0.0) > now:
                continue
            owner = topic_pipelines[0]
            worker = DlqCounterWorker(
                org_id=owner["org_id"], pipeline_id=owner["id"], topic=topic, attribution=DlqAttribution(topic_pipelines),
            )
            worker.start()
            self._workers[topic] = worker

        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                await prune_old_buckets()
            except Exception:
                logger.warning("dlq_count.prune_failed", exc_info=True)

    async def run_forever(self, interval_seconds: float = 10.0) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("dlq_count.reconcile_failed")
            await asyncio.sleep(interval_seconds)

    async def stop_all(self) -> None:
        from app.core.shutdown import stop_all_isolated

        workers = [(f"dlq-count:{topic}", self._workers.pop(topic).stop) for topic in list(self._workers)]
        await stop_all_isolated(workers, timeout=settings.shutdown_step_timeout_seconds)


dlq_counter_manager = DlqCounterManager()
