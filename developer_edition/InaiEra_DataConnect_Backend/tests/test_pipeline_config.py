import pytest

from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_config import error_handling_connect_config, generate_pipeline_config


@pytest.mark.asyncio
async def test_pipeline_config_generation_includes_topic_connector_and_dlq(fake_postgres) -> None:
    pipeline = PipelineCreate(
        name="Customer CDC",
        template_id="tpl",
        connection_profile_id="conn",
        transform_policy_id="tfp",
        zone="raw",
        domain="erp",
        source_system="sap",
        entity="customers",
        mode="cdc",
        version="v1",
        connector_config={"tasks.max": "2"},
    )

    generated = await generate_pipeline_config(
        org_id="org_1001",
        pipeline=pipeline,
        connector_template={
            "connector_class": "io.debezium.connector.postgresql.PostgresConnector",
            "default_config": {"tasks.max": "1"},
        },
        connection_profile={
            "connection_config": {"database.hostname": "postgres"},
            "secret_ref": "vault://org_1001/source",
        },
        transform_policy={
            "transforms": [
                {
                    "order": 1,
                    "name": "createKey",
                    "type": "ValueToKey",
                    "scope": "value",
                    "config": {"fields": "customer_id"},
                }
            ]
        },
    )

    assert generated["topic_name"] == "raw-erp-sap-customers-cdc-v1"
    assert generated["connector_name"] == "erp--sap--customers--cdc--v1"
    assert generated["generated_config"]["tasks.max"] == "2"
    assert generated["generated_config"]["inai.secret.ref"] == "vault://org_1001/source"
    # GAP-04: this is a SOURCE connector, and Kafka Connect gives sources no dead-letter queue — so it skips and
    # logs bad records and its config carries no DLQ keys at all (they would be silently ignored).
    assert generated["generated_config"]["errors.tolerance"] == "all"
    assert not [key for key in generated["generated_config"] if key.startswith("errors.deadletterqueue")]


@pytest.mark.asyncio
async def test_a_sink_connector_config_carries_its_dead_letter_topic(fake_postgres) -> None:
    pipeline = PipelineCreate(
        name="Customer sink", template_id="tpl", connection_profile_id="conn", zone="prepared", domain="erp",
        source_system="sap", entity="customers", mode="batch", version="v1",
        connector_config={"topics": "raw-erp-sap-customers-cdc-v1"},
    )

    generated = await generate_pipeline_config(
        org_id="org_1001", pipeline=pipeline,
        connector_template={"connector_class": "io.confluent.connect.jdbc.JdbcSinkConnector", "connector_type": "sink"},
        connection_profile={"connection_config": {}},
    )

    # GAP-03: Kafka Connect's own DLQ is a distinct topic (mode=connector)
    # from the quality-validation quarantine (mode=validation) — see
    # app/services/topic_naming.py.
    assert (
        generated["generated_config"]["errors.deadletterqueue.topic.name"]
        == "quarantine-erp-sap-customers-connector-v1"
    )


def _pipeline(**overrides) -> PipelineCreate:
    base = {
        "name": "Customer CDC", "template_id": "tpl", "connection_profile_id": "conn",
        "zone": "raw", "domain": "erp", "source_system": "sap", "entity": "customers",
        "mode": "cdc", "version": "v1",
    }
    base.update(overrides)
    return PipelineCreate(**base)


@pytest.mark.asyncio
async def test_source_connector_gets_explicit_topic_provisioning_config(fake_postgres) -> None:
    """The ticket this closes: a SOURCE connector's generated config must
    carry explicit partitions/replication/retention/cleanup/min-ISR/
    compression/max-message-size/schema-strategy, not rely on implicit
    broker defaults."""
    generated = await generate_pipeline_config(
        org_id="org_1001",
        pipeline=_pipeline(),
        connector_template={"connector_class": "x", "connector_type": "source"},
        connection_profile={"connection_config": {}},
    )

    config = generated["generated_config"]
    assert config["topic.creation.enable"] == "true"
    assert "topic.creation.default.partitions" in config
    assert "topic.creation.default.replication.factor" in config
    assert "topic.creation.default.retention.ms" in config
    assert "topic.creation.default.cleanup.policy" in config
    assert "topic.creation.default.min.insync.replicas" in config
    assert "topic.creation.default.compression.type" in config
    assert "topic.creation.default.max.message.bytes" in config
    assert "key.converter.key.subject.name.strategy" in config


@pytest.mark.asyncio
async def test_sink_connector_gets_no_topic_provisioning_config(fake_postgres) -> None:
    """A sink reads from an existing topic; it never creates one, so
    topic.creation.* config would be meaningless there."""
    generated = await generate_pipeline_config(
        org_id="org_1001",
        pipeline=_pipeline(),
        connector_template={"connector_class": "x", "connector_type": "sink"},
        connection_profile={"connection_config": {}},
    )

    config = generated["generated_config"]
    assert "topic.creation.enable" not in config
    assert "key.converter.key.subject.name.strategy" not in config


@pytest.mark.asyncio
async def test_sink_connector_reads_from_its_own_specified_upstream_topic(fake_postgres) -> None:
    """A sink names the topic it reads FROM via connector_config["topics"] — that must
    survive into the generated config untouched. It used to be unconditionally overwritten
    with the pipeline's own derived topic_name (meaningful only for a source's OUTPUT topic),
    so every deployed sink connector silently consumed from a topic nothing ever produced to."""
    generated = await generate_pipeline_config(
        org_id="org_1001",
        pipeline=_pipeline(entity="customers_copy", connector_config={"topics": "raw__erp__sap__customers__cdc__v1"}),
        connector_template={"connector_class": "x", "connector_type": "sink"},
        connection_profile={"connection_config": {}},
    )

    config = generated["generated_config"]
    assert config["topics"] == "raw__erp__sap__customers__cdc__v1"
    assert generated["topic_name"] != config["topics"]
    assert "topic.prefix" not in config


@pytest.mark.asyncio
async def test_source_connector_respects_explicit_topic_policy_override(fake_postgres) -> None:
    generated = await generate_pipeline_config(
        org_id="org_1001",
        pipeline=_pipeline(),
        connector_template={"connector_class": "x", "connector_type": "source"},
        connection_profile={"connection_config": {}},
        topic_policy={"partitions": 42, "cleanup_policy": "compact"},
    )

    config = generated["generated_config"]
    assert config["topic.creation.default.partitions"] == "42"
    assert config["topic.creation.default.cleanup.policy"] == "compact"


# ── GAP-04: error_handling_policy ─────────────────────────────────────────────
# errors.tolerance used to be hard-coded to "all" — now driven by the
# pipeline's own error_handling_policy, and shared between this
# create-time preview builder and connect_config.build_connector (the
# actual deploy-time config), so the two can't drift the way they used to
# (see error_handling_connect_config's own docstring for that history).

_QUARANTINE_KWARGS = {"domain": "erp", "source_system": "sap", "entity": "customers", "version": "v1"}


def test_fail_fast_sets_tolerance_none_and_no_dlq_config() -> None:
    config = error_handling_connect_config(error_handling_policy="FAIL_FAST", direction="SINK", **_QUARANTINE_KWARGS)

    assert config["errors.tolerance"] == "none"
    assert "errors.deadletterqueue.topic.name" not in config


def test_dlq_and_continue_sets_tolerance_all_and_dlq_config() -> None:
    config = error_handling_connect_config(error_handling_policy="DLQ_AND_CONTINUE", direction="SINK", **_QUARANTINE_KWARGS)

    assert config["errors.tolerance"] == "all"
    assert config["errors.deadletterqueue.topic.name"] == "quarantine-erp-sap-customers-connector-v1"


def test_skip_and_log_tolerates_errors_without_a_dead_letter_queue_for_source_and_sink() -> None:
    for direction in ("SOURCE", "SINK"):
        config = error_handling_connect_config(error_handling_policy="SKIP_AND_LOG", direction=direction, **_QUARANTINE_KWARGS)

        assert config["errors.tolerance"] == "all"
        assert config["errors.log.enable"] == "true"
        assert not [key for key in config if key.startswith("errors.deadletterqueue")]


@pytest.mark.parametrize("policy", ["DLQ_AND_CONTINUE", "THRESHOLD"])
def test_a_source_never_gets_dead_letter_keys_even_if_a_dlq_policy_reaches_the_builder(policy) -> None:
    """Defense in depth: validation rejects these for a source, but the builder must not emit keys Connect ignores."""
    config = error_handling_connect_config(error_handling_policy=policy, direction="SOURCE", **_QUARANTINE_KWARGS)

    assert not [key for key in config if key.startswith("errors.deadletterqueue")]


@pytest.mark.parametrize("bad_policy", ["", "TOLERATE_EVERYTHING", "dlq_and_continue"])
def test_an_unrecognized_policy_fails_closed_not_open(bad_policy) -> None:
    config = error_handling_connect_config(error_handling_policy=bad_policy, direction="SINK", **_QUARANTINE_KWARGS)

    assert config["errors.tolerance"] == "none"
    assert not [key for key in config if key.startswith("errors.deadletterqueue")]


def test_dlq_topic_replication_factor_follows_the_platform_default(monkeypatch) -> None:
    """Without this, Kafka Connect's DeadLetterQueueReporter auto-creates the DLQ
    topic under its OWN hardcoded default (3) — ignoring this platform's configured
    topic_default_replication_factor entirely, unlike every other topic it creates.
    On a single-broker cluster that fails outright with InvalidReplicationFactorException
    the moment a sink task starts (a source hits the same wall, just later, on its
    first bad record) — this is the config knob that actually prevents it."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "topic_default_replication_factor", 1)
    config = error_handling_connect_config(error_handling_policy="DLQ_AND_CONTINUE", direction="SINK", **_QUARANTINE_KWARGS)

    assert config["errors.deadletterqueue.topic.replication.factor"] == "1"


def test_threshold_sets_tolerance_all_and_dlq_config_same_as_dlq_and_continue() -> None:
    """THRESHOLD's Kafka Connect config is identical to DLQ_AND_CONTINUE's —
    Connect itself has no "N errors per window" concept; the difference is
    entirely in app/services/error_threshold.py + runtime_reconciler.py's
    own enforcement on top."""
    config = error_handling_connect_config(error_handling_policy="THRESHOLD", direction="SINK", **_QUARANTINE_KWARGS)

    assert config["errors.tolerance"] == "all"
    assert config["errors.deadletterqueue.topic.name"] == "quarantine-erp-sap-customers-connector-v1"


@pytest.mark.asyncio
async def test_generated_preview_reflects_fail_fast_policy(fake_postgres) -> None:
    generated = await generate_pipeline_config(
        org_id="org_1001",
        pipeline=_pipeline(error_handling_policy="FAIL_FAST"),
        connector_template={"connector_class": "x", "connector_type": "source"},
        connection_profile={"connection_config": {}},
    )

    config = generated["generated_config"]
    assert config["errors.tolerance"] == "none"
    assert "errors.deadletterqueue.topic.name" not in config

