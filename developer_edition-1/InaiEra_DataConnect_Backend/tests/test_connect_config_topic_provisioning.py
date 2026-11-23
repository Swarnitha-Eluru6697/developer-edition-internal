import pytest

from app.services.connect_config import JDBC_SINK, JDBC_SOURCE, build_connector


def _pipeline(**overrides) -> dict:
    base = {
        "id": "pipe-orders",
        "org_id": "org_topic_prov",
        "domain": "sales",
        "source_system": "pg",
        "entity": "orders",
        "version": "v1",
        "topic_name": "raw-sales-pg-orders-cdc-v1",
        "connector_name": "raw--sales--pg--orders--cdc--v1",
        "connector_config": {},
    }
    base.update(overrides)
    return base


def _connection(**overrides) -> dict:
    config = {"hostname": "db.internal", "database": "salesdb", "username": "svc", "password": "hunter2"}
    config.update(overrides)
    return {"connection_config": config}


@pytest.mark.asyncio
async def test_source_connector_deploy_config_includes_topic_creation_settings(fake_postgres) -> None:
    """The real, deploy-time config (not just the preview) must carry
    explicit topic-provisioning config for a source connector."""
    template = {"connector_class": JDBC_SOURCE}
    config, produced_topic = await build_connector(_pipeline(), _connection(), template, schema=None)

    assert produced_topic
    assert config["topic.creation.enable"] == "true"
    assert "topic.creation.default.partitions" in config
    assert "topic.creation.default.replication.factor" in config
    assert "key.converter.key.subject.name.strategy" in config


@pytest.mark.asyncio
async def test_sink_connector_deploy_config_has_no_topic_creation_settings(fake_postgres) -> None:
    """A sink connector reads from an existing topic — it must never get
    topic.creation.* config, which only applies to a connector creating its
    own output topic."""
    template = {"connector_class": JDBC_SINK}
    pipeline = _pipeline(connector_config={"topics": "raw-t-s-some_input_topic-batch-v1"})
    config, produced_topic = await build_connector(pipeline, _connection(), template, schema=None)

    assert produced_topic is None
    assert "topic.creation.enable" not in config
    assert "key.converter.key.subject.name.strategy" not in config


@pytest.mark.asyncio
async def test_source_connector_deploy_config_respects_explicit_topic_policy(fake_postgres) -> None:
    template = {"connector_class": JDBC_SOURCE}
    topic_policy = {"org_id": "org_topic_prov", "partitions": 16, "compression_type": "zstd"}

    config, _ = await build_connector(_pipeline(), _connection(), template, schema=None, topic_policy=topic_policy)

    assert config["topic.creation.default.partitions"] == "16"
    assert config["topic.creation.default.compression.type"] == "zstd"


@pytest.mark.asyncio
async def test_source_connector_deploy_config_uses_domain_default_topic_policy(fake_postgres) -> None:
    """No explicit topic_policy_id on the pipeline, but the org has a
    domain-default TopicPolicy for this pipeline's domain — that must be
    picked up automatically at deploy time."""
    fake_postgres.create(
        "topic_policies",
        {"org_id": "org_domain_deploy", "domain": "sales", "is_domain_default": True, "partitions": 9},
    )
    template = {"connector_class": JDBC_SOURCE}
    pipeline = _pipeline(org_id="org_domain_deploy")

    config, _ = await build_connector(pipeline, _connection(), template, schema=None, topic_policy=None)

    assert config["topic.creation.default.partitions"] == "9"
