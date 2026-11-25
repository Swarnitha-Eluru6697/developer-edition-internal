"""
Topic names are joined with "-" (raw-erp-sap-customers-cdc-v1) and ONLY with "-": Gravitino, the catalog behind UCS,
cannot load an object name with more than two ".", so the earlier "." join is gone. That makes the separator part of every
segment's grammar — no segment may contain a "-". A name carries no organization (since 2026-11-06). Every name DERIVED from a pipeline (prepared,
quarantine, dead-letter) uses the same separator.
"""

import pytest

from app.models.pipeline import Pipeline
from app.schemas.pipeline import PipelineCreate
from app.services import topic_naming
from app.services.pipeline_service import pipeline_service
from app.services.runtime_ownership import pipeline_by_topic_name
from app.services.sink_topics import SinkTopicError, validate_sink_topics
from app.services.topic_naming import (
    TOPIC_SEPARATOR,
    TopicParts,
    build_catalog_qualified_name,
    build_connector_quarantine_topic_name,
    build_topic_name,
    build_validation_quarantine_topic_name,
    build_worker_quarantine_topic_name,
    to_catalog_qualified_name,
    to_prepared_topic,
    validate_topic_parts,
)

PARTS = TopicParts(
    zone="raw", domain="erp", source_system="sap", entity="customers", mode="cdc", version="v1",
)
KWARGS = {"domain": "erp", "source_system": "sap", "entity": "customers", "version": "v1"}


# ── building and validating names ──────────────────────────────────────────────────────────────────────────────────


def test_a_topic_is_joined_with_dashes() -> None:
    assert TOPIC_SEPARATOR == "-"
    assert build_topic_name(PARTS) == "raw-erp-sap-customers-cdc-v1"


def test_the_name_has_no_dot_so_gravitino_can_load_it() -> None:
    assert "." not in build_topic_name(PARTS)


@pytest.mark.parametrize("field", ["domain", "source_system", "entity"])
def test_a_dash_is_refused_inside_a_segment(field) -> None:
    """Otherwise domain="a-b"+source_system="c" and domain="a"+source_system="b-c" would be the same topic."""
    parts = TopicParts(**{**PARTS.__dict__, field: "a-b"})
    with pytest.raises(ValueError, match="'-' is the separator"):
        validate_topic_parts(parts)


def test_the_two_names_that_would_collide_with_a_naive_dash_join_are_both_refused() -> None:
    for domain, source_system in (("a-b", "c"), ("a", "b-c")):
        with pytest.raises(ValueError, match="'-' is the separator"):
            validate_topic_parts(TopicParts(**{**PARTS.__dict__, "domain": domain, "source_system": source_system}))


def test_a_dot_is_no_longer_a_separator_or_a_legal_character() -> None:
    assert not hasattr(topic_naming, "LEGACY_TOPIC_SEPARATOR")
    assert not hasattr(topic_naming, "topic_separator_for_org")
    with pytest.raises(TypeError):
        build_topic_name(PARTS, separator=".")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="must be lowercase"):
        validate_topic_parts(TopicParts(**{**PARTS.__dict__, "domain": "a.b"}))
    assert TopicParts.try_parse("org_1001.raw.erp.sap.customers.cdc.v1") is None     # a dotted name is not a topic any more


# ── names derived from a pipeline ──────────────────────────────────────────────────────────────────────────────────


def test_every_derived_topic_is_dashed() -> None:
    for builder, mode in (
        (build_validation_quarantine_topic_name, "validation"),
        (build_connector_quarantine_topic_name, "connector"),
        (build_worker_quarantine_topic_name, "quality_worker"),
    ):
        assert builder(**KWARGS) == f"quarantine-erp-sap-customers-{mode}-v1"


def test_the_prepared_topic_swaps_only_the_zone_part() -> None:
    assert to_prepared_topic("raw-erp-sap-customers-cdc-v1") == "prepared-erp-sap-customers-cdc-v1"
    # a domain, source or entity that happens to be called "raw" is not the zone
    assert to_prepared_topic("raw-raw-raw-raw-cdc-v1") == "prepared-raw-raw-raw-cdc-v1"


@pytest.mark.parametrize("topic", ["prepared-erp-sap-customers-cdc-v1", "org_1001.curated.erp.sap.customers.cdc.v1", "plain"])
def test_a_topic_that_is_not_raw_is_left_alone(topic) -> None:
    assert to_prepared_topic(topic) == topic


def test_the_catalog_identifier_matches_the_parts() -> None:
    assert to_catalog_qualified_name(build_topic_name(PARTS)) == build_catalog_qualified_name(PARTS)


# ── a sink's input topic ───────────────────────────────────────────────────────────────────────────────────────────


def test_a_sink_may_read_a_well_formed_topic() -> None:
    assert validate_sink_topics("raw-erp-sap-customers-cdc-v1")


@pytest.mark.parametrize(
    "topic",
    [
        "org_1001-custom",  # not one of the six-part names this platform builds
        "org_1001.raw.erp.sap.customers.cdc.v1",  # the dotted form is gone
        "acme-corp-raw-erp-sap-customers-cdc-v1",  # not a six-part name either (a name from before 2026-11-06 starts with ONE org token)
    ],
)
def test_a_name_that_is_not_a_topic_is_invalid_topic_name(topic) -> None:
    with pytest.raises(SinkTopicError, match="not a valid input topic name") as exc_info:
        validate_sink_topics(topic)
    assert exc_info.value.code == "INVALID_TOPIC_NAME"


# ── creation, editing and attribution ────────────────────────────────────────────────────────────────────────────────


def _seed(fake_postgres, org_id: str, connector_type: str = "source") -> tuple[dict, dict]:
    klass = "io.confluent.connect.jdbc.JdbcSinkConnector" if connector_type == "sink" else "io.confluent.connect.jdbc.JdbcSourceConnector"
    template = fake_postgres.create(
        "connector_templates", {"connector_class": klass, "connector_type": connector_type, "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": org_id, "name": "c", "connection_config": {}})
    return template, connection


def _payload(template: dict, connection: dict, **overrides) -> PipelineCreate:
    base = {
        "name": "P", "template_id": template["id"], "connection_profile_id": connection["id"], "zone": "raw",
        "domain": "sales", "source_system": "pg", "entity": "orders", "mode": "batch", "version": "v1",
    }
    base.update(overrides)
    return PipelineCreate(**base)


@pytest.mark.asyncio
async def test_a_pipeline_created_now_gets_a_dashed_topic_and_records_it(fake_postgres) -> None:
    template, connection = _seed(fake_postgres, "org_a")

    created = await pipeline_service.create_pipeline("org_a", _payload(template, connection))

    assert created["topic_name"] == "raw-sales-pg-orders-batch-v1"
    assert "topic_separator" not in created                        # the "." join is gone, so there is nothing to record
    assert not hasattr(Pipeline, "topic_separator")
    assert created["generated_config"]["topics"] == created["topic_name"]
    assert "." not in created["topic_name"]


@pytest.mark.asyncio
async def test_a_dashed_sink_reads_the_dashed_source_and_gets_a_dashed_dead_letter_topic(fake_postgres) -> None:
    source_template, connection = _seed(fake_postgres, "org_a")
    sink_template, _ = _seed(fake_postgres, "org_a", "sink")
    source = await pipeline_service.create_pipeline("org_a", _payload(source_template, connection))

    sink = await pipeline_service.create_pipeline(
        "org_a", _payload(sink_template, connection, zone="prepared", entity="orders_copy", connector_config={"topics": source["topic_name"]}),
    )

    assert sink["generated_config"]["topics"] == "raw-sales-pg-orders-batch-v1"
    assert sink["generated_config"]["errors.deadletterqueue.topic.name"] == "quarantine-sales-pg-orders_copy-connector-v1"


@pytest.mark.asyncio
async def test_an_org_id_with_capitals_and_dashes_no_longer_blocks_a_pipeline_because_the_name_carries_no_org(fake_postgres) -> None:
    """The reason for the change: ORG-ACME-001 broke the seven-part grammar. A topic and a connector name are built without it now."""
    template, connection = _seed(fake_postgres, "ORG-ACME-001")

    created = await pipeline_service.create_pipeline("ORG-ACME-001", _payload(template, connection))

    assert created["topic_name"] == "raw-sales-pg-orders-batch-v1"
    assert created["connector_name"] == "sales--pg--orders--batch--v1"
    assert "ORG" not in created["topic_name"] + created["connector_name"]


@pytest.mark.asyncio
async def test_ownership_maps_a_pipeline_to_its_raw_prepared_and_quarantine_topics(fake_postgres) -> None:
    pipeline = fake_postgres.create("pipelines", {
        "org_id": "org_a", "domain": "sales", "source_system": "pg", "entity": "orders", "version": "v1", "direction": "SOURCE",
        "connector_config": {"tables": ["orders"]}, "connector_name": "c", "topic_name": "raw-sales-pg-orders-batch-v1",
    })

    owned = await pipeline_by_topic_name("org_a")

    assert owned["raw-sales-pg-orders-batch-v1"]["id"] == pipeline["id"]
    assert owned["prepared-sales-pg-orders-batch-v1"]["id"] == pipeline["id"]
    assert owned["quarantine-sales-pg-orders-validation-v1"]["id"] == pipeline["id"]
    assert owned["quarantine-sales-pg-orders-connector-v1"]["id"] == pipeline["id"]


# ── BUG-05: typed errors ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topic", "code"),
    [
        ("raw-sales-pg-sales-force-batch-v1", "INVALID_TOPIC_NAME"),   # seven parts: not a topic name
        ("org_a.raw.sales.pg.orders.batch.v1", "INVALID_TOPIC_NAME"),  # the retired dotted form
        ("org_a-*", "INVALID_TOPIC_NAME"),                               # a pattern
    ],
)
async def test_creating_a_sink_returns_a_typed_code_for_a_bad_topic(fake_postgres, topic, code) -> None:
    from app.core.errors import ValidationError

    template, connection = _seed(fake_postgres, "org_a", "sink")

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline("org_a", _payload(template, connection, connector_config={"topics": topic}))

    assert exc_info.value.code == code
    assert exc_info.value.status_code == 422
    assert exc_info.value.field_errors[0].field == "connector_config.topics"
    assert exc_info.value.details == {"topic": topic}
