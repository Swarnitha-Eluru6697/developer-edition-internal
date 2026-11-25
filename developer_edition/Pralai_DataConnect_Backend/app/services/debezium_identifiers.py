"""
Deterministic, collision-resistant runtime identifiers for Debezium CDC
connectors.

Debezium requires several source-side identifiers to be unique within the
scope where they actually operate:

  - `topic.prefix` (the connector's "logical server name") must be unique
    across every Debezium connector in the Kafka Connect cluster — it
    namespaces every topic the connector produces, including the internal
    DDL/schema-change topic.
  - A PostgreSQL replication slot name and publication name must be unique
    per PostgreSQL server (every pipeline reading the same database needs
    its own slot/publication) and must satisfy PostgreSQL's identifier
    rules: lowercase letters, digits, underscore only; <= 63 bytes.
  - MySQL's `database.server.id` must be a unique 32-bit integer among
    every replica (Debezium connector included) reading a given server's
    binlog.

What this replaced: `topic.prefix` was built from a coarse
`<org>.<domain>.<source_system>` prefix shared by every pipeline under that
org/domain/source — two pipelines differing only in entity/mode/version got
an IDENTICAL prefix. PostgreSQL's slot/publication names were derived from
the entity name alone, colliding across any two pipelines (different org,
different domain, same source database) that happened to target
same-named tables. MySQL's `database.server.id` was a single hardcoded
literal ("184054") for every pipeline — guaranteed to collide the moment a
second MySQL CDC pipeline existed against the same server.

Every function here is a pure, deterministic function of the pipeline's own
`connector_name` — already globally unique (it encodes the pipeline's full
org/zone/domain/source_system/entity/mode/version identity, see
app/services/topic_naming.py) and immutable once the pipeline is created
(PipelineUpdate cannot touch the identity fields it's built from — see
app/schemas/pipeline.py). Because the input is already persisted and
stable, these outputs are effectively persisted and stable too: recomputing
them for the same pipeline always yields the same value, so a redeploy
never orphans a PostgreSQL replication slot or reassigns a MySQL server id
out from under a connector that's already using it.
"""

import hashlib
import re

# PostgreSQL's NAMEDATALEN is 64, leaving 63 usable bytes for an identifier.
_POSTGRES_IDENTIFIER_MAX_LEN = 63
# Hex chars of a content hash appended when an identifier must be truncated
# to fit — 32 bits of collision resistance is far more than this needs, but
# costs only 8 characters of the limited budget.
_HASH_SUFFIX_LEN = 8


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:_HASH_SUFFIX_LEN]


def _sanitize_postgres_identifier(raw: str) -> str:
    """Lowercase letters, digits, and underscore only, starting with a
    letter or underscore — PostgreSQL's unquoted-identifier rules."""
    sanitized = re.sub(r"[^a-z0-9_]", "_", raw.lower())
    if not sanitized or not re.match(r"^[a-z_]", sanitized):
        sanitized = f"_{sanitized}"
    return sanitized


def _fit_postgres_identifier(raw: str) -> str:
    """Sanitize, then — only if it doesn't fit PostgreSQL's length limit —
    truncate while appending a hash of the FULL, untruncated value. Two
    identifiers that differ only after the truncation point still can't
    collide, since the hash captures the part that got cut off."""
    sanitized = _sanitize_postgres_identifier(raw)
    if len(sanitized) <= _POSTGRES_IDENTIFIER_MAX_LEN:
        return sanitized
    suffix = f"_{_stable_hash(raw)}"
    return sanitized[: _POSTGRES_IDENTIFIER_MAX_LEN - len(suffix)] + suffix


def debezium_topic_prefix(connector_name: str) -> str:
    """The connector's logical server name / topic.prefix. Restricted to
    the characters Kafka topic names allow, since this becomes a topic-name
    prefix; unique because connector_name already is."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", connector_name)


def debezium_slot_name(connector_name: str) -> str:
    """PostgreSQL replication slot name — unique per pipeline, valid per
    PostgreSQL identifier rules."""
    return _fit_postgres_identifier(f"dbz_slot_{connector_name}")


def debezium_publication_name(connector_name: str) -> str:
    """PostgreSQL publication name — unique per pipeline, valid per
    PostgreSQL identifier rules."""
    return _fit_postgres_identifier(f"dbz_pub_{connector_name}")


def debezium_server_id(connector_name: str) -> int:
    """MySQL database.server.id — a unique unsigned 32-bit integer.
    Deterministic hash of connector_name, offset into the upper half of the
    valid range (2**31 .. 2**32 - 1) to steer clear of the small integers
    (1, 2, 3, ...) real replicas are conventionally assigned by hand."""
    digest = int(hashlib.sha256(connector_name.encode()).hexdigest()[:8], 16)
    return (digest % (2**31 - 1)) + 2**31
