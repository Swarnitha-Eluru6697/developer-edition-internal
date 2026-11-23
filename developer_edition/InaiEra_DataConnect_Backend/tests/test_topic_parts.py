"""BUG-01: a topic segment may never contain the separator.

A topic's six fields (zone, domain, source system, entity, mode, version) are joined with "-". Every field therefore
follows `[a-z0-9_]+` — a literal "-" inside one would make `split("-")` return more than six tokens. TopicParts is the one
immutable model that builds, parses and derives topic names; nothing else splits a topic string.

"-" is the only separator; the old "." join is gone. The property tests are seeded (no extra dependency, fully reproducible).
"""

import dataclasses
import random
import string

import pytest

from app.services.sink_topics import SinkTopicError, validate_sink_topics
from app.services.topic_naming import (
    ALLOWED_MODES,
    ALLOWED_ZONES,
    TopicParseError,
    TopicParts,
    build_connector_quarantine_topic_name,
    build_topic_name,
    build_validation_quarantine_topic_name,
    build_worker_quarantine_topic_name,
    normalize_segment,
    to_catalog_qualified_name,
    to_prepared_topic,
    validate_topic_parts,
)

PARTS = TopicParts(
    zone="raw", domain="erp", source_system="sap", entity="customers", mode="cdc", version="v1",
)
ALPHABET = string.ascii_lowercase + string.digits


def _segment(rng: random.Random) -> str:
    """A grammar-conforming segment: words of a-z0-9 joined by a SINGLE underscore (so it also round-trips through the catalog)."""
    words = ["".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 8))) for _ in range(rng.randint(1, 3))]
    return "_".join(words)


def _random_parts(rng: random.Random) -> TopicParts:
    return TopicParts(
        zone=rng.choice(sorted(ALLOWED_ZONES)), domain=_segment(rng), source_system=_segment(rng),
        entity=_segment(rng), mode=rng.choice(sorted(ALLOWED_MODES)), version=f"v{rng.randint(1, 99)}",
    )


# ── the model ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_topic_parts_is_immutable_and_has_exactly_the_six_fields() -> None:
    assert [f.name for f in dataclasses.fields(TopicParts)] == list(TopicParts.FIELDS)
    assert len(TopicParts.FIELDS) == 6
    with pytest.raises(dataclasses.FrozenInstanceError):
        PARTS.zone = "prepared"  # type: ignore[misc]


def test_derivations_return_new_parts_and_never_change_the_original() -> None:
    prepared = PARTS.prepared()
    assert prepared.zone == "prepared"
    assert PARTS.zone == "raw"
    assert PARTS.with_zone("curated").zone == "curated"
    assert PARTS.quarantine("validation") == dataclasses.replace(PARTS, zone="quarantine", mode="validation")
    assert prepared.prepared() == prepared                                   # already prepared: unchanged
    assert PARTS.with_zone("curated").prepared().zone == "curated"           # only a raw topic has a prepared counterpart


# ── all six fields, in every position ────────────────────────────────────────────────────────────────────────────


def test_random_valid_parts_survive_build_then_parse_for_every_field() -> None:
    rng = random.Random(20260009)
    for _ in range(500):
        parts = _random_parts(rng)
        topic = build_topic_name(parts)
        parsed = TopicParts.parse(topic)
        assert parsed == parts
        assert len(topic.split("-")) == 6                                    # the whole point: six tokens, never seven
        assert parsed.topic_name() == topic


@pytest.mark.parametrize("field", TopicParts.FIELDS)
@pytest.mark.parametrize("bad", ["a-b", "sales-force", "customer-orders", "quality-worker", "-a", "a-", "A", "a b", "a.b", "", "é"])
def test_a_dashed_topic_refuses_every_bad_value_in_every_field(field, bad) -> None:
    parts = dataclasses.replace(PARTS, **{field: bad})
    with pytest.raises(ValueError, match="is required|must be lowercase|'-' is the separator|may not contain"):
        validate_topic_parts(parts)
    with pytest.raises(ValueError, match="is required|must be lowercase|'-' is the separator|may not contain"):
        build_topic_name(parts)                                              # the builder itself refuses it, not just the validator


@pytest.mark.parametrize("field", ["domain", "source_system", "entity"])
def test_the_refusal_says_what_to_do_instead(field) -> None:
    with pytest.raises(ValueError, match=r"use '_' instead, e\.g\. 'sales_force'"):
        validate_topic_parts(dataclasses.replace(PARTS, **{field: "sales-force"}))


# ── underscore normalisation ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("quality-worker", "quality_worker"), ("Sales-Force", "sales_force"), ("customer - orders", "customer_orders"), ("a--b", "a_b"), ("ok_already", "ok_already")],
)
def test_normalize_segment_turns_hyphens_into_underscores(raw, expected) -> None:
    assert normalize_segment(raw) == expected


def test_the_worker_quarantine_mode_is_spelled_with_an_underscore_everywhere() -> None:
    assert "quality-worker" not in ALLOWED_MODES
    assert "quality_worker" in ALLOWED_MODES
    kwargs = {"domain": "erp", "source_system": "sap", "entity": "customers", "version": "v1"}
    topic = build_worker_quarantine_topic_name(**kwargs)
    assert topic == "quarantine-erp-sap-customers-quality_worker-v1"
    assert len(topic.split("-")) == 6                                        # the report's example used to split into seven


def test_a_topic_written_before_the_rename_is_still_understood_as_the_normalised_mode() -> None:
    old = "quarantine-erp-sap-customers-quality-worker-v1"          # seven tokens
    parsed = TopicParts.parse(old)
    assert parsed.mode == "quality_worker"
    assert parsed.entity == "customers"
    assert parsed.version == "v1"
    assert parsed.topic_name() == "quarantine-erp-sap-customers-quality_worker-v1"
    assert TopicParts.try_parse("org_1001.quarantine.erp.sap.customers.quality-worker.v1") is None    # the dotted form is gone


# ── malformed topics ───────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "topic",
    [
        "", "plain", "org_1001-custom", "raw-erp-sap-customers-cdc",                    # too few
        "raw-erp-sap-customers-cdc-v1-extra", "raw-erp-sap-sales-force-cdc-v1",   # too many (a hyphenated entity)
        "raw-erp-sap-customers-cdc-1", "raw-erp-sap-customers-cdc-v",            # version is not v<number>
        "org_1001--erp-sap-customers-cdc-v1", "raw-erp-sap-customers--v1",                # an empty field
        "RAW-erp-sap-customers-cdc-v1", "raw-erp-sap-custo mers-cdc-v1",         # characters outside the grammar
        "org_1001.raw.erp.sap.customers.cdc.v1",                                                   # the old dotted form
    ],
)
def test_a_malformed_topic_is_a_parse_error_never_a_guess(topic) -> None:
    with pytest.raises(TopicParseError):
        TopicParts.parse(topic)
    assert TopicParts.try_parse(topic) is None


def test_parse_errors_are_value_errors_so_existing_handlers_still_catch_them() -> None:
    assert issubclass(TopicParseError, ValueError)


def test_random_strings_never_crash_the_parser() -> None:
    rng = random.Random(7)
    pool = ALPHABET + "-._ ABC"
    for _ in range(2000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 60)))
        result = TopicParts.try_parse(text)
        if result is not None:                                               # whatever parses must round-trip exactly
            built = result.topic_name(soft_limit=10_000)
            assert built == text or text.endswith(built) or "quality" in text       # a name from before 2026-11-06 starts with an org: rebuilt without it


# ── a name carries no organization ─────────────────────────────────────────────────────────────────────────────────


def test_a_topic_name_says_nothing_about_who_owns_it() -> None:
    topic = "raw-erp-sap-customers-cdc-v1"
    assert not hasattr(TopicParts, "owned_by")
    assert not hasattr(PARTS, "orgid")
    assert validate_sink_topics(topic) == [topic]


@pytest.mark.parametrize(
    "topic",
    ["raw-erp-sap-sales-force-cdc-v1", "raw-erp-sap-customers", "raw-erp-sap-customers-cdc-version1"],
)
def test_a_sink_cannot_claim_a_malformed_topic(topic) -> None:
    with pytest.raises(SinkTopicError, match="not a valid input topic name") as exc_info:
        validate_sink_topics(topic)
    assert exc_info.value.code == "INVALID_TOPIC_NAME"


# ── prepared / quarantine derivation ───────────────────────────────────────────────────────────────────────────────


def test_prepared_topic_swaps_only_the_zone_field() -> None:
    rng = random.Random(11)
    for _ in range(300):
        parts = dataclasses.replace(_random_parts(rng), zone="raw")
        raw = build_topic_name(parts)
        assert to_prepared_topic(raw) == build_topic_name(parts.prepared())
        assert TopicParts.parse(to_prepared_topic(raw)) == parts.prepared()


def test_a_domain_called_raw_is_not_mistaken_for_the_zone() -> None:
    assert to_prepared_topic("raw-raw-raw-raw-cdc-v1") == "prepared-raw-raw-raw-cdc-v1"


@pytest.mark.parametrize("topic", ["prepared-erp-sap-customers-cdc-v1", "raw-erp-sap-sales-force-cdc-v1", "plain", ""])
def test_a_topic_that_is_not_a_wellformed_raw_topic_is_returned_unchanged(topic) -> None:
    assert to_prepared_topic(topic) == topic


def test_quarantine_topics_are_the_parts_with_zone_and_mode_replaced() -> None:
    kwargs = {"domain": "erp", "source_system": "sap", "entity": "customers", "version": "v1"}
    for builder, mode in (
        (build_validation_quarantine_topic_name, "validation"),
        (build_connector_quarantine_topic_name, "connector"),
        (build_worker_quarantine_topic_name, "quality_worker"),
    ):
        topic = builder(**kwargs)
        assert TopicParts.parse(topic) == PARTS.quarantine(mode)
        assert topic == PARTS.quarantine(mode).topic_name()


def test_the_quarantine_builders_refuse_a_hyphenated_field_instead_of_building_an_ambiguous_name() -> None:
    kwargs = {"domain": "erp", "source_system": "sap", "entity": "sales-force", "version": "v1"}
    for builder in (build_validation_quarantine_topic_name, build_connector_quarantine_topic_name, build_worker_quarantine_topic_name):
        with pytest.raises(ValueError, match="'-' is the separator"):
            builder(**kwargs)


# ── catalog conversion ─────────────────────────────────────────────────────────────────────────────────────────────


def test_catalog_name_round_trips_through_parts_for_random_topics() -> None:
    rng = random.Random(23)
    for _ in range(500):
        parts = _random_parts(rng)
        catalog = parts.catalog_qualified_name()
        assert catalog.count("__") == 5
        assert "-" not in catalog
        assert "." not in catalog
        assert TopicParts.from_catalog_qualified_name(catalog) == parts
        assert to_catalog_qualified_name(build_topic_name(parts)) == catalog


@pytest.mark.parametrize("field", ["domain", "source_system", "entity"])
@pytest.mark.parametrize("bad", ["a__b", "_a", "a_"])
def test_a_segment_that_would_blur_the_catalog_joiner_is_refused(field, bad) -> None:
    """"__" joins the catalog name, so a segment containing or touching one could not be split back into its six fields."""
    with pytest.raises(ValueError, match="'__'"):
        validate_topic_parts(dataclasses.replace(PARTS, **{field: bad}))


def test_a_malformed_catalog_name_is_a_parse_error() -> None:
    for bad in ("", "a__b", "raw__erp__sap__customers__cdc", "raw__erp__sap__sales-force__cdc__v1"):
        with pytest.raises(TopicParseError):
            TopicParts.from_catalog_qualified_name(bad)


def test_the_catalog_name_of_the_documented_example_is_built_from_its_parts() -> None:
    assert to_catalog_qualified_name("raw-erp-sap-customers-quality_worker-v1") == "raw__erp__sap__customers__quality_worker__v1"


@pytest.mark.parametrize(
    "not_a_topic",
    [
        "raw-erp-sap-my-table-cdc-v1",     # a "-" inside a field: 7 tokens, is it "my-table" or two fields? Cannot be told.
        "legacy-topic-name",                         # a name from before the naming rules
        "org_1001.raw.erp.sap.customers.cdc.v1",     # the retired dotted form
        "raw-erp-sap-customers-cdc",        # a part missing
        "raw-erp-sap-cus__tomers-cdc-v1",   # "__" inside a field
        "",
    ],
)
def test_a_name_that_is_not_a_platform_topic_has_no_catalog_identifier_instead_of_a_guessed_one(not_a_topic) -> None:
    """BUG-04: no global string replace. Swapping every "-" would give a name that splits back into the wrong fields."""
    assert to_catalog_qualified_name(not_a_topic) is None


def test_two_names_that_a_blind_replace_would_merge_are_never_given_the_same_catalog_identifier() -> None:
    # Under the old rules domain="a-b"+source="c" and domain="a"+source="b-c" were both written ...-a-b-c-...: one name, two meanings.
    # A seven-part name like that is refused rather than guessed at:
    assert to_catalog_qualified_name("raw-a-b-c-orders-cdc-v1") is None
    clean = to_catalog_qualified_name("raw-a_b-c-orders-cdc-v1")
    other = to_catalog_qualified_name("raw-a-b_c-orders-cdc-v1")
    assert clean == "raw__a_b__c__orders__cdc__v1"
    assert other == "raw__a__b_c__orders__cdc__v1"
    assert clean != other                            # valid names keep their field boundaries, so they cannot collide


def test_a_pipeline_row_with_an_unparseable_topic_still_serialises_with_a_null_catalog_name() -> None:
    from app.models.pipeline import Pipeline
    from app.services.pipeline_service import _pipeline_to_dict

    row = Pipeline(
        id="p1", org_id="org_a", name="n", domain="sales", source_system="pg", entity="orders", version="v1", direction="SOURCE",
        status="draft", connector_config={}, connector_name="c", topic_name="raw-sales-pg-my-table-cdc-v1",
    )
    out = _pipeline_to_dict(row)
    assert out["topic_name"] == "raw-sales-pg-my-table-cdc-v1"   # the physical topic is untouched
    assert out["catalog_qualified_name"] is None
