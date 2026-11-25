import pytest

from app.services.topic_naming import (
    TopicParseError,
    TopicParts,
    build_catalog_qualified_name,
    build_connector_name,
    build_connector_quarantine_topic_name,
    build_topic_name,
    build_validation_quarantine_topic_name,
    parse_connector_name,
    to_catalog_qualified_name,
    validate_topic_parts,
    validation_quarantine_topic_for,
)


def test_build_topic_name_matches_design() -> None:
    parts = TopicParts(
        zone="raw",
        domain="erp",
        source_system="sap",
        entity="customers",
        mode="cdc",
        version="v1",
    )

    validate_topic_parts(parts)

    # BUG-01: "." is the real, physical Kafka topic name (the agreed
    # InaiEra contract) — never "__", which is only the separate,
    # Gravitino-safe catalog_qualified_name (see below).
    assert build_topic_name(parts) == "raw-erp-sap-customers-cdc-v1"
    assert build_connector_name(parts) == "erp--sap--customers--cdc--v1"


def test_build_catalog_qualified_name_is_the_gravitino_safe_double_underscore_identity() -> None:
    parts = TopicParts(
        zone="raw",
        domain="erp",
        source_system="sap",
        entity="customers",
        mode="cdc",
        version="v1",
    )

    assert build_catalog_qualified_name(parts) == "raw__erp__sap__customers__cdc__v1"
    # Trivially inter-derivable from the physical topic name.
    assert to_catalog_qualified_name(build_topic_name(parts)) == build_catalog_qualified_name(parts)


def test_topic_version_must_match_contract() -> None:
    parts = TopicParts(
        zone="raw",
        domain="erp",
        source_system="sap",
        entity="customers",
        mode="cdc",
        version="1",
    )

    with pytest.raises(ValueError, match="version"):
        validate_topic_parts(parts)


# ── GAP-03: connector-DLQ vs quality-validation quarantine ───────────────────
# Two fundamentally different failure classes — a Kafka Connect connector
# failure (bad serialization, a converter error, a malformed source message)
# versus a DataConnect quality-rule failure (a missing field, an invalid
# value) — must land on two distinct topics, not one shared "quarantine"
# topic that made monitoring unable to tell which kind of failure it was
# looking at.


def _quarantine_kwargs() -> dict:
    return {"domain": "crm", "source_system": "salesforce", "entity": "accounts", "version": "v1"}


def test_validation_and_connector_quarantine_topics_are_distinct() -> None:
    validation = build_validation_quarantine_topic_name(**_quarantine_kwargs())
    connector = build_connector_quarantine_topic_name(**_quarantine_kwargs())

    assert validation == "quarantine-crm-salesforce-accounts-validation-v1"
    assert connector == "quarantine-crm-salesforce-accounts-connector-v1"
    assert validation != connector


def test_quarantine_topics_are_both_zone_quarantine() -> None:
    """Same zone (both are quarantine-zone topics for monitoring/ACL
    purposes) — only the mode segment tells them apart."""
    validation = build_validation_quarantine_topic_name(**_quarantine_kwargs())
    connector = build_connector_quarantine_topic_name(**_quarantine_kwargs())

    assert validation.split("-")[0] == "quarantine"
    assert connector.split("-")[0] == "quarantine"



# ── a sink's rule failures are told apart from a source's by the mode ─────────


def _pipeline(direction: str) -> dict:
    return {"direction": direction, "domain": "shop", "source_system": "pg", "entity": "products", "version": "v1"}


def test_a_sinks_validation_quarantine_uses_mode_validation_sink_and_a_sources_keeps_validation() -> None:
    assert validation_quarantine_topic_for(_pipeline("SOURCE")) == "quarantine-shop-pg-products-validation-v1"
    assert validation_quarantine_topic_for(_pipeline("SINK")) == "quarantine-shop-pg-products-validation_sink-v1"
    assert validation_quarantine_topic_for(_pipeline("sink")) == "quarantine-shop-pg-products-validation_sink-v1"
    # same fields, opposite directions: two topics, never one shared
    assert validation_quarantine_topic_for(_pipeline("SOURCE")) != validation_quarantine_topic_for(_pipeline("SINK"))


def test_the_sink_quarantine_name_is_a_valid_six_part_topic() -> None:
    name = validation_quarantine_topic_for(_pipeline("SINK"))
    parts = TopicParts.parse(name)
    assert (parts.zone, parts.mode) == ("quarantine", "validation_sink")
    validate_topic_parts(parts)   # the mode is in the platform vocabulary


# ── 2026-11-06: a name carries no organization ──────────────────────────────────────────────────────────────────────


def test_a_topic_and_a_connector_name_carry_no_organization() -> None:
    parts = TopicParts(zone="raw", domain="shop", source_system="pg", entity="products", mode="batch", version="v1")

    assert build_topic_name(parts) == "raw-shop-pg-products-batch-v1"
    assert build_connector_name(parts) == "shop--pg--products--batch--v1"
    assert "orgid" not in TopicParts.FIELDS


def test_a_topic_built_before_the_change_still_parses_with_its_organization_dropped() -> None:
    assert TopicParts.parse("org_1001-prepared-shop-pg-products-batch-v1") == TopicParts.parse("prepared-shop-pg-products-batch-v1")
    assert TopicParts.parse("org_1001-quarantine-shop-pg-products-validation-v1").mode == "validation"
    assert TopicParts.parse("org_1001-quarantine-shop-pg-products-quality-worker-v1").mode == "quality_worker"      # pre-grammar spelling
    assert to_catalog_qualified_name("org_1001-raw-shop-pg-products-batch-v1") == "raw__shop__pg__products__batch__v1"


def test_a_name_that_is_neither_six_parts_nor_the_old_seven_is_refused() -> None:
    for bad in ("raw-shop-pg-products-batch", "raw-shop-pg-products-batch-v1-extra", "shop-pg-products-batch-v1-x", "raw.shop.pg.products.batch.v1"):
        with pytest.raises(TopicParseError):
            TopicParts.parse(bad)


def test_a_connector_name_built_before_the_change_still_parses() -> None:
    assert parse_connector_name("org_1001--shop--pg--products--batch--v1") == parse_connector_name("shop--pg--products--batch--v1")
    with pytest.raises(ValueError, match="orgid"):
        parse_connector_name("ORG_1--shop--pg--products--batch--v1")        # the old organization part still has to be a plain lowercase id
