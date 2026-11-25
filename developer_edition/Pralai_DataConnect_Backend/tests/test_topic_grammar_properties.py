"""
Hardening: property-based tests of the topic grammar (app/services/topic_naming.py).

The example-based tests pin known cases; these state the INVARIANTS the whole design rests on and let Hypothesis hunt for a counter-example:

  * a topic name is built from, and parses back to, exactly the six fields it came from (no field is lost, merged or reinterpreted);
  * two different identities can never produce the same name, nor the same catalog identifier (no collisions);
  * "-" is the separator, so it can never be inside a field — injecting one makes the name unparseable instead of ambiguous;
  * the catalog identifier is the same six fields joined with "__" and round-trips too;
  * derived names (prepared / quarantine) change only the fields they claim to;
  * parsing is TOTAL: any string at all either parses to fields that rebuild it, or is cleanly rejected — it never raises anything else;
  * a name carries no organization: any well-formed name can be read by a sink, and a name from before 2026-11-06 still parses.

Failures print the minimal counter-example Hypothesis found; `.hypothesis/` keeps it so the next run replays it first.
"""

import re
import string

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.services.sink_topics import SinkTopicError, validate_sink_topics
from app.services.topic_naming import (
    ALLOWED_MODES,
    ALLOWED_ZONES,
    CATALOG_SEPARATOR,
    TOPIC_SEPARATOR,
    TopicParseError,
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

PROPERTY = settings(max_examples=400, deadline=None)
# every way the grammar can refuse a name (see _validate_topic_segment / _validate_fields / build_topic_name)
GRAMMAR_ERROR = r"must be lowercase|may not contain|must match|exceeds soft limit|is required"

# A grammar-valid free segment: a-z0-9 words joined by single "_", never leading/trailing "_", never "__" (see _validate_topic_segment).
segment = st.from_regex(r"[a-z0-9]{1,8}(_[a-z0-9]{1,8}){0,2}", fullmatch=True)
version = st.integers(min_value=0, max_value=10**6).map(lambda n: f"v{n}")


@st.composite
def topic_parts(draw) -> TopicParts:
    return TopicParts(
        zone=draw(st.sampled_from(sorted(ALLOWED_ZONES))), domain=draw(segment),
        source_system=draw(segment), entity=draw(segment), mode=draw(st.sampled_from(sorted(ALLOWED_MODES))), version=draw(version),
    )


# ── building and parsing ───────────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(topic_parts())
def test_a_built_name_parses_back_to_exactly_its_fields(parts) -> None:
    assert TopicParts.parse(build_topic_name(parts)) == parts


@PROPERTY
@given(topic_parts())
def test_a_built_name_is_six_dash_joined_fields_and_has_no_dot(parts) -> None:
    topic = build_topic_name(parts)
    assert topic.count(TOPIC_SEPARATOR) == 5
    assert "." not in topic
    assert re.fullmatch(r"[a-z0-9_-]+", topic)
    assert topic == TOPIC_SEPARATOR.join((parts.zone, parts.domain, parts.source_system, parts.entity, parts.mode, parts.version))


@PROPERTY
@given(topic_parts(), topic_parts())
def test_two_different_identities_never_share_a_name_or_a_catalog_identifier(a, b) -> None:
    if build_topic_name(a) == build_topic_name(b):
        assert a == b
    if build_catalog_qualified_name(a) == build_catalog_qualified_name(b):
        assert a == b


@PROPERTY
@given(topic_parts())
def test_the_catalog_identifier_is_six_double_underscore_joined_fields_and_round_trips(parts) -> None:
    catalog = build_catalog_qualified_name(parts)
    assert catalog.count(CATALOG_SEPARATOR) == 5          # a segment can never contain "__", so it is exactly the joiners
    assert "-" not in catalog
    assert "." not in catalog
    assert TopicParts.from_catalog_qualified_name(catalog) == parts
    assert to_catalog_qualified_name(build_topic_name(parts)) == catalog


# ── the separator can never hide inside a field ────────────────────────────────────────────────────────────────────

@PROPERTY
@given(topic_parts(), st.integers(min_value=0, max_value=5), st.data())
def test_putting_a_dash_inside_a_field_makes_the_name_unparseable_not_ambiguous(parts, index, data) -> None:
    fields = [getattr(parts, name) for name in TopicParts.FIELDS]
    value = fields[index]
    assume(len(value) >= 2)
    cut = data.draw(st.integers(min_value=1, max_value=len(value) - 1))
    fields[index : index + 1] = [value[:cut], value[cut:]]
    text = TOPIC_SEPARATOR.join(fields)                    # seven tokens: could mean "a-b" in one field, or two fields
    # The ONE legitimately understood seven-token form is the pre-grammar mode spelling `quality-worker` (read, never built).
    assume(not (index == 4 and fields[4:6] == ["quality", "worker"]))
    assert TopicParts.try_parse(text) is None
    assert to_catalog_qualified_name(text) is None         # BUG-04: no guessed identifier for a name that cannot be parsed


@PROPERTY
@given(topic_parts(), st.sampled_from(TopicParts.FIELDS), st.sampled_from(["-", ".", " ", "A", "é", "/", ":", "__"]))
def test_a_field_containing_a_forbidden_character_is_refused_when_building(parts, name, bad) -> None:
    broken = TopicParts(**{**parts.__dict__, name: getattr(parts, name) + bad + "x"})
    with pytest.raises(ValueError, match=GRAMMAR_ERROR):
        build_topic_name(broken)
    with pytest.raises(ValueError, match=GRAMMAR_ERROR):
        validate_topic_parts(broken)


@PROPERTY
@given(topic_parts(), st.sampled_from(["", "v", "1", "V1", "v-1", "v1.0", "v1a", " v1", "version1"]))
def test_only_v_followed_by_digits_is_a_version(parts, bad_version) -> None:
    with pytest.raises(ValueError, match=GRAMMAR_ERROR):
        build_topic_name(TopicParts(**{**parts.__dict__, "version": bad_version}))


@PROPERTY
@given(topic_parts())
def test_a_name_over_the_soft_limit_is_refused_and_at_the_limit_is_accepted(parts) -> None:
    topic = build_topic_name(parts)
    assert build_topic_name(parts, soft_limit=len(topic)) == topic
    with pytest.raises(ValueError, match=GRAMMAR_ERROR):
        build_topic_name(parts, soft_limit=len(topic) - 1)


# ── derived names ──────────────────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(topic_parts())
def test_the_prepared_counterpart_changes_only_the_zone_and_only_for_raw(parts) -> None:
    topic = build_topic_name(parts)
    derived = to_prepared_topic(topic)
    if parts.zone == "raw":
        assert TopicParts.parse(derived) == TopicParts(**{**parts.__dict__, "zone": "prepared"})
    else:
        assert derived == topic


@PROPERTY
@given(topic_parts())
def test_quarantine_names_keep_the_identity_and_differ_only_in_zone_and_mode(parts) -> None:
    kwargs = {"domain": parts.domain, "source_system": parts.source_system, "entity": parts.entity, "version": parts.version}
    for builder, mode in (
        (build_validation_quarantine_topic_name, "validation"),
        (build_connector_quarantine_topic_name, "connector"),
        (build_worker_quarantine_topic_name, "quality_worker"),
    ):
        derived = TopicParts.parse(builder(**kwargs))
        assert (derived.zone, derived.mode) == ("quarantine", mode)
        assert (derived.domain, derived.source_system, derived.entity, derived.version) == (
            parts.domain, parts.source_system, parts.entity, parts.version)


# ── parsing is total ───────────────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(st.text(max_size=120))
def test_parsing_any_string_never_raises_anything_but_a_clean_rejection(text) -> None:
    try:
        parts = TopicParts.parse(text)
    except TopicParseError:
        assert TopicParts.try_parse(text) is None
        assert to_catalog_qualified_name(text) is None
        return
    assert TopicParts.try_parse(text) == parts
    rebuilt = TOPIC_SEPARATOR.join(getattr(parts, name) for name in TopicParts.FIELDS)
    # The only names that parse but do not rebuild to themselves: the legacy mode spelling `...-quality-worker-...`, and a name from before
    # 2026-11-06, which starts with an organization token the rebuilt name no longer has.
    assert rebuilt == text or text.endswith(TOPIC_SEPARATOR + rebuilt) or "-quality-worker-" in text


@PROPERTY
@given(st.text(alphabet=string.ascii_lowercase + string.digits + "_-.", max_size=100))
def test_parsing_a_name_built_from_the_grammars_own_alphabet_is_still_total(text) -> None:
    parts = TopicParts.try_parse(text)
    if parts is not None:
        assert parts.catalog_qualified_name().count(CATALOG_SEPARATOR) == 5
        assert "." not in text                              # the retired dotted form is never accepted


@PROPERTY
@given(st.lists(st.one_of(st.none(), st.integers(), st.text(max_size=5), st.binary(max_size=5)), max_size=3))
def test_try_parse_tolerates_non_string_input_without_raising(values) -> None:
    for value in values:
        try:
            TopicParts.try_parse(value)  # type: ignore[arg-type]
        except TypeError:
            pytest.fail(f"try_parse raised TypeError for {value!r}")


# ── no organization in the name ────────────────────────────────────────────────────────────────────────────────────

@PROPERTY
@given(topic_parts())
def test_a_sink_may_read_any_well_formed_topic_because_the_name_says_nothing_about_an_organization(parts) -> None:
    topic = build_topic_name(parts)
    assert validate_sink_topics(topic) == [topic]


@PROPERTY
@given(topic_parts(), segment)
def test_a_name_from_before_the_change_parses_to_the_same_fields_with_its_organization_dropped(parts, org) -> None:
    assume(org not in ALLOWED_ZONES)
    old = TOPIC_SEPARATOR.join((org, build_topic_name(parts)))
    assert TopicParts.parse(old) == parts


@PROPERTY
@given(topic_parts(), st.integers(min_value=0, max_value=5), st.data())
def test_a_malformed_topic_is_an_invalid_name_for_a_sink(parts, index, data) -> None:
    fields = [getattr(parts, name) for name in TopicParts.FIELDS]
    assume(len(fields[index]) >= 2)
    cut = data.draw(st.integers(min_value=1, max_value=len(fields[index]) - 1))
    fields[index : index + 1] = [fields[index][:cut], fields[index][cut:]]
    assume(not (index == 4 and fields[4:6] == ["quality", "worker"]))
    with pytest.raises(SinkTopicError) as exc_info:
        validate_sink_topics(TOPIC_SEPARATOR.join(fields))
    assert exc_info.value.code == "INVALID_TOPIC_NAME"
