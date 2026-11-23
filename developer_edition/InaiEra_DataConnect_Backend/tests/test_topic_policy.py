import pytest

from app.core.config import settings
from app.services.topic_policy import (
    find_domain_default,
    platform_defaults,
    resolve_topic_config,
    schema_strategy_converter_config,
    topic_creation_connect_config,
)


@pytest.mark.asyncio
async def test_resolve_topic_config_falls_back_to_platform_defaults_when_no_policy(fake_postgres) -> None:
    """The whole point of the ticket: even with no pipeline or domain policy,
    the resolved config is the platform's explicit, configured default —
    never an implicit broker default."""
    resolved = await resolve_topic_config("org_no_policy", "no_such_domain", None)
    assert resolved == platform_defaults()
    assert resolved["partitions"] == settings.topic_default_partitions
    assert resolved["replication_factor"] == settings.topic_default_replication_factor
    assert resolved["schema_strategy"] == settings.topic_default_schema_strategy


@pytest.mark.asyncio
async def test_resolve_topic_config_explicit_policy_overrides_platform_defaults(fake_postgres) -> None:
    explicit = {"partitions": 24, "replication_factor": 5, "cleanup_policy": "compact"}
    resolved = await resolve_topic_config("org_x", "sales", explicit)

    assert resolved["partitions"] == 24
    assert resolved["replication_factor"] == 5
    assert resolved["cleanup_policy"] == "compact"
    # Fields the policy didn't set still fall back to the platform default.
    assert resolved["compression_type"] == settings.topic_default_compression_type
    assert resolved["schema_strategy"] == settings.topic_default_schema_strategy


@pytest.mark.asyncio
async def test_resolve_topic_config_explicit_policy_null_field_falls_back_to_default(fake_postgres) -> None:
    """A policy that HAS the field but left it null must still inherit the
    platform default for that one field — not silently resolve to None."""
    explicit = {"partitions": 12, "replication_factor": None}
    resolved = await resolve_topic_config("org_x", "sales", explicit)

    assert resolved["partitions"] == 12
    assert resolved["replication_factor"] == settings.topic_default_replication_factor


@pytest.mark.asyncio
async def test_find_domain_default_matches_org_and_domain(fake_postgres) -> None:
    fake_postgres.create(
        "topic_policies",
        {"org_id": "org_domain_test", "domain": "finance", "is_domain_default": True, "partitions": 18},
    )

    found = await find_domain_default("org_domain_test", "finance")
    assert found is not None
    assert found["partitions"] == 18


@pytest.mark.asyncio
async def test_find_domain_default_ignores_non_default_policies(fake_postgres) -> None:
    fake_postgres.create(
        "topic_policies",
        {"org_id": "org_domain_test_2", "domain": "finance", "is_domain_default": False, "partitions": 99},
    )

    assert await find_domain_default("org_domain_test_2", "finance") is None


@pytest.mark.asyncio
async def test_resolve_topic_config_uses_domain_default_when_pipeline_has_no_explicit_policy(fake_postgres) -> None:
    fake_postgres.create(
        "topic_policies",
        {"org_id": "org_domain_test_3", "domain": "sales", "is_domain_default": True, "partitions": 30},
    )

    resolved = await resolve_topic_config("org_domain_test_3", "sales", None)
    assert resolved["partitions"] == 30


@pytest.mark.asyncio
async def test_resolve_topic_config_explicit_policy_beats_domain_default(fake_postgres) -> None:
    fake_postgres.create(
        "topic_policies",
        {"org_id": "org_domain_test_4", "domain": "sales", "is_domain_default": True, "partitions": 30},
    )
    explicit = {"partitions": 3}

    resolved = await resolve_topic_config("org_domain_test_4", "sales", explicit)
    assert resolved["partitions"] == 3


def test_topic_creation_connect_config_sets_the_required_knobs() -> None:
    resolved = platform_defaults()
    config = topic_creation_connect_config(resolved)

    assert config["topic.creation.enable"] == "true"
    assert config["topic.creation.default.partitions"] == str(resolved["partitions"])
    assert config["topic.creation.default.replication.factor"] == str(resolved["replication_factor"])
    assert config["topic.creation.default.cleanup.policy"] == resolved["cleanup_policy"]
    assert config["topic.creation.default.min.insync.replicas"] == str(resolved["min_insync_replicas"])
    assert config["topic.creation.default.compression.type"] == resolved["compression_type"]
    assert config["topic.creation.default.max.message.bytes"] == str(resolved["max_message_bytes"])
    assert config["topic.creation.default.retention.ms"] == str(resolved["retention_ms"])


def test_topic_creation_connect_config_omits_unset_optional_fields() -> None:
    resolved = platform_defaults()
    resolved["replication_factor"] = None
    resolved["retention_ms"] = None
    resolved["retention_bytes"] = None
    resolved["min_insync_replicas"] = None
    resolved["max_message_bytes"] = None

    config = topic_creation_connect_config(resolved)

    for key in (
        "topic.creation.default.replication.factor",
        "topic.creation.default.retention.ms",
        "topic.creation.default.retention.bytes",
        "topic.creation.default.min.insync.replicas",
        "topic.creation.default.max.message.bytes",
    ):
        assert key not in config


def test_schema_strategy_converter_config_maps_each_strategy() -> None:
    resolved = {"schema_strategy": "record_name"}
    config = schema_strategy_converter_config(resolved)

    assert config["key.converter.key.subject.name.strategy"] == (
        "io.confluent.kafka.serializers.subject.RecordNameStrategy"
    )
    assert config["value.converter.value.subject.name.strategy"] == (
        "io.confluent.kafka.serializers.subject.RecordNameStrategy"
    )
