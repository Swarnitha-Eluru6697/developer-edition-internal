"""
Real-time quality evaluation against live pipeline data, in one of two modes (NEW-01; app/services/quality_mode.py):

    OBSERVE (default)   observe only — everything below up to "ENFORCE" describes it.
    ENFORCE             the worker is a gate: a passing record is written to the pipeline's PREPARED topic, a failing record to
                        quarantine, and consumers read the prepared topic instead of the data topic. See "ENFORCE" below.

Real-time quality observation against live pipeline data.

Attaching a quality policy to a pipeline previously only ran a one-time
sample check at creation time (see PipelineService.autorun_quality_from_source)
— it had zero visibility into the actual, ongoing Kafka Connect data flow.

This module is what actually observes a policy continuously, for BOTH source
and sink pipelines: for every running pipeline with a `quality_policy_id`
attached (and whose own zone isn't "quarantine" — see below), a
QualityStreamWorker consumes the pipeline's own real topic — exactly the
topic Kafka Connect itself writes to (source) or reads from (sink); see
connect_config.py's `_build_connector`/`_sink_topic`, which never redirect
this regardless of quality_policy_id — evaluates each record against the
policy, and records the outcome to dc_quality_stream_stats
(QualityStreamStatsRepository.record_outcome, an atomic running total per
pipeline). It never alters what Kafka Connect reads or writes and never
touches the pipeline's own topic: the FULL data set stays on that topic,
failing records included, and reaches every sink exactly as before.

A record that FAILS the policy is additionally COPIED to the pipeline's
validation quarantine topic,
<org>-quarantine-<domain>-<source_system>-<entity>-validation-<version>
(mode validation_sink for a sink pipeline; app/services/topic_naming.py), when the policy says on_failure =
route_to_quarantine with quarantine_sink = kafka — the same destination the
manual Execute path (QualityService.execute_policy) already uses. That topic
holds ONLY DataConnect quality-rule failures; Kafka Connect's own failures
(bad serialization, converter errors, malformed source messages) go to the
separate <...>.connector.<version> dead-letter topic, so monitoring can tell
the two failure classes apart (GAP-03). A bounded sample of failures is also
kept in dc_quality_stream_stats.sample_failures (see
QualityStreamStatsRepository.append_sample_failure) for the Quality page.

ENFORCE. The same loop, with one more step for a record that PASSES: it is written (acknowledged) to
<org>-prepared-<domain>-<source_system>-<entity>-<mode>-<version> — the pipeline's own name with the zone "prepared" — as the very same message
Kafka Connect wrote (Connect envelope and key intact). A FAILING record goes to quarantine and is never written to the prepared topic. A record
is therefore in exactly one place: prepared XOR quarantine. The data topic itself is still not touched: it keeps every record, which is why a
sink that must only see valid data reads the prepared topic (connect_config._sink_topic) and may not read the data topic
(pipeline_spec_validator). In ENFORCE a failing record is quarantined even if the policy was later edited to something else: the only
alternatives are dropping it or forwarding it, and either would break the guarantee. If the worker is down the prepared topic simply stops
growing — a stalled sink, never an invalid record delivered.

Delivery is at-least-once, and a record's offset is committed ONLY once the record is either
fully handled or safely dead-lettered (BUG-03):

    handled              stats written, and (for a failing record) its quarantine copy produced and
                         acknowledged, or (ENFORCE, a passing record) its prepared copy produced and
                         acknowledged                                         -> commit
    a step fails         retried with backoff (settings.quality_stream_max_handle_attempts)
                                                                              -> no commit
    retries exhausted    the record is written to the WORKER dead-letter topic
                         <org>-quarantine-<domain>-<source_system>-<entity>-quality_worker-<version>
                         and that write is acknowledged                       -> commit
    dead-letter topic    keep retrying with backoff, forever                    -> never commit
    unavailable          (the partition stalls: backpressure, not silent loss)

Each step is retried on its own, so the stats write is not repeated when only the quarantine
produce failed (no double count). A crash between a step and the commit can still repeat a record
(at-least-once), e.g. write the same failing record to the quarantine topic twice.

What a crash between the steps and the commit does (NEW-05). The worker records a record's statistics and publishes its quarantine / prepared
copy BEFORE it commits the Kafka offset, so a crash in between hands the same record over again after the restart. Two different guarantees apply:

    statistics           EXACTLY ONCE. The claim on the record's (pipeline, topic, partition, offset) is written in the same transaction as the
                         totals, the failure sample and the per-rule counts (QualityStreamOffset), so a replay is recognised and counts nothing
                         (`dataconnect_quality_replays_skipped_total`, log `quality_stream.replay_not_recounted`).
    published messages   AT LEAST ONCE. Whether a quarantine / prepared / dead-letter copy reached Kafka before the crash cannot be known, so the
                         replay publishes it again. Every such message carries the header `inai.dedupe_key` =
                         `<pipeline id>:<topic>:<partition>:<offset>` — the same on every delivery of the same source record — and the quarantine
                         and dead-letter payloads carry `event_id` (`<topic>:<partition>:<offset>`). A consumer that needs each record once
                         discards a message whose dedupe key it has already seen. (This is the same header the batch jobs use for their chunks.)

Stalling is correct but must not be silent (GAP-03). While a worker is stuck on a record, it counts quality retries and
dead-letter publish failures (app/core/metrics.py), is visible in the blocked-partitions and oldest-uncommitted-age gauges, and keeps
a heartbeat on its pipeline's runtime-state row; once stuck longer than settings.quality_stream_degraded_after_seconds the reconciler
reports the pipeline DEGRADED with the reason (app/services/quality_worker_health.py). None of this is ever a reason to commit: the
commit happens only after a record was handled or its dead-letter write was acknowledged, exactly as above.

The worker dead-letter topic is the THIRD failure class, distinct from a rule failure (validation
topic) and a Kafka Connect failure (connector topic) — see
topic_naming.build_worker_quarantine_topic_name. It holds records this worker could not process
(unreadable bytes, an evaluation error, the stats database or the quarantine produce staying down),
with the reason, the error, whether the stats were already recorded, and the original record, so
they can be replayed once whatever broke is fixed.

- A SINK pipeline's input is `connector_config["topics"]`: EVERY topic it was
  configured to read (e.g. "SOURCE TOPIC TO READ" in the UI) — one consumer, one
  subscription, all of them (NEW-02). Each record carries the topic, partition and
  offset it was read from into whatever this worker writes about it.
- A SOURCE pipeline's input is `pipeline["topic_name"]` itself — the same
  topic its own connector writes every record to.

A pipeline whose own configured zone IS "quarantine" is left alone entirely
(no worker) — that zone isn't part of this feature.

QualityStreamManager periodically reconciles which pipelines currently need
a worker (running, quality_policy_id set, zone != "quarantine") against
which workers are actually alive, the same self-healing pattern
app/services/runtime_reconciler.py already uses for Kafka Connect state —
so a worker survives an app restart (it's recreated on the next tick) and a
pipeline that's paused or loses its policy gets its worker stopped
automatically, without every lifecycle endpoint needing to know about this.
"""

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import settings
from app.core.metrics import (
    record_quality_dlq_failure,
    record_quality_replay_skipped,
    record_quality_retry,
)
from app.db.session import AsyncSessionLocal
from app.models.pipeline import Pipeline
from app.repositories.dataconnect import (
    PipelineRuntimeStateRepository,
    QualityStreamOffsetRepository,
    QualityStreamStatsRepository,
)
from app.services.failure_counters import record_validation_failure
from app.services.kafka_admin import ensure_simple_topic, trim_consumed_records
from app.services.kafka_runtime import KafkaRecord, NativeKafkaConsumer, get_kafka_producer
from app.services.pipeline_topics import consumed_topics, is_sink_pipeline
from app.services.quality import build_quarantine_envelope, evaluate_record
from app.services.quality_mode import (
    is_enforcing,
    is_sink_handoff,
    prepared_topic_for,
    quality_consumer_group,
)
from app.services.quality_worker_health import quality_worker_health
from app.services.quarantine_sink import get_quarantine_sink
from app.services.safe_error import to_safe_error
from app.services.topic_naming import (
    build_worker_quarantine_topic_name,
    validation_quarantine_topic_for,
)
from app.services.ucs_quality_sync import emit_stream_quality

logger = structlog.get_logger(__name__)

_DEAD_LETTER_MIN_RETRY_DELAY_SECONDS = 0.05


def _unwrap_connect_envelope(value: dict[str, Any]) -> dict[str, Any]:
    """Kafka Connect's JSON converter (schemas.enable=true — the platform
    default; see topic_policy.schema_strategy_converter_config) wraps every
    record as {"schema": ..., "payload": {...}}. Quality rules are defined
    against the actual field names in the data, so evaluate `payload`; a
    record without that envelope (schemas disabled, or produced directly by
    something else) is used as-is. Exact decimals in the payload are decoded
    with the schema (see _decode_logical_types)."""
    if "schema" in value and "payload" in value:
        return _decode_logical_types(value["schema"], value["payload"]) or {}
    return value


_CONNECT_DECIMAL = "org.apache.kafka.connect.data.Decimal"
_DEBEZIUM_VARIABLE_DECIMAL = "io.debezium.data.VariableScaleDecimal"


def _decimal(raw: str, scale: int) -> int | float | None:
    """A Connect decimal as JSON carries it: base64 of the unscaled value's big-endian two's complement bytes. None if it is not that."""
    try:
        unscaled = int.from_bytes(base64.b64decode(raw, validate=True), "big", signed=True)
    except (binascii.Error, ValueError):
        return None
    number = Decimal(unscaled).scaleb(-scale)
    return int(number) if scale <= 0 else float(number)


def _decode_logical_types(schema: Any, value: Any) -> Any:
    """The record as the rules read it: an exact decimal (MySQL's DECIMAL from any JDBC source; every NUMERIC/DECIMAL from a "v2" pipeline,
    see connect_config.record_format_of) arrives as base64 text such as "ITcg", which no range rule can compare (every record failed the
    2026-11-02 MySQL test's price rules). Decoded to a number using the field's schema: scale 0 → int, otherwise float. Only this copy
    changes: the prepared topic still gets the record exactly as Kafka Connect wrote it, so the sink writes the exact value."""
    if not isinstance(schema, dict) or value is None:
        return value
    name = schema.get("name")
    if name == _CONNECT_DECIMAL and isinstance(value, str):
        decoded = _decimal(value, int((schema.get("parameters") or {}).get("scale") or 0))
        return value if decoded is None else decoded
    if name == _DEBEZIUM_VARIABLE_DECIMAL and isinstance(value, dict) and isinstance(value.get("value"), str):
        decoded = _decimal(value["value"], int(value.get("scale") or 0))
        return value if decoded is None else decoded
    if schema.get("type") == "struct" and isinstance(value, dict):
        fields = {f.get("field"): f for f in schema.get("fields") or [] if isinstance(f, dict)}
        return {k: _decode_logical_types(fields.get(k), v) for k, v in value.items()}
    if schema.get("type") == "array" and isinstance(value, list):
        return [_decode_logical_types(schema.get("items"), v) for v in value]
    return value


def _decode_record(record: KafkaRecord) -> dict[str, Any] | None:
    """Returns the raw, still-enveloped message value. _unwrap_connect_envelope
    strips it transiently, just for evaluation — this function itself never
    needs to, since nothing here ever re-produces the record anywhere."""
    raw = (record.value or {}).get("_raw")
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _input_topics(pipeline: dict[str, Any]) -> list[str]:
    """EVERY topic this pipeline's worker must consume — exactly the topics Kafka Connect itself uses for this pipeline (see this module's
    docstring): a source's own topic, or ALL of a sink's input topics (NEW-02). Empty for a pipeline whose own zone is "quarantine" (out of
    scope for now).

    A sink can read several topics (`connector_config["topics"]` is comma-separated or a list). This used to return that setting as ONE
    string and hand it to the consumer as one topic name, which Kafka rejects as illegal on every poll — so a sink with two or more input
    topics had none of them checked, and the worker only logged the error and retried."""
    if pipeline.get("zone") == "quarantine":
        return []
    return consumed_topics(pipeline)


@dataclass(frozen=True)
class _Origin:
    """Where a record came from. With several input topics, "the pipeline's topic" no longer says which one a failing record was read from."""

    topic: str
    partition: int | None = None
    offset: int | None = None

    @property
    def event_id(self) -> str | None:
        """The source record's stable identity, `<topic>:<partition>:<offset>` — the same on every delivery of that record. None when the
        position is not known (a record handed to the worker directly, not read from Kafka)."""
        if self.partition is None or self.offset is None:
            return None
        return f"{self.topic}:{self.partition}:{self.offset}"

    def dedupe_key(self, pipeline_id: str) -> str | None:
        """What a consumer of this worker's output deduplicates on: the event, per pipeline (two pipelines can read one topic, and each
        writes its own quarantine / prepared / dead-letter message for the same source record)."""
        event_id = self.event_id
        return f"{pipeline_id}:{event_id}" if event_id else None

    def headers(self) -> dict[str, str]:
        out = {"inai.source_topic": self.topic}
        if self.partition is not None:
            out["inai.source_partition"] = str(self.partition)
        if self.offset is not None:
            out["inai.source_offset"] = str(self.offset)
        return out


class QualityStreamWorker:
    """Owns one Kafka consumer + one background task for a single pipeline.
    Not shared, not reused after stop() — QualityStreamManager builds a
    fresh one whenever it decides a pipeline needs (re)enforcement."""

    def __init__(self, *, org_id: str, pipeline: dict[str, Any], policy: dict[str, Any]):
        self.org_id = org_id
        self.pipeline = pipeline
        self.policy = policy
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._quarantine_topic_ready = False
        self._dead_letter_topic_ready = False
        self._prepared_topic_ready = False
        self._enforcing = is_enforcing(pipeline)
        self._prepared_topic = prepared_topic_for(pipeline)
        self._health_key = f"{org_id}:{pipeline['id']}"
        self._connector_type = (str(pipeline.get("direction") or "").lower() or None)
        self._consumer: Any = None
        # The totals last reported to UCS, and when (time.monotonic) — see _report_to_ucs.
        self._ucs_reported: tuple[int, int] | None = None
        self._ucs_reported_at = 0.0
        self.spec = worker_spec(pipeline, policy)
        # An ENFORCE sink's hand-off topic: what its connector has written is deleted from it (_trim_handoff).
        self._handoff = self._prepared_topic if is_sink_handoff(self._prepared_topic) else None
        self._trimmed: dict[int, int] = {}
        self._trimmed_at = 0.0

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name=f"quality-stream-{self.pipeline['id']}"
        )

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
        source_topics = _input_topics(self.pipeline)
        if not source_topics:
            logger.warning("quality_stream.no_source_topic", pipeline_id=self.pipeline["id"])
            return

        consumer = NativeKafkaConsumer(
            bootstrap_servers=settings.kafka_bootstrap,
            topics=source_topics,
            group_id=quality_consumer_group({**self.pipeline, "org_id": self.org_id}),
        )
        self._consumer = consumer
        logger.info(
            "quality_stream.started", pipeline_id=self.pipeline["id"],
            source_topics=source_topics, policy_id=self.policy["id"],
            quality_mode="ENFORCE" if self._enforcing else "OBSERVE", prepared_topic=self._prepared_topic,
        )
        if self._enforcing and not self._routes_to_kafka_quarantine():
            logger.warning(
                "quality_stream.enforce_policy_not_routing_to_quarantine", pipeline_id=self.pipeline["id"],
                on_failure=self.policy.get("on_failure"),
            )
        try:
            await self._forget_offsets_past_topic_end(consumer)
            while not self._stopping:
                try:
                    record = await consumer.poll()
                except Exception:
                    logger.exception("quality_stream.poll_failed", pipeline_id=self.pipeline["id"])
                    await asyncio.sleep(1.0)
                    continue
                if record is None:
                    await self._report_to_ucs()   # idle: the totals of the last burst still reach UCS
                    await self._trim_handoff()
                    continue
                # Returns only when the record has been handled or its dead-letter write was
                # acknowledged. An exception (e.g. cancellation on shutdown) skips the commit, so
                # the record is redelivered rather than lost.
                quality_worker_health.begin(self._health_key)
                await self._process(record)
                await consumer.commit()
                await self._record_committed()
                await self._report_to_ucs()
                await self._trim_handoff()
        finally:
            await self._record_committed()   # shutdown mid-stall: do not leave this pipeline reported as stuck
            await consumer.close()
            logger.info("quality_stream.stopped", pipeline_id=self.pipeline["id"])

    async def _report_to_ucs(self) -> None:
        """The pipeline's running totals (dc_quality_stream_stats) as quality assertions in UCS (ucs_quality_sync.emit_stream_quality), at
        most once per ucs_quality_stream_sync_interval_seconds and only when they changed — one per topic the totals describe:
          * the topic it checks: passed / total (only with a single input: with several, the totals cover them together);
          * the validation quarantine topic it copies failures to: every record there failed, so 0% — reported as "warn", not "fail": a
            quarantine topic holding failures is it working, and UCS's default quality-breach alert fires on "fail";
          * in ENFORCE, the prepared topic it forwards passing records to: every record there passed, so 100%.
        Best effort: a failure is logged, never raised — it must not stop the worker or hold back the next record."""
        if not settings.ucs_quality_sync_enabled:
            return
        now = time.monotonic()
        if now - self._ucs_reported_at < settings.ucs_quality_stream_sync_interval_seconds:
            return
        self._ucs_reported_at = now
        try:
            async with AsyncSessionLocal() as session:
                stats = await QualityStreamStatsRepository(session).get_by_pipeline_id(self.org_id, self.pipeline["id"])
            if stats is None or (stats.total_count, stats.failed_count) == self._ucs_reported:
                return
            total, failed = stats.total_count, stats.failed_count
            reports: list[dict[str, Any]] = []
            topics = _input_topics(self.pipeline)
            if len(topics) == 1:
                report: dict[str, Any] = {"topic_name": topics[0], "total": total, "failed": failed}
                if is_sink_pipeline(self.pipeline):
                    # A sink checks a topic ANOTHER pipeline produces — and that producer may report the same topic under the same policy
                    # name (an ENFORCE source reports its prepared topic). One UCS check per (topic, name), so the sink's gets its own name.
                    report["check_name"] = f"{self.policy.get('name') or self.policy['id']} · {self.pipeline.get('name') or self.pipeline['id']}"
                reports.append(report)
            if self._enforcing or self._routes_to_kafka_quarantine():
                reports.append({"topic_name": self._validation_quarantine_topic(), "total": failed, "failed": failed,
                                "status": "warn", "run_suffix": "-q"})
            if self._prepared_topic and not self._handoff:   # a sink's hand-off is internal: not in the catalog, so not scored
                reports.append({"topic_name": self._prepared_topic, "total": total - failed, "failed": 0, "run_suffix": "-p"})
            for report in reports:
                await emit_stream_quality(self.org_id, self.pipeline, self.policy, **report)
            self._ucs_reported = (total, failed)
        except Exception:  # noqa: BLE001 — reporting to UCS is best effort
            logger.warning("quality_stream.ucs_report_failed", pipeline_id=self.pipeline["id"], exc_info=True)

    async def _trim_handoff(self) -> None:
        """An ENFORCE sink keeps passing records in Kafka only until its own connector has written them: at most every
        sink_handoff_trim_interval_seconds, the hand-off topic's records below the connector's committed offset are deleted. The connector
        being down loses nothing — nothing it has not committed is deleted (and the topic keeps its normal retention as a backstop). Best
        effort: a failure is logged, never raised."""
        if not self._handoff:
            return
        now = time.monotonic()
        if now - self._trimmed_at < settings.sink_handoff_trim_interval_seconds:
            return
        self._trimmed_at = now
        try:
            self._trimmed |= await trim_consumed_records(self._handoff, f"connect-{self.pipeline['connector_name']}", already=self._trimmed)
        except Exception:  # noqa: BLE001
            logger.warning("quality_stream.handoff_trim_failed", pipeline_id=self.pipeline["id"], exc_info=True)

    def _validation_quarantine_topic(self) -> str:
        pipeline = self.pipeline
        return validation_quarantine_topic_for(pipeline)

    async def _forget_offsets_past_topic_end(self, consumer: Any) -> None:
        """A topic that was deleted and recreated starts again at offset 0. A stored offset BEYOND a topic's end can only mean that, and keeping
        it would leave every new record uncounted until the topic caught up. Drops those rows when the worker starts. A failure here must not
        stop the worker: the worst case is the pre-NEW-05 behaviour (a replay is counted again) for the run."""
        end_offsets = getattr(consumer, "end_offsets", None)
        if not callable(end_offsets):
            return
        try:
            ends = await end_offsets()
            async with AsyncSessionLocal() as session:
                repo = QualityStreamOffsetRepository(session)
                stored = await repo.stored(pipeline_id=self.pipeline["id"], topics={t for t, _ in ends})
                stale: dict[str, list[int]] = {}
                for (topic, partition), counted_up_to in stored.items():
                    if (topic, partition) in ends and counted_up_to > ends[(topic, partition)]:
                        stale.setdefault(topic, []).append(partition)
                for topic, partitions in stale.items():
                    await repo.forget(pipeline_id=self.pipeline["id"], topic=topic, partitions=partitions)
                if stale:
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("quality_stream.recreated_topic_check_failed", pipeline_id=self.pipeline["id"], exc_info=True)
            return
        if stale:
            logger.warning("quality_stream.topic_recreated_offsets_reset", pipeline_id=self.pipeline["id"], topics=stale)

    async def _process(self, record: KafkaRecord) -> None:
        origin = _Origin(topic=record.topic, partition=record.partition, offset=record.offset)
        if "_raw" in (record.value or {}) and record.value["_raw"] is None:
            await self._pass_tombstone(key=record.key, origin=origin)
            return
        envelope = _decode_record(record)
        if envelope is None:
            raw = (record.value or {}).get("_raw")
            await self._dead_letter(
                reason="undecodable_record", error="The record is not a JSON object.",
                original_record=raw, key=record.key, stats_recorded=False, origin=origin,
            )
            return
        await self._handle(envelope=envelope, key=record.key, origin=origin)

    async def _handle(
        self, *, envelope: dict[str, Any], key: str | None = None, origin: _Origin | None = None,
    ) -> None:
        """Evaluate one record, record the outcome, and — for a failing record — copy it to the
        validation quarantine topic; in ENFORCE mode a passing record is written to the prepared topic. Never writes to the pipeline's
        own data topic: this worker doesn't own the data flow, Kafka Connect does (see this module's docstring).

        Returns only when the record is fully handled or dead-lettered; it never gives up on a
        record silently (BUG-03). Each step is retried on its own.

        `origin` is where the record was read from (topic, partition, offset). It is carried into every record this writes — quarantine,
        dead letter, the failure sample and the prepared copy — so with several input topics a failure points at one exact message. Absent
        (a direct call), the pipeline's first input topic is used."""
        origin = origin or self._default_origin()
        payload = _unwrap_connect_envelope(envelope)
        try:
            result = evaluate_record(payload, self.policy)
        except Exception as exc:  # noqa: BLE001 - an evaluation bug on this record: retrying can't help, dead-letter it
            await self._dead_letter(
                reason="evaluation_error", error=exc, original_record=payload, key=key, stats_recorded=False, origin=origin,
            )
            return

        counted: list[bool | None] = []

        async def _stats() -> None:
            counted.append(await self._record_outcome(
                valid=result["valid"], failed_rules=result["failed_rules"], original_record=payload, origin=origin,
            ))

        try:
            await self._with_retry("stats", _stats)
            if counted and counted[-1] is False:
                # A replay: counted already, so not again. The record is still handled below — whether its quarantine / prepared copy reached
                # Kafka before the crash cannot be known, so it is published again with the SAME dedupe key.
                record_quality_replay_skipped(connector_type=self._connector_type)
                logger.info(
                    "quality_stream.replay_not_recounted", pipeline_id=self.pipeline["id"], event_id=origin.event_id,
                )
        except Exception as exc:  # noqa: BLE001
            await self._dead_letter(
                reason="stats_write_failed", error=exc, original_record=payload, key=key,
                failed_rules=result["failed_rules"], stats_recorded=False, origin=origin,
            )
            return

        if not result["valid"]:
            try:
                await self._with_retry(
                    "quarantine",
                    lambda: self._quarantine(failed_rules=result["failed_rules"], original_record=payload, origin=origin),
                )
            except Exception as exc:  # noqa: BLE001
                # The stats are already counted — a replay must not count this record twice.
                await self._dead_letter(
                    reason="quarantine_produce_failed", error=exc, original_record=payload, key=key,
                    failed_rules=result["failed_rules"], stats_recorded=True, origin=origin,
                )
            return

        if self._enforcing:
            try:
                await self._with_retry(
                    "prepared", lambda: self._forward_to_prepared(envelope=envelope, key=key, origin=origin),
                )
            except Exception as exc:  # noqa: BLE001
                # Counted as passed already (stats_recorded): a replay of this dead letter must forward it, not count it again. The
                # FULL original message (Connect envelope included) is kept, so a replay can write exactly what Connect wrote.
                await self._dead_letter(
                    reason="prepared_produce_failed", error=exc, original_record=envelope, key=key, stats_recorded=True,
                    origin=origin,
                )

    async def _pass_tombstone(self, *, key: str | None, origin: _Origin) -> None:
        """A tombstone (a null value under a row's key) is how a CDC topic says "this row was deleted" (connect_config._dbz_unwrap_v2). There
        is nothing to evaluate and nothing to count. In ENFORCE it is forwarded, so the sink behind the prepared topic deletes the row too;
        in OBSERVE the sink reads the data topic itself and this worker has nothing to do. (Dead-lettering it as unreadable, as before,
        lost every delete.)"""
        if not self._enforcing:
            return
        try:
            await self._with_retry("prepared", lambda: self._forward_to_prepared(envelope=None, key=key, origin=origin))
        except Exception as exc:  # noqa: BLE001
            await self._dead_letter(
                reason="prepared_produce_failed", error=exc, original_record=None, key=key, stats_recorded=True, origin=origin,
            )

    def _default_origin(self) -> _Origin:
        """The origin to report when the caller did not say where a record came from: the pipeline's first input topic."""
        topics = _input_topics(self.pipeline)
        return _Origin(topic=topics[0] if topics else str(self.pipeline.get("topic_name") or ""))

    async def _with_retry(self, step: str, action: Callable[[], Awaitable[None]]) -> None:
        """Runs `action` up to settings.quality_stream_max_handle_attempts times, sleeping
        settings.quality_stream_handle_backoff_seconds * attempt between tries. Raises the last
        error once exhausted — the caller then dead-letters; nothing is swallowed here."""
        attempts = settings.quality_stream_max_handle_attempts
        backoff = settings.quality_stream_handle_backoff_seconds
        for attempt in range(1, attempts + 1):
            try:
                await action()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= attempts:
                    logger.error(
                        f"quality_stream.{step}_exhausted", pipeline_id=self.pipeline["id"], attempts=attempt,
                        exc_info=True,
                    )
                    raise
                logger.warning(
                    f"quality_stream.{step}_retry", pipeline_id=self.pipeline["id"], attempt=attempt,
                    max_attempts=attempts, exc_info=True,
                )
                record_quality_retry(connector_type=self._connector_type, step=step)
                await self._note_blocked()
                await self._sleep_blocked(backoff * attempt)

    # ── GAP-03: visibility while stuck. Nothing here influences whether a record is committed. ──────────────────────

    async def _note_blocked(self) -> None:
        """This record failed to move forward: count it as blocked and (throttled) tell the database."""
        count_fn = getattr(self._consumer, "assigned_partition_count", None)
        partitions = (count_fn() if callable(count_fn) else 1) or 1
        quality_worker_health.mark_blocked(self._health_key, partitions=partitions)
        await self._publish_blocked()

    async def _sleep_blocked(self, delay: float) -> None:
        """Sleeps `delay`, but in slices no longer than the heartbeat interval, so a long dead-letter backoff does not let the
        heartbeat go stale and make a stuck worker look dead."""
        beat = max(0.01, settings.quality_stream_blocked_heartbeat_seconds)
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(remaining, beat))
            await self._publish_blocked()

    async def _publish_blocked(self) -> None:
        state = quality_worker_health.get(self._health_key)
        if state is None or state.blocked_since is None:
            return
        now = time.monotonic()
        if state.published and now - state.last_published < settings.quality_stream_blocked_heartbeat_seconds:
            return
        state.last_published = now   # also on failure: a database outage must not be hammered every loop
        wall_now = datetime.now(UTC)
        try:
            await self._write_blocked_state(wall_now - timedelta(seconds=now - state.blocked_since), wall_now)
            state.published = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("quality_stream.blocked_state_write_failed", pipeline_id=self.pipeline["id"], exc_info=True)

    async def _record_committed(self) -> None:
        """The record committed (or the worker is stopping): nothing is stuck any more."""
        if not quality_worker_health.end(self._health_key):
            return
        try:
            await self._clear_blocked_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A stale heartbeat stops counting by itself after a few missed beats (quality_worker_health.quality_stall).
            logger.warning("quality_stream.blocked_state_clear_failed", pipeline_id=self.pipeline["id"], exc_info=True)

    async def _write_blocked_state(self, since: datetime, heartbeat_at: datetime) -> None:
        async with AsyncSessionLocal() as session:
            await PipelineRuntimeStateRepository(session).mark_quality_blocked(
                self.pipeline["id"], since=since, heartbeat_at=heartbeat_at,
            )
            await session.commit()

    async def _clear_blocked_state(self) -> None:
        async with AsyncSessionLocal() as session:
            await PipelineRuntimeStateRepository(session).clear_quality_blocked(self.pipeline["id"])
            await session.commit()

    def _routes_to_kafka_quarantine(self) -> bool:
        return (
            self.policy.get("on_failure", "route_to_quarantine") == "route_to_quarantine"
            and self.policy.get("quarantine_sink", "kafka") == "kafka"
        )

    async def _quarantine(
        self, *, failed_rules: list[str], original_record: dict[str, Any], origin: _Origin | None = None,
    ) -> None:
        """Copy one failing record to <org>-quarantine-<domain>-<source_system>-<entity>-validation
        .<version> and wait for the broker's acknowledgement. The topic is created explicitly the
        first time (ensure_simple_topic) rather than left to the broker's implicit auto-create,
        which is dev-only. RAISES on failure — _handle retries it and, once exhausted, dead-letters
        the record instead of dropping the copy."""
        if not self._enforcing and not self._routes_to_kafka_quarantine():
            return
        pipeline = self.pipeline
        topic = self._validation_quarantine_topic()
        origin = origin or self._default_origin()
        failure = build_quarantine_envelope(
            original_topic=origin.topic,
            original_partition=origin.partition,
            original_offset=origin.offset,
            event_id=origin.event_id,
            pipeline_id=pipeline["id"],
            schema_id=pipeline.get("schema_version_id"),
            quality_policy_id=self.policy["id"],
            failed_rules=failed_rules,
            original_record=original_record,
        )
        if not self._quarantine_topic_ready:
            await ensure_simple_topic(topic, replication_factor=settings.topic_default_replication_factor)
            self._quarantine_topic_ready = True
        sink = get_quarantine_sink(self.policy, await get_kafka_producer())
        await sink.send(envelopes=[failure], pipeline=pipeline)

    async def _forward_to_prepared(self, *, envelope: dict[str, Any] | None, key: str | None, origin: _Origin | None = None) -> None:
        """ENFORCE: write one PASSING record, exactly as Kafka Connect wrote it, to the pipeline's prepared topic and wait for the broker's
        acknowledgement. The topic is created explicitly the first time. RAISES on failure — _handle retries it and, once exhausted,
        dead-letters the record instead of dropping it."""
        topic = self._prepared_topic
        if topic is None:  # unreachable: only called when enforcing
            raise RuntimeError("ENFORCE worker has no prepared topic")
        if not self._prepared_topic_ready:
            await ensure_simple_topic(topic, replication_factor=settings.topic_default_replication_factor)
            self._prepared_topic_ready = True
        producer = await get_kafka_producer()
        origin = origin or self._default_origin()
        headers = {"inai.pipeline_id": self.pipeline["id"], "inai.quality_mode": "ENFORCE", **origin.headers()}
        if dedupe_key := origin.dedupe_key(self.pipeline["id"]):
            headers["inai.dedupe_key"] = dedupe_key               # the same on every delivery of this source record
        await producer.produce(topic=topic, key=key, value=envelope, headers=headers)

    async def _dead_letter(
        self, *, reason: str, error: Exception | str, original_record: Any, key: str | None,
        stats_recorded: bool, failed_rules: list[str] | None = None, origin: _Origin | None = None,
    ) -> None:
        """Writes a record this worker could not process to the worker dead-letter topic and
        returns only once the broker has acknowledged it. If that write fails the worker keeps
        retrying forever (loudly) and never returns — so the caller never commits past a record
        that exists nowhere durable. Cancellation (shutdown) is the only way out, and it leaves the
        offset uncommitted."""
        pipeline = self.pipeline
        topic = build_worker_quarantine_topic_name(
            domain=pipeline["domain"], source_system=pipeline["source_system"],
            entity=pipeline["entity"], version=pipeline["version"]
        )
        detail = error if isinstance(error, str) else to_safe_error(error, event="quality_stream.dead_letter").message
        envelope = {
            "failure_type": "quality_worker_failed",
            "reason": reason,
            "error": detail,
            "pipeline_id": pipeline["id"],
            **({"original_partition": origin.partition} if origin and origin.partition is not None else {}),
            **({"original_offset": origin.offset} if origin and origin.offset is not None else {}),
            "original_topic": (origin or self._default_origin()).topic,
            **({"event_id": origin.event_id} if origin and origin.event_id else {}),
            "quality_policy_id": self.policy["id"],
            "failed_rules": failed_rules or [],
            # True when the stats/counters already include this record: replaying it must not count it twice.
            "stats_recorded": stats_recorded,
            "received_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "original_record": original_record,
        }
        backoff = settings.quality_stream_handle_backoff_seconds
        ceiling = settings.quality_stream_dead_letter_backoff_max_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                if not self._dead_letter_topic_ready:
                    await ensure_simple_topic(topic, replication_factor=settings.topic_default_replication_factor)
                    self._dead_letter_topic_ready = True
                producer = await get_kafka_producer()
                await producer.produce(
                    topic=topic, key=key, value=envelope,
                    headers={
                        "inai.failure_type": "quality_worker_failed", "inai.reason": reason, "inai.pipeline_id": pipeline["id"],
                        **({"inai.dedupe_key": dedupe_key} if (dedupe_key := origin.dedupe_key(pipeline["id"]) if origin else None) else {}),
                    },
                )
                logger.error(
                    "quality_stream.dead_lettered", pipeline_id=pipeline["id"], topic=topic, reason=reason,
                    stats_recorded=stats_recorded,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(
                    "quality_stream.dead_letter_unavailable", pipeline_id=pipeline["id"], topic=topic,
                    reason=reason, attempt=attempt, exc_info=True,
                )
                record_quality_dlq_failure(connector_type=self._connector_type)
                await self._note_blocked()
                # Floor so a zero/tiny configured backoff can't turn a broker outage into a hot loop
                # that floods the log with one traceback per iteration.
                await self._sleep_blocked(max(_DEAD_LETTER_MIN_RETRY_DELAY_SECONDS, min(backoff * attempt, ceiling)))

    async def _record_outcome(
        self, *, valid: bool, failed_rules: list[str], original_record: dict[str, Any], origin: _Origin | None = None,
    ) -> bool:
        """Records one record's statistics — ONCE (NEW-05). True when it was counted, False when this exact record (pipeline, topic, partition,
        offset) had already been counted and this is a replay after a crash: nothing is written then, so the totals, the failure sample and the
        per-rule counts cannot be inflated by a redelivery. The claim on the offset and the statistics are ONE transaction: both commit or
        neither does. A record with no Kafka position (handed to the worker directly) cannot be recognised and is always counted.

        Logs its own distinct event before re-raising, so a failure here
        is still identifiable as "the stats write failed", not just a bare
        "record_failed" — but it DOES re-raise: `_with_retry` (via `_handle`)
        needs to see the failure to retry it and, on exhaustion, to
        dead-letter the record rather than commit past it."""
        try:
            async with AsyncSessionLocal() as session:
                if origin is not None and origin.partition is not None and origin.offset is not None:
                    fresh = await QualityStreamOffsetRepository(session).claim(
                        pipeline_id=self.pipeline["id"], topic=origin.topic, partition=origin.partition, offset=origin.offset,
                    )
                    if not fresh:
                        return False
                repo = QualityStreamStatsRepository(session)
                await repo.record_outcome(
                    org_id=self.org_id, pipeline_id=self.pipeline["id"],
                    quality_policy_id=self.policy["id"], valid=valid,
                )
                if not valid:
                    envelope = build_quarantine_envelope(
                        original_topic=(origin or self._default_origin()).topic,
                        original_partition=origin.partition if origin else None,
                        original_offset=origin.offset if origin else None,
                        event_id=origin.event_id if origin else None,
                        pipeline_id=self.pipeline["id"],
                        schema_id=self.pipeline.get("schema_version_id"),
                        quality_policy_id=self.policy["id"],
                        failed_rules=failed_rules,
                        original_record=original_record,
                    )
                    await repo.append_sample_failure(
                        org_id=self.org_id, pipeline_id=self.pipeline["id"], envelope=envelope,
                    )
                    # FE-BUG-17: the exact per-rule count (the sample above is
                    # bounded and only for display) — same transaction as the stats.
                    await record_validation_failure(
                        session, org_id=self.org_id, pipeline_id=self.pipeline["id"], failed_rules=failed_rules,
                    )
                await session.commit()
                return True
        except Exception:
            logger.warning("quality_stream.stats_update_failed", pipeline_id=self.pipeline["id"], exc_info=True)
            raise


def worker_spec(pipeline: dict[str, Any], policy: dict[str, Any]) -> str:
    """Everything a worker reads ONCE, when it is built: what it consumes, the mode that decides where a record goes, the names its output
    topics are built from, and the policy it evaluates. QualityStreamManager rebuilds a running worker when this changes — otherwise an
    edit (OBSERVE -> ENFORCE, a rule changed, another input topic) took effect only after the backend restarted."""
    return json.dumps({
        "inputs": _input_topics(pipeline),
        "quality_mode": pipeline.get("quality_mode"),
        "prepared_topic": prepared_topic_for(pipeline),
        "names": [pipeline.get(k) for k in ("org_id", "domain", "source_system", "entity", "version")],
        "policy": {k: policy.get(k) for k in ("id", "name", "rules", "on_failure", "quarantine_sink")},
    }, sort_keys=True, default=str)


def _pipeline_row_to_dict(row: Pipeline) -> dict[str, Any]:
    # Deferred import: pipeline_service imports runtime_reconciler at module
    # level, and this mirrors that module's own existing deferred-import
    # convention to avoid turning that into a cycle through this module.
    from app.services.pipeline_service import _pipeline_to_dict

    return _pipeline_to_dict(row)


class QualityStreamManager:
    """Tracks one QualityStreamWorker per pipeline that needs real-time
    quality observation — running, with a quality_policy_id attached, and
    whose own zone isn't "quarantine". Works for source and sink pipelines
    alike; see _input_topics for how each resolves what to actually consume."""

    def __init__(self) -> None:
        self._workers: dict[str, QualityStreamWorker] = {}

    @staticmethod
    def _needs_worker(pipeline: dict[str, Any]) -> bool:
        return bool(pipeline.get("quality_policy_id")) and pipeline.get("zone") != "quarantine"

    async def reconcile_once(self) -> None:
        from app.services.quality_service import quality_service

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(select(Pipeline).where(Pipeline.status == "running"))
            ).scalars().all()
            candidates = {
                row.id: _pipeline_row_to_dict(row) for row in rows
            }
        candidates = {
            pid: pipeline for pid, pipeline in candidates.items() if self._needs_worker(pipeline)
        }

        for pipeline_id, pipeline in candidates.items():
            policy = await quality_service.get_policy(pipeline["org_id"], pipeline["quality_policy_id"])
            running = self._workers.get(pipeline_id)
            if running is not None:
                if policy is None or running.spec == worker_spec(pipeline, policy):
                    continue
                # Edited since the worker was built: rebuild it. Same consumer group, so it carries on from the last committed record.
                await self._workers.pop(pipeline_id).stop()
                logger.info("quality_stream.worker_rebuilt", pipeline_id=pipeline_id, quality_mode=pipeline.get("quality_mode"))
            if not policy:
                logger.warning("quality_stream.policy_missing", pipeline_id=pipeline_id)
                continue
            worker = QualityStreamWorker(org_id=pipeline["org_id"], pipeline=pipeline, policy=policy)
            worker.start()
            self._workers[pipeline_id] = worker
            logger.info("quality_stream.worker_added", pipeline_id=pipeline_id)

        for pipeline_id in [pid for pid in self._workers if pid not in candidates]:
            await self._workers.pop(pipeline_id).stop()
            logger.info("quality_stream.worker_removed", pipeline_id=pipeline_id)

    async def run_forever(self, interval_seconds: float = 10.0) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("quality_stream.reconcile_failed")
            await asyncio.sleep(interval_seconds)

    async def stop_all(self) -> None:
        """Stop every worker at once. One that fails or hangs must not leave the others consuming (app/core/shutdown.py)."""
        from app.core.shutdown import stop_all_isolated

        workers = [(f"quality-stream:{pipeline_id}", self._workers.pop(pipeline_id).stop) for pipeline_id in list(self._workers)]
        await stop_all_isolated(workers, timeout=settings.shutdown_step_timeout_seconds)


quality_stream_manager = QualityStreamManager()
