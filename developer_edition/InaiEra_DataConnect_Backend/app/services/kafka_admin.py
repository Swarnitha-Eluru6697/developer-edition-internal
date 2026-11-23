"""
Explicit Kafka topic provisioning via the Kafka Admin API.

The local development Compose broker runs with
KAFKA_AUTO_CREATE_TOPICS_ENABLE=true (see infra/docker-compose.yml) — the
first producer or consumer to touch an unknown topic just gets *something*
created for it, with whatever the broker's bare defaults happen to be. Fine
for development. Production DataConnect behavior must not depend on that
implicit, unrecorded path: before a connector is ever deployed, the
pipeline's resolved TopicPolicy (see app/services/topic_policy.py) is used
to either create the topic outright with those exact settings, or confirm
that an already-existing topic's configuration matches it.

Gated by settings.topic_admin_provisioning_enabled (default False, same
opt-in convention as every other real-infrastructure feature flag in this
codebase — see settings.runtime_deploy_enabled) so tests and a bare
development checkout never need a live broker connection.

Deliberately NOT covered: reconciling partitions/replication_factor on an
ALREADY-EXISTING topic. Kafka doesn't support shrinking either one, and
safely growing them (a partition increase can break key-based ordering for
existing consumers; a replication-factor change needs a partition
reassignment) is a distinct, riskier operation than "provision before first
deploy" — out of scope here. Only the topic-level configs
(retention/cleanup/min-ISR/compression/max-message-bytes) are diffed against
an existing topic.

GAP-06: a resolved TopicPolicy's replication_factor (platform default 3, or
whatever a domain/pipeline policy overrides it to) is only ever validated
against Kafka's own hard ge=1 floor (app/schemas/topic.py) — nothing checks
it against how many brokers the target cluster actually has. Kafka itself
rejects an over-ambitious CreateTopics with InvalidReplicationFactorError,
but only after issuing the request, and that error was previously getting
flattened into the same generic TopicProvisioningError as "the broker is
unreachable" — ambiguous, and a 500 the caller can't act on (see
app/core/errors.py's module docstring on why DataConnectError subclasses
exist at all). _check_replication_factor asks the live cluster (via the
same KafkaAdminClient already open for this call, admin.describe_cluster())
how many brokers it has and raises TopicReplicationFactorError — a
distinct, 422/non-retryable, UI-actionable error — before ever building the
CreateTopics request.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog
from kafka.admin import ConfigResource, ConfigResourceType, KafkaAdminClient, NewTopic
from kafka.errors import KafkaError, TopicAlreadyExistsError

from app.core.config import settings
from app.core.errors import TopicProvisioningError, TopicReplicationFactorError
from app.services.kafka_cluster import brokers_of, topic_exists_sync, topic_policy_problems

logger = structlog.get_logger(__name__)

# TopicPolicy/resolve_topic_config field name -> the Kafka topic-config key it maps to.
_TOPIC_CONFIG_KEYS = {
    "retention_ms": "retention.ms",
    "retention_bytes": "retention.bytes",
    "cleanup_policy": "cleanup.policy",
    "min_insync_replicas": "min.insync.replicas",
    "compression_type": "compression.type",
    "max_message_bytes": "max.message.bytes",
}


@dataclass(frozen=True)
class TopicProvisionResult:
    """What ensure_topic actually did — also the shape recorded in the
    audit trail (see PipelineService._record_topic_policy_audit)."""

    topic: str
    action: str  # "created" | "verified" | "config_mismatch"
    resolved_config: dict[str, Any]
    observed_config: dict[str, str] | None = None
    mismatches: dict[str, tuple[str, str | None]] = field(default_factory=dict)


def _expected_topic_configs(resolved_config: dict[str, Any]) -> dict[str, str]:
    configs = {}
    for field_name, kafka_key in _TOPIC_CONFIG_KEYS.items():
        value = resolved_config.get(field_name)
        if value is not None:
            configs[kafka_key] = str(value)
    return configs


def _check_replication_factor(admin: KafkaAdminClient, topic: str, resolved: dict[str, Any]) -> None:
    """GAP-06 preflight: reject a policy the live cluster can't satisfy BEFORE issuing CreateTopics — see this module's
    docstring. Covers the replication factor against the broker count, and min.insync.replicas against both the replication
    factor and the brokers (app/services/kafka_cluster.py's topic_policy_problems). A cluster reporting zero brokers is treated as
    "couldn't determine broker count" (a describe_cluster() response shape this codebase hasn't seen, not "there are no brokers")
    and only the checks that need no broker count run; a genuinely unreachable admin API raises KafkaError from
    describe_cluster() itself, caught the same way as any other Admin API call in this module."""
    broker_count = brokers_of(admin)  # raises KafkaError if the cluster can't be reached
    if not broker_count:
        logger.warning("topic_admin.broker_count_unknown", topic=topic)
    problems = topic_policy_problems(resolved, broker_count or None)
    if problems:
        raise TopicReplicationFactorError(
            f"Cannot create topic '{topic}': " + " ".join(p.message for p in problems),
            details={
                "topic": topic, "requested_replication_factor": resolved["replication_factor"],
                "min_insync_replicas": resolved.get("min_insync_replicas"), "broker_count": broker_count or None,
                "problems": [p.code for p in problems],
            },
        )


async def ensure_topic(topic: str, resolved_config: dict[str, Any]) -> TopicProvisionResult:
    """Create `topic` with `resolved_config`'s settings if it doesn't exist
    yet; if it already exists, verify its topic-level config matches (a
    mismatch is reported, never silently corrected — this must never mutate
    the config of a topic a connector might already be deployed against)."""
    return await asyncio.to_thread(_ensure_topic_sync, topic, resolved_config)


def _ensure_topic_sync(topic: str, resolved_config: dict[str, Any]) -> TopicProvisionResult:
    admin = KafkaAdminClient(
        bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-topic-admin"
    )
    try:
        # GAP-06: only a topic that is about to be CREATED is checked against the cluster. One that already exists is verified
        # (config diffed, never changed) — what a policy says about its replication now cannot be applied to it anyway.
        if topic_exists_sync(admin, topic):
            return _verify_existing_topic(admin, topic, resolved_config)
        _check_replication_factor(admin, topic, resolved_config)
        new_topic = NewTopic(
            name=topic,
            num_partitions=resolved_config["partitions"],
            replication_factor=resolved_config["replication_factor"],
            topic_configs=_expected_topic_configs(resolved_config),
        )
        try:
            admin.create_topics([new_topic])
            logger.info(
                "topic_admin.created", topic=topic,
                partitions=resolved_config["partitions"],
                replication_factor=resolved_config["replication_factor"],
            )
            return TopicProvisionResult(topic=topic, action="created", resolved_config=resolved_config)
        except TopicAlreadyExistsError:
            return _verify_existing_topic(admin, topic, resolved_config)
    except KafkaError as exc:
        raise TopicProvisioningError(f"Could not provision topic '{topic}': {exc}") from exc
    finally:
        admin.close()


def _verify_existing_topic(
    admin: KafkaAdminClient, topic: str, resolved_config: dict[str, Any]
) -> TopicProvisionResult:
    resource = ConfigResource(ConfigResourceType.TOPIC, topic)
    described = admin.describe_configs([resource])
    entries = described.get("topic", {}).get(topic, {})
    observed = {name: entry["value"] for name, entry in entries.items()}

    expected = _expected_topic_configs(resolved_config)
    mismatches = {
        key: (expected_value, observed.get(key))
        for key, expected_value in expected.items()
        if observed.get(key) != expected_value
    }
    if mismatches:
        logger.warning("topic_admin.config_mismatch", topic=topic, mismatches=mismatches)
        return TopicProvisionResult(
            topic=topic, action="config_mismatch", resolved_config=resolved_config,
            observed_config=observed, mismatches=mismatches,
        )
    logger.info("topic_admin.verified", topic=topic)
    return TopicProvisionResult(
        topic=topic, action="verified", resolved_config=resolved_config, observed_config=observed
    )


async def ensure_simple_topic(topic: str, *, partitions: int = 1, replication_factor: int = 1) -> None:
    """Idempotently create `topic` with bare defaults if it doesn't exist yet —
    unconditional, unlike provision_topic_and_record_audit (gated behind
    settings.topic_admin_provisioning_enabled): that one resolves a
    pipeline's own TopicPolicy for its primary advertised topic, but the
    quality-routing feature's topics (a pipeline's own zone topic when its
    real output was redirected to an internal "intake" staging topic
    instead, plus that pipeline's quarantine topic — see
    app/services/quality_stream_worker.py and quarantine_sink.py) are never
    covered by that path and must not depend on the broker's implicit
    auto-create either way. See this module's docstring for why that
    implicit path is dev-only: on a shared cluster it silently recreates
    empty topics for pipelines that no longer exist the moment any stray
    client (this codebase's own or a third-party one, e.g. a Kafka UI's
    background metrics poll against a stale topic-name cache) so much as
    describes their old name — confirmed against a live cluster, repeatedly,
    with zero involvement from any of the pipelines' or workers' own code
    once they were properly torn down."""
    await asyncio.to_thread(_ensure_simple_topic_sync, topic, partitions, replication_factor)


async def trim_consumed_records(topic: str, group_id: str, *, already: dict[int, int] | None = None) -> dict[int, int]:
    """Delete the records of `topic` that consumer group `group_id` has committed past, partition by partition: what was already read and
    acted on, and nothing it has not reached yet. Returns {partition: offset deleted up to} for the partitions trimmed now; `already` (the
    caller's last result) skips a partition with nothing new. Used for an ENFORCE sink's hand-off topic (quality_mode.SINK_HANDOFF_PREFIX):
    a passing record stays in Kafka only until the sink's connector has written it to the table."""
    return await asyncio.to_thread(_trim_consumed_records_sync, topic, group_id, already or {})


def _trim_consumed_records_sync(topic: str, group_id: str, already: dict[int, int]) -> dict[int, int]:
    admin = KafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-handoff-trim")
    try:
        committed = admin.list_group_offsets({group_id: None}).get(group_id) or {}
        to_delete = {tp: meta.offset for tp, meta in committed.items()
                     if tp.topic == topic and meta.offset is not None and meta.offset > already.get(tp.partition, 0)}
        if to_delete:
            admin.delete_records(to_delete)
            logger.info("topic_admin.trimmed_consumed", topic=topic, group_id=group_id,
                        offsets={tp.partition: offset for tp, offset in to_delete.items()})
        return {tp.partition: offset for tp, offset in to_delete.items()}
    finally:
        admin.close()


def _ensure_simple_topic_sync(topic: str, partitions: int, replication_factor: int) -> None:
    admin = KafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap, client_id="dataconnect-topic-admin")
    try:
        try:
            admin.create_topics(
                [NewTopic(name=topic, num_partitions=partitions, replication_factor=replication_factor)]
            )
            logger.info("topic_admin.ensured", topic=topic)
        except TopicAlreadyExistsError:
            pass
    except KafkaError as exc:
        raise TopicProvisioningError(f"Could not provision topic '{topic}': {exc}") from exc
    finally:
        admin.close()


async def provision_topic_and_record_audit(
    org_id: str, pipeline: dict[str, Any], topic: str, topic_policy: dict[str, Any] | None
) -> TopicProvisionResult:
    """The single entry point both PipelineService.deploy() and the runtime
    reconciler's self-heal redeploy path call before a source connector is
    deployed: resolve the effective TopicPolicy, explicitly create/validate
    the topic against it, and record what was resolved/observed to the
    audit trail (app/models/audit.py) — regardless of which caller actually
    triggered the deploy, so the intended policy is always on record.

    Raises TopicProvisioningError (propagated, never swallowed) if the
    Admin API itself can't be reached — callers must let that abort the
    deploy rather than fall through to the broker's implicit auto-create.
    Raises TopicReplicationFactorError (GAP-06) if the resolved policy's
    replication_factor exceeds the live cluster's broker count.
    """
    from app.db.session import AsyncSessionLocal
    from app.repositories.dataconnect import AuditLogRepository
    from app.services.topic_policy import resolve_topic_config

    resolved = await resolve_topic_config(org_id, pipeline["domain"], topic_policy)
    result = await ensure_topic(topic, resolved)
    async with AsyncSessionLocal() as session:
        await AuditLogRepository(session).create(
            {
                "org_id": org_id,
                "actor": "system:pipeline_deploy",
                "action": f"topic_policy_{result.action}",
                "resource_type": "topic",
                "resource_id": result.topic,
                "before_state": {"observed_config": result.observed_config} if result.observed_config else None,
                "after_state": {"resolved_policy": result.resolved_config, "mismatches": result.mismatches},
            }
        )
        await session.commit()
    logger.info(
        "topic_admin.provisioning_recorded", pipeline_id=pipeline.get("id"), topic=topic,
        action=result.action, mismatches=bool(result.mismatches),
    )
    return result
