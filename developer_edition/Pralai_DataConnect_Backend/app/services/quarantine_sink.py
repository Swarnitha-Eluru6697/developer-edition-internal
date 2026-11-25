"""
Quarantine destination for the FULL set of failed records from a quality
policy execution (see QualityService.execute_policy) — as opposed to the
small, bounded sample persisted alongside the aggregate QualityResult row in
Postgres (see build_quality_result_summary), every failed record is routed
here so nothing is silently discarded for a high-volume batch.

The destination is selected by the policy's `quarantine_sink` field:
  - "kafka" (default): produced onto the pipeline's quarantine topic via the
    existing Kafka producer abstraction (app/services/kafka_runtime.py).
  - "object_storage": reserved for a future backend (S3/GCS/...). This
    codebase has no object-storage client, bucket, or credential convention
    yet. app/services/quality.py's validate_quality_policy_fields already
    rejects a policy that selects it at create/update time (BUG-09) — no
    policy saved through the API can reach ObjectStorageQuarantineSink.send()
    below at all. It still raises rather than silently no-op-ing, purely as
    a defense-in-depth backstop (a hand-inserted row, a future validation
    gap) — this must never be the only thing standing between a user and a
    silently-dropped quarantine record.
"""

from typing import Any, Protocol

from app.services.kafka_runtime import KafkaProducer
from app.services.record_key import record_key
from app.services.topic_naming import (
    validation_quarantine_topic_for,
)


class QuarantineSink(Protocol):
    async def send(
        self, *, envelopes: list[dict[str, Any]], pipeline: dict[str, Any], dedupe_prefix: str | None = None,
    ) -> None: ...


class KafkaQuarantineSink:
    def __init__(self, producer: KafkaProducer):
        self.producer = producer

    async def send(
        self, *, envelopes: list[dict[str, Any]], pipeline: dict[str, Any], dedupe_prefix: str | None = None,
    ) -> None:
        """`dedupe_prefix` (GAP-13): a quality job resumed after its worker died rewrites the chunk it was in the middle of
        in full. Each message of that chunk therefore carries a stable `inai.dedupe_key` (<prefix>:<position>) — the same
        on every attempt — so a consumer can discard the repeat. (Kafka gives no exactly-once without transactions;
        this makes at-least-once safe to consume.)"""
        topic = validation_quarantine_topic_for(pipeline)
        for position, envelope in enumerate(envelopes):
            original_record = envelope["original_record"]
            key = record_key(original_record)
            headers = {
                "inai.failure_type": envelope["failure_type"],
                "inai.pipeline_id": pipeline["id"],
            }
            if dedupe_prefix:
                headers["inai.dedupe_key"] = f"{dedupe_prefix}:{position}"
            elif envelope.get("event_id"):
                # NEW-05: a stream-worker failure. Its identity is the source record's position, per pipeline (two pipelines can read one topic).
                headers["inai.dedupe_key"] = f"{pipeline['id']}:{envelope['event_id']}"
            await self.producer.produce(topic=topic, key=key, value=envelope, headers=headers)


class ObjectStorageQuarantineSink:
    async def send(
        self, *, envelopes: list[dict[str, Any]], pipeline: dict[str, Any], dedupe_prefix: str | None = None,
    ) -> None:
        raise NotImplementedError(
            "quality_policy.quarantine_sink='object_storage' has no backend yet — "
            "implement an object storage client (bucket, credentials, upload path) "
            "before enabling this option on a policy."
        )


def get_quarantine_sink(policy: dict[str, Any], producer: KafkaProducer) -> QuarantineSink:
    sink = policy.get("quarantine_sink", "kafka")
    if sink == "object_storage":
        return ObjectStorageQuarantineSink()
    return KafkaQuarantineSink(producer)
