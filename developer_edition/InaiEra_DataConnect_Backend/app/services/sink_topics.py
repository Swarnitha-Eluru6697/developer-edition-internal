"""
A sink pipeline's input topics, and the settings that belong to only one direction (GAP-12).

A sink reads `connector_config["topics"]`, which is free text the caller types. Every topic a sink reads must be a plain, literal
topic name this platform builds (no wildcard, no regex, no internal `_`-prefixed topic), decided by the canonical parser
(app/services/topic_naming.py), never by prefix or token counts. A topic that does not parse is INVALID_TOPIC_NAME.

A topic name carries no organization (since 2026-11-06), so the name cannot say whose topic it is: an organization owns its own Kafka, and
that is what keeps one organization's topics from another's. The same check runs when the pipeline is created or edited, when its config
is built for deploy, and on every reconciler tick, because all of them build the config through connect_config._sink_topic.
"""

import re
from collections.abc import Iterable

from app.services.topic_naming import TopicParseError, TopicParts

INVALID_TOPIC_NAME = "INVALID_TOPIC_NAME"

# Kafka's legal topic characters; also rules out wildcards, regexes, whitespace and path tricks.
_TOPIC_RE = re.compile(r"^[A-Za-z0-9._-]{1,249}$")

# Settings that only make sense on one direction. A SOURCE with `topics` (or a SINK with `topic.prefix`) is a config
# for the other kind of connector — silently ignored or, worse, half-applied.
SINK_ONLY_CONFIG_KEYS = frozenset({"topics", "topics.regex"})
SOURCE_ONLY_CONFIG_KEYS = frozenset({
    "tables", "topic.prefix", "table.include.list", "table.whitelist", "snapshot.mode",
    "incrementing.column.name", "timestamp.column.name",
})


class SinkTopicError(ValueError):
    """The sink's input topic setting is not acceptable; the message says why and is safe to show the user. `code` is
    INVALID_TOPIC_NAME (malformed, or not a topic name at all)."""

    def __init__(self, message: str, *, code: str = INVALID_TOPIC_NAME, topic: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.topic = topic


def _split(raw: object) -> list[str]:
    if isinstance(raw, str):
        parts: Iterable[object] = raw.split(",")
    elif isinstance(raw, list | tuple):
        parts = raw
    else:
        raise SinkTopicError("The sink's input topic must be a topic name, or a list of topic names.", code=INVALID_TOPIC_NAME)
    topics = []
    for part in parts:
        if not isinstance(part, str):
            raise SinkTopicError("The sink's input topic must be a topic name, or a list of topic names.", code=INVALID_TOPIC_NAME)
        if part.strip():
            topics.append(part.strip())
    return topics


def validate_sink_topics(raw: object) -> list[str]:
    """The sink's input topics as a clean list, or SinkTopicError. Every topic must be literal and a name this platform builds."""
    topics = _split(raw)
    if not topics:
        raise SinkTopicError("Sink pipelines require an input topic to read from — set connector_config.topics.", code=INVALID_TOPIC_NAME)
    seen: list[str] = []
    for topic in topics:
        if not _TOPIC_RE.match(topic):
            raise SinkTopicError(
                f"'{topic}' is not a valid input topic name: use a literal topic name (letters, digits, '.', '_' and "
                "'-' only — no wildcards or patterns).",
                code=INVALID_TOPIC_NAME, topic=topic,
            )
        try:
            TopicParts.parse(topic)
        except TopicParseError as exc:
            raise SinkTopicError(f"'{topic}' is not a valid input topic name: {exc}", code=INVALID_TOPIC_NAME, topic=topic) from exc
        if topic not in seen:
            seen.append(topic)
    return seen


def normalize_sink_topics(raw: object) -> str:
    """The validated topics in the one form the connector is configured with (comma-separated)."""
    return ",".join(validate_sink_topics(raw))


def misplaced_config_keys(direction: str, connector_config: dict | None) -> list[str]:
    """The keys in `connector_config` that belong to the other direction, sorted."""
    foreign = SINK_ONLY_CONFIG_KEYS if direction == "SOURCE" else SOURCE_ONLY_CONFIG_KEYS
    return sorted(key for key in (connector_config or {}) if key in foreign)
