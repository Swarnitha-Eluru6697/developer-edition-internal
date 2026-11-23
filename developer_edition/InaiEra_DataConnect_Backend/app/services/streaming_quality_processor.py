from typing import Any

from app.services.kafka_runtime import KafkaProducer
from app.services.pipeline_topics import data_topic
from app.services.quality import build_quarantine_envelope, evaluate_record
from app.services.record_key import record_key
from app.services.topic_naming import (
    to_prepared_topic,
    validation_quarantine_topic_for,
)


class StreamingQualityProcessor:
    def __init__(self, producer: KafkaProducer):
        self.producer = producer

    async def process_record(
        self,
        *,
        record: dict[str, Any],
        pipeline: dict[str, Any],
        policy: dict[str, Any],
        schema_id: str | None = None,
    ) -> dict[str, Any]:
        result = evaluate_record(record, policy)
        # Same key for both branches: same-entity records stay ordered on one
        # partition whether they end up prepared or quarantined.
        key = record_key(record)

        if result["valid"]:
            prepared_topic = self._prepared_topic(pipeline)
            delivery = await self.producer.produce(
                topic=prepared_topic,
                key=key,
                value=record,
                headers={"inai.pipeline_id": pipeline["id"]},
            )
            return {
                "status": "valid",
                "destination_topic": prepared_topic,
                "failed_rules": [],
                "delivery": delivery.as_dict(),
            }

        quarantine_topic = self._quarantine_topic(pipeline)
        envelope = build_quarantine_envelope(
            original_topic=data_topic(pipeline),
            pipeline_id=pipeline["id"],
            schema_id=schema_id or pipeline.get("schema_version_id"),
            quality_policy_id=policy["id"],
            failed_rules=result["failed_rules"],
            original_record=record,
        )
        delivery = await self.producer.produce(
            topic=quarantine_topic,
            key=key,
            value=envelope,
            headers={
                "inai.failure_type": envelope["failure_type"],
                "inai.pipeline_id": pipeline["id"],
            },
        )
        return {
            "status": "quarantined",
            "destination_topic": quarantine_topic,
            "failed_rules": result["failed_rules"],
            "quarantine_record": envelope,
            "delivery": delivery.as_dict(),
        }

    def _prepared_topic(self, pipeline: dict[str, Any]) -> str:
        return to_prepared_topic(data_topic(pipeline))

    def _quarantine_topic(self, pipeline: dict[str, Any]) -> str:
        return validation_quarantine_topic_for(pipeline)

