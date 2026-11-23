"""
Topic provisioning policy — explicit partitions, replication factor, retention,
cleanup policy, min in-sync replicas, compression, maximum message size, and
schema strategy for the topics a pipeline's source connector creates, instead
of leaving them to whatever the Kafka Connect worker / broker happens to
default to.

Resolution order (see resolve_topic_config):
    1. The pipeline's own TopicPolicy (`topic_policy_id`), if it set one.
    2. The org's domain-default TopicPolicy for the pipeline's domain (a
       policy with is_domain_default=True and domain == the pipeline's
       domain), if the pipeline didn't set its own.
    3. Platform defaults (settings.topic_default_*) for any field still null
       after 1-2 — still an explicit, configured value, never a bare
       "whatever the broker feels like."

A policy only needs to override the fields it cares about: any field left
null at a given level falls through to the next.

The resolved config is turned into real settings two ways:
  - topic_creation_connect_config(): Kafka Connect's own
    `topic.creation.default.*` mechanism (Connect API 2.6+), which makes a
    SOURCE connector create its own output topic(s) with these exact
    settings rather than relying on broker auto-create defaults.
  - schema_strategy_converter_config(): the Confluent Schema Registry
    subject-naming-strategy converter properties. Harmless to set even when
    the deployed converter isn't schema-registry-aware (Connect ignores
    config keys a converter doesn't recognize) — it just makes the schema
    strategy an explicit, recorded choice for whenever a registry-aware
    converter is used.

PostgreSQL is authoritative for TopicPolicy rows (dc_topic_policies, see
app/models/topic.py) — TopicPolicyService below is the CRUD API, and
find_domain_default()/resolve_topic_config() read the same table rather
than a memory-store copy of it.
"""

from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.errors import ValidationError
from app.db.session import AsyncSessionLocal
from app.models.topic import TopicPolicy
from app.repositories.dataconnect import TopicPolicyRepository
from app.schemas.errors import FieldError

CLEANUP_POLICIES = {"delete", "compact", "compact,delete"}
COMPRESSION_TYPES = {"producer", "gzip", "snappy", "lz4", "zstd", "uncompressed"}
SCHEMA_STRATEGIES = {"topic_name", "record_name", "topic_record_name"}

_SCHEMA_STRATEGY_CLASS = {
    "topic_name": "io.confluent.kafka.serializers.subject.TopicNameStrategy",
    "record_name": "io.confluent.kafka.serializers.subject.RecordNameStrategy",
    "topic_record_name": "io.confluent.kafka.serializers.subject.TopicRecordNameStrategy",
}

# The 8 knobs this ticket asks for, keyed by TopicPolicy field name.
POLICY_FIELDS = (
    "partitions",
    "replication_factor",
    "retention_ms",
    "retention_bytes",
    "cleanup_policy",
    "min_insync_replicas",
    "compression_type",
    "max_message_bytes",
    "schema_strategy",
)


def platform_defaults() -> dict[str, Any]:
    return {
        "partitions": settings.topic_default_partitions,
        "replication_factor": settings.topic_default_replication_factor,
        "retention_ms": settings.topic_default_retention_ms,
        "retention_bytes": None,
        "cleanup_policy": settings.topic_default_cleanup_policy,
        "min_insync_replicas": settings.topic_default_min_insync_replicas,
        "compression_type": settings.topic_default_compression_type,
        "max_message_bytes": settings.topic_default_max_message_bytes,
        "schema_strategy": settings.topic_default_schema_strategy,
    }


def _policy_to_dict(row: TopicPolicy) -> dict[str, Any]:
    return {
        "id": row.id, "org_id": row.org_id, "name": row.name, "description": row.description,
        "domain": row.domain, "is_domain_default": row.is_domain_default,
        "partitions": row.partitions, "replication_factor": row.replication_factor,
        "retention_ms": row.retention_ms, "retention_bytes": row.retention_bytes,
        "cleanup_policy": row.cleanup_policy, "min_insync_replicas": row.min_insync_replicas,
        "compression_type": row.compression_type, "max_message_bytes": row.max_message_bytes,
        "schema_strategy": row.schema_strategy,
    }


async def find_domain_default(org_id: str, domain: str) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        row = await TopicPolicyRepository(session).find_domain_default(org_id, domain)
        return _policy_to_dict(row) if row else None


async def resolve_topic_config(
    org_id: str, domain: str, explicit_policy: dict[str, Any] | None
) -> dict[str, Any]:
    """Fully resolve the 8 provisioning knobs: the pipeline's own policy >
    the org's domain-default policy for `domain` > platform defaults, field
    by field."""
    policy = explicit_policy or await find_domain_default(org_id, domain)
    defaults = platform_defaults()
    if not policy:
        return defaults
    return {
        field: policy.get(field) if policy.get(field) is not None else default
        for field, default in defaults.items()
    }


def topic_creation_connect_config(resolved: dict[str, Any]) -> dict[str, str]:
    """Kafka Connect `topic.creation.default.*` config for a SOURCE
    connector — only meaningful for connectors that create their own output
    topic(s), never for sinks (which read from an already-existing topic)."""
    config = {
        "topic.creation.enable": "true",
        "topic.creation.default.partitions": str(resolved["partitions"]),
        "topic.creation.default.cleanup.policy": resolved["cleanup_policy"],
        "topic.creation.default.compression.type": resolved["compression_type"],
    }
    if resolved.get("replication_factor") is not None:
        config["topic.creation.default.replication.factor"] = str(resolved["replication_factor"])
    if resolved.get("retention_ms") is not None:
        config["topic.creation.default.retention.ms"] = str(resolved["retention_ms"])
    if resolved.get("retention_bytes") is not None:
        config["topic.creation.default.retention.bytes"] = str(resolved["retention_bytes"])
    if resolved.get("min_insync_replicas") is not None:
        config["topic.creation.default.min.insync.replicas"] = str(resolved["min_insync_replicas"])
    if resolved.get("max_message_bytes") is not None:
        config["topic.creation.default.max.message.bytes"] = str(resolved["max_message_bytes"])
    return config


def schema_strategy_converter_config(resolved: dict[str, Any]) -> dict[str, str]:
    strategy_class = _SCHEMA_STRATEGY_CLASS[resolved["schema_strategy"]]
    return {
        "key.converter.key.subject.name.strategy": strategy_class,
        "value.converter.value.subject.name.strategy": strategy_class,
    }


class TopicPolicyService:
    async def list_policies(self, org_id: str) -> list[dict[str, Any]]:
        async with AsyncSessionLocal() as session:
            rows = await TopicPolicyRepository(session).list_for_org(org_id)
            return [_policy_to_dict(r) for r in rows]

    async def get_policy(self, org_id: str, policy_id: str) -> dict[str, Any] | None:
        async with AsyncSessionLocal() as session:
            row = await TopicPolicyRepository(session).get_for_org(org_id, policy_id)
            return _policy_to_dict(row) if row else None

    async def get_or_404(self, org_id: str, policy_id: str) -> dict[str, Any]:
        policy = await self.get_policy(org_id, policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic policy not found")
        return policy

    async def _check_against_the_cluster(self, payload: dict[str, Any]) -> None:
        """GAP-06: a policy that asks for more replicas than the cluster has brokers can never create a topic. Refused when it is
        SAVED, not when a pipeline using it first tries to deploy. Only the fields the policy sets itself are judged here
        (the rest fall through to platform defaults, checked at readiness/deploy), and a cluster that can't be asked, or a
        deployment with the check switched off, never blocks a save."""
        if not settings.topic_feasibility_check_enabled:
            return
        explicit = {k: payload.get(k) for k in ("replication_factor", "min_insync_replicas") if payload.get(k) is not None}
        if not explicit:
            return
        from app.services.kafka_cluster import broker_count, topic_policy_problems

        brokers = await broker_count()
        if brokers is None:
            return
        # Judge only what this policy states: pair each explicit field with a value that cannot itself be the problem.
        resolved = {
            "replication_factor": explicit.get("replication_factor"),
            "min_insync_replicas": explicit.get("min_insync_replicas"),
        }
        problems = [
            p for p in topic_policy_problems(resolved, brokers)
            if p.code in ("replication_exceeds_brokers", "min_isr_exceeds_brokers")
        ]
        if problems:
            fields = [
                FieldError(field="replication_factor" if "replication" in p.code.split("_")[0] else "min_insync_replicas",
                           message=p.message)
                for p in problems
            ]
            raise ValidationError(
                " ".join(p.message for p in problems) + f" This Kafka cluster has {brokers} broker(s).",
                field_errors=fields,
            )

    async def create_policy(self, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self._check_against_the_cluster(payload)
        payload = dict(payload)
        payload["org_id"] = org_id
        async with AsyncSessionLocal() as session:
            row = await TopicPolicyRepository(session).create(payload)
            await session.commit()
            return _policy_to_dict(row)

    async def update_policy(self, org_id: str, policy_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Full-replace PUT semantics: `payload` must carry every field
        # (including explicit nulls for anything the caller wants cleared),
        # not just the ones that changed — see app/api/routes/topic_policies.py.
        await self._check_against_the_cluster(payload)
        async with AsyncSessionLocal() as session:
            repo = TopicPolicyRepository(session)
            row = await repo.get_for_org(org_id, policy_id)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic policy not found")
            payload = dict(payload)
            payload["org_id"] = org_id
            row = await repo.update(row, payload)
            await session.commit()
            return _policy_to_dict(row)

    async def delete_policy(self, org_id: str, policy_id: str) -> None:
        async with AsyncSessionLocal() as session:
            repo = TopicPolicyRepository(session)
            row = await repo.get_for_org(org_id, policy_id)
            if row is not None:
                await repo.delete(row)
                await session.commit()


topic_policy_service = TopicPolicyService()
