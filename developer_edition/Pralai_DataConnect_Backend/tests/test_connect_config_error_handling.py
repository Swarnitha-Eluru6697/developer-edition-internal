"""
GAP-04: errors.tolerance used to only ever be set in the create-time
PREVIEW builder (pipeline_config.generate_pipeline_config) — the REAL
deploy-time config, built by connect_config.build_connector, never set it
at all, so every actually-deployed connector silently ran under Kafka
Connect's own default (errors.tolerance=none) regardless of what the
preview claimed. These tests are against build_connector itself — the
function whose output is what actually gets POSTed to Kafka Connect
(app/services/pipeline_service.py's deploy(), and the runtime reconciler).
"""

import pytest

from app.services.connect_config import DBZ_PG, JDBC_SINK, build_connector


def _pipeline(**overrides) -> dict:
    base = {
        "id": "pipe-orders", "org_id": "org_error_handling", "domain": "sales", "source_system": "pg",
        "entity": "orders", "version": "v1",
        "topic_name": "raw-sales-pg-orders-cdc-v1",
        "connector_name": "raw--sales--pg--orders--cdc--v1",
        "connector_config": {},
    }
    base.update(overrides)
    return base


def _connection() -> dict:
    return {"connection_config": {"hostname": "db.internal", "database": "salesdb", "username": "svc", "password": "hunter2"}}


@pytest.mark.asyncio
async def test_fail_fast_deploy_config_has_no_dlq(fake_postgres) -> None:
    template = {"connector_class": DBZ_PG}
    config, _ = await build_connector(
        _pipeline(error_handling_policy="FAIL_FAST"), _connection(), template, schema=None,
    )

    assert config["errors.tolerance"] == "none"
    assert "errors.deadletterqueue.topic.name" not in config


@pytest.mark.asyncio
async def test_dlq_and_continue_deploy_config_has_a_dlq_topic_for_a_sink(fake_postgres) -> None:
    template = {"connector_class": JDBC_SINK, "connector_type": "sink"}
    pipeline = _pipeline(connector_config={"topics": "raw-t-s-some_input_topic-batch-v1"}, error_handling_policy="DLQ_AND_CONTINUE")

    config, _ = await build_connector(pipeline, _connection(), template, schema=None)

    assert config["errors.tolerance"] == "all"
    assert config["errors.deadletterqueue.topic.name"] == (
        "quarantine-sales-pg-orders-connector-v1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["SKIP_AND_LOG", "DLQ_AND_CONTINUE", "THRESHOLD"])
async def test_a_source_deploy_config_never_carries_dead_letter_keys(fake_postgres, policy) -> None:
    """Kafka Connect defines no dead-letter queue for a source connector; emitting the keys would be inert config
    that reads as if bad records were being kept."""
    template = {"connector_class": DBZ_PG, "connector_type": "source"}

    config, _ = await build_connector(_pipeline(error_handling_policy=policy), _connection(), template, schema=None)

    assert config["errors.tolerance"] == "all"
    assert not [key for key in config if key.startswith("errors.deadletterqueue")]


@pytest.mark.asyncio
async def test_missing_error_handling_policy_defaults_by_connector_direction(fake_postgres) -> None:
    """A pipeline row from before this field existed (or a caller that built a plain dict without it) must still
    deploy safely: a sink keeps its bad records on the DLQ, a source skips and logs (it has no DLQ)."""
    source = _pipeline()
    source.pop("error_handling_policy", None)
    source_config, _ = await build_connector(
        source, _connection(), {"connector_class": DBZ_PG, "connector_type": "source"}, schema=None,
    )
    sink = _pipeline(connector_config={"topics": "raw-t-s-some_input_topic-batch-v1"})
    sink.pop("error_handling_policy", None)
    sink_config, _ = await build_connector(
        sink, _connection(), {"connector_class": JDBC_SINK, "connector_type": "sink"}, schema=None,
    )

    assert source_config["errors.tolerance"] == "all"
    assert "errors.deadletterqueue.topic.name" not in source_config
    assert sink_config["errors.tolerance"] == "all"
    assert "errors.deadletterqueue.topic.name" in sink_config


@pytest.mark.asyncio
async def test_error_handling_config_applies_to_sinks_too(fake_postgres) -> None:
    """Not just sources — a sink's real deploy config gets the same
    treatment (build_connector applies it uniformly, after _build_connector,
    rather than each connector-class branch needing its own copy)."""
    template = {"connector_class": JDBC_SINK}
    pipeline = _pipeline(connector_config={"topics": "raw-t-s-some_input_topic-batch-v1"}, error_handling_policy="FAIL_FAST")

    config, produced_topic = await build_connector(pipeline, _connection(), template, schema=None)

    assert produced_topic is None
    assert config["errors.tolerance"] == "none"
