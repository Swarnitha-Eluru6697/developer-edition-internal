import re
from dataclasses import dataclass, replace
from typing import Any, ClassVar

ALLOWED_ZONES = {"raw", "prepared", "curated", "quarantine", "meta"}
# Every mode is a topic segment, so it follows the same grammar as the other six: a-z, 0-9 and "_" — never "-". The worker-failure
# quarantine mode used to be spelled "quality-worker", which added an eighth "-"-separated token to its topic names.
ALLOWED_MODES = {"cdc", "snapshot", "batch", "api", "stream", "validation", "validation_sink", "connector", "quality_worker", "replay"}
# Spellings that existed before the grammar was tightened, mapped to the one canonical name. Only ever READ (a topic that
# already exists in Kafka is still understood); nothing builds a name with one.
LEGACY_MODE_ALIASES = {"quality-worker": "quality_worker"}
SEGMENT_RE = re.compile(r"^[a-z0-9_-]+$")
VERSION_RE = re.compile(r"^v[0-9]+$")

# A topic's six parts are joined with "-": raw-erp-sap-customers-cdc-v1. The organisation is NOT part of a topic name: an organisation
# owns its own Kafka, so its topics need no prefix to be told apart (before 2026-11-06 the name began with the org id, and the old
# seven-part names are still READ, see TopicParts.parse). Gravitino (the catalog behind UCS)
# cannot load an object name with more than two ".", so the earlier "." join made every topic invisible to it. That join is gone:
# "-" is the only separator, and it is only safe if no segment can contain one (a "-" inside a segment would let
# domain="a-b"+source_system="c" collide with domain="a"+source_system="b-c"). So every segment is limited to a-z, 0-9 and "_".
TOPIC_SEPARATOR = "-"
TOPIC_SEGMENT_RE = re.compile(r"^[a-z0-9_]+$")
# The catalog identifier joins the same seven segments with "__". For that to be reversible a segment may not itself contain
# "__", nor begin or end with "_" (which would run into the joiner as "___").
CATALOG_SEPARATOR = "__"


class TopicParseError(ValueError):
    """A string that is not a seven-part topic name this platform builds."""


def normalize_segment(value: str) -> str:
    """The grammar-conforming spelling of a segment: lowercased, with every "-" (and whitespace) turned into "_". For values the
    platform itself defines ("quality-worker" -> "quality_worker"). User-supplied segments are refused with a clear error
    rather than silently rewritten, so what a user typed is never turned into a different identity."""
    return re.sub(r"[-\s]+", "_", value.strip().lower())


def _without_legacy_org(tokens: list[str]) -> list[str]:
    """A name built before 2026-11-06 starts with the organisation id: `<org>-<zone>-...` (zone is the SECOND token, the first is not
    a zone). Drops that token. A current name starts with its zone, so it is returned unchanged."""
    if len(tokens) >= 2 and tokens[0] not in ALLOWED_ZONES and tokens[1] in ALLOWED_ZONES:
        return tokens[1:]
    return tokens


@dataclass(frozen=True)
class TopicParts:
    """The six logical fields of a topic name, immutable. The ONE place a topic name is built from fields, taken apart into
    fields, or derived from another topic (prepared / quarantine / catalog identifier) — nothing else splits a topic string."""

    zone: str
    domain: str
    source_system: str
    entity: str
    mode: str
    version: str

    FIELDS: ClassVar[tuple[str, ...]] = ("zone", "domain", "source_system", "entity", "mode", "version")

    @classmethod
    def parse(cls, topic: str) -> "TopicParts":
        """The six fields of a built topic name; TopicParseError if it is not one. Strict: every field must follow the
        segment grammar ([a-z0-9_]) and the version must be v<number>. A topic built before 2026-11-06 began with the
        organisation id (`org_1001-raw-erp-...`, seven parts); it is still understood, the organisation part is dropped. A pre-grammar
        worker-quarantine topic spelled `...-quality-worker-v1` is still understood, as mode `quality_worker`."""
        if not isinstance(topic, str) or not topic:
            raise TopicParseError("topic name is required")
        tokens = _without_legacy_org(topic.split(TOPIC_SEPARATOR))
        if len(tokens) == len(cls.FIELDS) + 1 and tokens[4:6] == ["quality", "worker"]:
            tokens = [*tokens[:4], "quality_worker", *tokens[6:]]
        if len(tokens) != len(cls.FIELDS):
            raise TopicParseError(
                f"topic {topic!r} must have exactly {len(cls.FIELDS)} parts joined by {TOPIC_SEPARATOR!r}: "
                + TOPIC_SEPARATOR.join(f"<{f}>" for f in cls.FIELDS)
            )
        parts = cls(*(LEGACY_MODE_ALIASES.get(t, t) if i == 4 else t for i, t in enumerate(tokens)))
        try:
            _validate_fields(parts, check_vocabulary=False)
        except ValueError as exc:
            raise TopicParseError(f"topic {topic!r} is not a valid topic name: {exc}") from exc
        return parts

    @classmethod
    def try_parse(cls, topic: str | None) -> "TopicParts | None":
        """`parse`, or None for anything that is not a topic name this platform builds."""
        try:
            return cls.parse(topic or "")
        except TopicParseError:
            return None

    def topic_name(self, soft_limit: int = 180) -> str:
        return build_topic_name(self, soft_limit=soft_limit)

    def catalog_qualified_name(self) -> str:
        return build_catalog_qualified_name(self)

    @classmethod
    def from_catalog_qualified_name(cls, name: str) -> "TopicParts":
        """Inverse of `catalog_qualified_name`; TopicParseError if `name` is not one."""
        tokens = _without_legacy_org(name.split(CATALOG_SEPARATOR))
        if len(tokens) != len(cls.FIELDS):
            raise TopicParseError(f"catalog name {name!r} must have exactly {len(cls.FIELDS)} parts joined by {CATALOG_SEPARATOR!r}")
        parts = cls(*tokens)
        try:
            _validate_fields(parts, check_vocabulary=False)
        except ValueError as exc:
            raise TopicParseError(f"catalog name {name!r} is not valid: {exc}") from exc
        return parts

    def with_zone(self, zone: str) -> "TopicParts":
        return replace(self, zone=zone)

    def with_mode(self, mode: str) -> "TopicParts":
        return replace(self, mode=mode)

    def prepared(self) -> "TopicParts":
        """The prepared-zone counterpart of a raw topic; any other zone is returned unchanged."""
        return self.with_zone("prepared") if self.zone == "raw" else self

    def quarantine(self, mode: str) -> "TopicParts":
        return self.with_zone("quarantine").with_mode(mode)


def _validate_segment(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} is required")
    if not SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{name} must be lowercase and contain only a-z, 0-9, _, or -")


def _validate_topic_segment(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} is required")
    if not TOPIC_SEGMENT_RE.fullmatch(value):
        raise ValueError(
            f"{name} must be lowercase and contain only a-z, 0-9 or _ ('-' is the separator between the parts of a "
            f"topic name, so it cannot appear inside one; use '_' instead, e.g. {normalize_segment(value)!r})"
        )
    if CATALOG_SEPARATOR in value or value.startswith("_") or value.endswith("_"):
        raise ValueError(
            f"{name} may not contain '__' or begin or end with '_': the catalog identifier joins the parts with '__', so "
            "that would make it impossible to split back into its fields."
        )


def _validate_fields(parts: TopicParts, *, check_vocabulary: bool = True) -> None:
    """Every field follows the topic segment grammar ([a-z0-9_] only, and "__"-safe), the version is v<number> and, when
    `check_vocabulary`, the zone and mode are ones the platform knows."""
    for name in TopicParts.FIELDS:
        _validate_topic_segment(name, getattr(parts, name))
    if not VERSION_RE.fullmatch(parts.version):
        raise ValueError("version must match ^v[0-9]+$")
    if check_vocabulary:
        if parts.zone not in ALLOWED_ZONES:
            raise ValueError(f"zone must be one of {sorted(ALLOWED_ZONES)}")
        if parts.mode not in ALLOWED_MODES:
            raise ValueError(f"mode must be one of {sorted(ALLOWED_MODES)}")


def validate_topic_parts(parts: TopicParts, soft_limit: int = 180) -> None:
    _validate_fields(parts)
    topic = build_topic_name(parts)
    if len(topic) > soft_limit:
        raise ValueError(f"topic exceeds soft limit of {soft_limit} characters")


def build_topic_name(parts: TopicParts, soft_limit: int = 180) -> str:
    # The real, physical Kafka topic name every connector / monitoring / dead-letter path uses:
    # <zone>-<domain>-<source_system>-<entity>-<mode>-<version>, e.g. raw-erp-sap-customers-cdc-v1.
    # "-" rather than "." so the name is loadable as a Gravitino object (see TOPIC_SEPARATOR). Every field is checked against the
    # grammar here, at the single place a name is built, so no caller can assemble a name whose fields would not parse back out.
    _validate_fields(parts)
    topic = TOPIC_SEPARATOR.join(getattr(parts, name) for name in TopicParts.FIELDS)
    if len(topic) > soft_limit:
        raise ValueError(f"topic exceeds soft limit of {soft_limit} characters")
    return topic


def build_catalog_qualified_name(parts: TopicParts) -> str:
    """BUG-01: the Gravitino-safe identifier for the SAME topic identity
    build_topic_name computes — "__"-joined because Gravitino uses "." as
    its own namespace separator and rejects any object name containing one
    (see build_topic_name's comment). Never the real Kafka topic name (never
    passed to Kafka Connect, never used for monitoring/offset lookups, never
    stored as Pipeline.topic_name) — only for code that needs to address this
    topic as a Gravitino catalog object. Nothing in this codebase calls
    Gravitino directly today (UCS resolves its own Gravitino FQN from an
    OpenLineage event's plain (namespace, name) via its own admin-managed
    mapping table — see app/services/ucs_lineage_sync.py's module docstring
    — so it's given the real topic_name, not this), but the identifier is
    exposed here so a future direct integration has a single, correct
    formula to use instead of reinventing one inline."""
    return CATALOG_SEPARATOR.join(getattr(parts, name) for name in TopicParts.FIELDS)


def to_catalog_qualified_name(topic_name: str) -> str | None:
    """build_catalog_qualified_name, but from an already-built physical topic name rather than its TopicParts — for a
    caller that only has the string (e.g. reading Pipeline.topic_name back off a row). It always goes through TopicParts, so
    the result is exactly what `TopicParts.catalog_qualified_name()` gives for the same topic.

    BUG-04: there is NO string-replace fallback. A "-" in a name that does not parse as seven fields cannot be told apart
    from a "-" inside one field (`...-sap-my-table-cdc-v1`: is the entity "my-table", or two fields?), and replacing every one
    would produce an identifier that splits back into the wrong fields, or that two different topics share. So a name this
    platform did not build (a row from before the naming rules, a hand-made one) has no catalog identifier: None."""
    parts = TopicParts.try_parse(topic_name)
    return parts.catalog_qualified_name() if parts is not None else None


def to_prepared_topic(topic_name: str) -> str:
    """The prepared-zone counterpart of a raw topic name, or the name unchanged if it isn't a raw-zone topic (or isn't a topic
    name this platform builds at all). The single formula every caller that needs this derivation should use
    (streaming_quality_processor, lineage_service, runtime_ownership). Only the zone field is replaced, never a "raw" that
    happens to be in a domain or entity name."""
    parts = TopicParts.try_parse(topic_name)
    if parts is None or parts.zone != "raw":
        return topic_name
    return parts.prepared().topic_name(soft_limit=len(topic_name) + len("prepared"))


def _build_quarantine_topic_name(
    *, domain: str, source_system: str, entity: str, version: str, mode: str,
) -> str:
    return build_topic_name(
        TopicParts(
            zone="quarantine",
            domain=domain,
            source_system=source_system,
            entity=entity,
            mode=mode,
            version=version,
        ),
    )


VALIDATION_MODE = "validation"
# A SINK pipeline's rule failures get their own mode, so a quarantine topic says which side of the flow it came from:
# `...-products-validation-v1` is a source's, `...-products_sink-validation_sink-v1` a sink's.
SINK_VALIDATION_MODE = "validation_sink"


def build_validation_quarantine_topic_name(
    *, domain: str, source_system: str, entity: str, version: str, sink: bool = False,
) -> str:
    """GAP-03: a DataConnect *quality* failure (a rule this app itself
    evaluates against record content — a missing customer_id, an invalid
    email, a status outside its allowed values) is a fundamentally
    different event from a Kafka Connect *connector* failure (bad
    serialization, a converter error, the connector itself erroring on a
    malformed source message) — mixing both into one topic made monitoring
    read "quarantine volume" as a single, uninterpretable number that could
    mean either. mode=validation (validation_sink for a sink pipeline), used by every quality-check failure path
    (app/services/quarantine_sink.py's KafkaQuarantineSink for the batch/
    stream-execute path, app/services/streaming_quality_processor.py for
    the continuous per-record path) so there's one formula for this half of
    the split, not one per caller. See build_connector_quarantine_topic_name
    for the other half."""
    return _build_quarantine_topic_name(
        domain=domain, source_system=source_system, entity=entity, version=version,
        mode=SINK_VALIDATION_MODE if sink else VALIDATION_MODE,
    )


def validation_quarantine_topic_for(pipeline: dict[str, Any]) -> str:
    """The validation quarantine topic of one pipeline: mode `validation` for a source, `validation_sink` for a sink (direction "SINK").
    Every caller that has the pipeline row uses this, so the source/sink choice is made in one place."""
    return build_validation_quarantine_topic_name(
        domain=pipeline["domain"], source_system=pipeline["source_system"],
        entity=pipeline["entity"], version=pipeline["version"], sink=(pipeline.get("direction") or "").upper() == "SINK",
    )


def build_connector_quarantine_topic_name(
    *, domain: str, source_system: str, entity: str, version: str
) -> str:
    """GAP-03: the OTHER half of the split — Kafka Connect's own dead-letter
    queue (errors.deadletterqueue.topic.name, set in
    app/services/pipeline_config.py) for a connector-level failure: invalid
    serialization, a converter error, the connector erroring on a malformed
    source message. This never carries a DataConnect quality-rule failure
    (see build_validation_quarantine_topic_name for that), and vice versa —
    two topics, two distinct failure classes, so monitoring can actually
    tell them apart instead of reading one undifferentiated "quarantine
    volume" number that could mean either."""
    return _build_quarantine_topic_name(
        domain=domain, source_system=source_system, entity=entity, version=version,
        mode="connector",
    )


def build_worker_quarantine_topic_name(
    *, domain: str, source_system: str, entity: str, version: str
) -> str:
    """BUG-03: the THIRD failure class. A *worker* failure is DataConnect's own quality worker
    (app/services/quality_stream_worker.py) being unable to finish handling a record — its stats
    database was down, the quarantine produce kept failing, the evaluation raised, the bytes
    weren't readable — whatever the record's own content or Kafka Connect's behaviour. It is neither
    a rule failure (build_validation_quarantine_topic_name: the record was read and broke a rule)
    nor a connector failure (build_connector_quarantine_topic_name: Kafka Connect couldn't ingest
    it). Mixing it into either would make a perfectly valid record look invalid, or inflate the
    "ingestion failures" count — and its remedy is different (restore what broke, then replay
    these records through the worker), so it gets its own topic. mode=quality_worker (underscore: a hyphen would be a second separator inside the name)."""
    return _build_quarantine_topic_name(
        domain=domain, source_system=source_system, entity=entity, version=version,
        mode="quality_worker",
    )


CONNECTOR_NAME_SEPARATOR = "--"
CONNECTOR_NAME_FIELDS = ("domain", "source_system", "entity", "mode", "version")


def _validate_connector_segment(name: str, value: str) -> None:
    """The connector name joins five segments with "--" (runtime design): <domain>--<source_system>--<entity>--<mode>--<version>.
    A segment may legitimately contain a single "-" (SEGMENT_RE allows it), but one containing "--", or beginning or
    ending with "-", would make the joined name impossible to split back into its six parts."""
    if CONNECTOR_NAME_SEPARATOR in value or value.startswith("-") or value.endswith("-"):
        raise ValueError(
            f"{name} may not contain '--' or begin or end with '-': the connector name joins its parts with '--', so "
            "that would make it ambiguous."
        )


def build_connector_name(parts: TopicParts) -> str:
    """<domain>--<source_system>--<entity>--<mode>--<version>, e.g. erp--sap--customers--cdc--v1. (Before 2026-11-06 it began with the org id.)"""
    for name, value in parts.__dict__.items():
        if name != "zone":
            _validate_segment(name, value)
            _validate_connector_segment(name, value)
    return CONNECTOR_NAME_SEPARATOR.join(
        [parts.domain, parts.source_system, parts.entity, parts.mode, parts.version]
    )


# Connector classes whose own validation refuses anything but lowercase letters, digits and hyphens in the
# connector name (LittleHorse: "Connector name only supports lowercase alphanumeric characters and hyphens").
# The parts may contain underscores (customer_orders), so these get the same name with "_" written as "-".
HYPHEN_ONLY_CONNECTOR_CLASSES = frozenset({
    "io.littlehorse.connect.WfRunSinkConnector",
    "io.littlehorse.connect.ExternalEventSinkConnector",
})


def connector_name_for_class(name: str, connector_class: str | None) -> str:
    """The deployed connector name: `name` unchanged, except for classes that accept hyphens only."""
    return name.replace("_", "-") if connector_class in HYPHEN_ONLY_CONNECTOR_CLASSES else name


def parse_connector_name(name: str) -> dict[str, str]:
    """The five parts of a connector name, or ValueError if it does not follow the format. A name built before 2026-11-06 began with the
    organisation id (six parts); it is still understood, the organisation part is dropped."""
    pieces = name.split(CONNECTOR_NAME_SEPARATOR)
    if len(pieces) == len(CONNECTOR_NAME_FIELDS) + 1:
        _validate_segment("orgid", pieces[0])
        pieces = pieces[1:]
    if len(pieces) != len(CONNECTOR_NAME_FIELDS):
        raise ValueError(
            f"connector name {name!r} must have exactly {len(CONNECTOR_NAME_FIELDS)} parts joined by "
            f"'{CONNECTOR_NAME_SEPARATOR}': <domain>--<source_system>--<entity>--<mode>--<version>"
        )
    parsed = dict(zip(CONNECTOR_NAME_FIELDS, pieces, strict=True))
    for field_name, value in parsed.items():
        _validate_segment(field_name, value)
        _validate_connector_segment(field_name, value)
    if parsed["mode"] not in ALLOWED_MODES:
        raise ValueError(f"connector name mode must be one of {sorted(ALLOWED_MODES)}")
    if not VERSION_RE.fullmatch(parsed["version"]):
        raise ValueError("connector name version must match ^v[0-9]+$")
    return parsed


def is_conforming_connector_name(name: str) -> bool:
    try:
        parse_connector_name(name)
    except ValueError:
        return False
    return True

