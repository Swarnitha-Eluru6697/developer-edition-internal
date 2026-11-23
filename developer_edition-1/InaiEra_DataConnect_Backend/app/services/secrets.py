"""
Secret handling for connection profiles and generated Kafka Connect configs.

The data model's intent (see app/models/connection.py) is that a connection
profile's `connection_config` holds only non-secret settings and `secret_ref`
points at where the real credential lives — GAP-01 was that nothing enforced
that split: a submitted password was persisted straight into
connection_config, so it sat in Postgres in plaintext, was returned by the
connections API (redacted only for *display*, not actually removed), and
could show up in an audit before/after snapshot.

split_secret_fields is the write-side enforcement of that split — called
once, at connection create/update time (see app/services/connection_service.py):
it pulls every credential-bearing field out of a submitted connection_config
so the caller can hand them to app/services/secret_store.py instead of ever
persisting them. mask_connection_config/mask_connection_profile are the
read-side counterpart, for display; redact_connector_config is the same
idea for a generated Kafka Connect connector config. resolve_secret is the
one place a connector config field's real credential value is read, from an
ALREADY-hydrated connection (see secret_store.hydrate_connection_secrets) —
this module is the one place that knows which keys are credential-bearing,
so every caller shares that judgment instead of duplicating it.
"""

import re
from typing import Any

from app.core.config import settings
from app.core.redaction import redact_secrets
from app.services.connection_config_schema import sensitive_keys_for_source_type

MASK = "********"

# Keys inside a ConnectionProfile.connection_config dict that hold a raw
# credential value directly (as opposed to host/port/db-name/etc., which are
# fine to persist and display). This is the fallback net: the primary,
# accurate signal is the per-source_type schema in connection_config_schema.py
# (field type "secret" or persist=False) — this global set only still matters
# for a source_type with no registered schema, or a field the schema doesn't
# yet know about.
SENSITIVE_CONNECTION_KEYS = {
    "password",
    "secret_access_key",
    "access_key_id",
    "client_secret",
    "api_key",
    "token",
    "private_key",
    "secret",
}

# Keys in a *generated Kafka Connect connector config* (connect_config.py's
# output) that hold a raw credential value directly.
SENSITIVE_CONNECTOR_CONFIG_KEYS = {
    "connection.password",
    "database.password",
    "aws.access.key.id",
    "aws.secret.access.key",
}

# Some connector config values are a whole URI with credentials embedded in
# it (e.g. mongodb://user:pass@host/db) rather than a dedicated password key —
# scrub just the credential portion of those, wherever they show up, so the
# rest of the URI (host, db, path) stays visible for troubleshooting.
_CREDENTIAL_URI_RE = re.compile(r"(://)([^/:@\s]+):([^/:@\s]*)(@)")


def _scrub_uri_credentials(value: str) -> str:
    return _CREDENTIAL_URI_RE.sub(rf"\1{MASK}:{MASK}\4", value)


# ── Pattern-based detection (defense in depth) ────────────────────────────────────────────────────────────────────────
# The exact-name sets above only know the credential keys someone thought of. A connection config is free-form, so a user can submit `passwd`,
# `auth_token` or `db_secret`; and the connector-config generator emits keys such as `gcs.credentials.json` and
# `azure.datalake.gen2.client.key` that are not in SENSITIVE_CONNECTOR_CONFIG_KEYS. "Anything secret-SHAPED is a secret" closes that, with
# suffixes that mark a value as a pointer or a setting (a URL, an id, a class name, a flag) rather than a credential kept out of the way.
_SECRET_NAME_RE = re.compile(
    r"(pass(word|wd|phrase)?|pwd|secret|token|credential|authorization|private[._-]?key|api[._-]?key|access[._-]?key|account[._-]?key|"
    r"client[._-]?key|jaas|auth\.user\.info)", re.IGNORECASE)
_NOT_A_CREDENTIAL_SUFFIX_RE = re.compile(
    r"[._-](url|uri|endpoint|ref|id|class|type|provider|file|path|location|enabled|mode|name|header|count|seconds|ms|algorithm|policy)$", re.IGNORECASE)


def looks_like_secret_key(key: Any) -> bool:
    """True for a key that is shaped like it holds a credential, unless its name says it is a pointer/setting (`token_url`, `credentials.provider.class`)."""
    name = str(key)
    return bool(_SECRET_NAME_RE.search(name)) and not _NOT_A_CREDENTIAL_SUFFIX_RE.search(name)


def _is_secret_key(key: str, sensitive: set[str], declared: set[str]) -> bool:
    """Known-sensitive by name, or secret-shaped AND not a field the source type's own schema declares (a declared field is trusted:
    `token_url` on an OAuth source is a URL the schema says is not a secret)."""
    return key in sensitive or (key not in declared and looks_like_secret_key(key))


def _declared_keys(source_type: str | None) -> set[str]:
    from app.services.connection_config_schema import schema_for_source_type

    return {str(field["key"]) for field in schema_for_source_type(source_type)}


def sensitive_keys(source_type: str | None = None) -> set[str]:
    """The credential-bearing keys for this source_type: the schema-driven
    set (accurate, per source_type — see connection_config_schema.py) plus
    the global fallback (still applied on top regardless, for an
    unregistered source_type or a field the schema hasn't caught up to)."""
    return SENSITIVE_CONNECTION_KEYS | sensitive_keys_for_source_type(source_type)


def split_secret_fields(
    connection_config: dict | None, source_type: str | None = None
) -> tuple[dict, dict]:
    """Splits a submitted connection_config into (non_secret, secret) —
    connection_service.py persists only `non_secret` in
    ConnectionProfile.connection_config; `secret` goes to
    app.services.secret_store instead, never to Postgres in the connection
    profile's own JSON.

    A secret-key value of MASK (the client echoing back what it was shown,
    unchanged) or None/empty is dropped from `secret` entirely rather than
    stored — connection_service.py's update path takes that as "leave
    whatever's already stored for this key alone", the same contract the
    old unmask_connection_config used to implement by restoring from
    connection_config (which no longer has anything to restore from, now
    that secrets don't live there)."""
    sensitive = sensitive_keys(source_type)
    declared = _declared_keys(source_type)
    non_secret: dict = {}
    secret: dict = {}
    for key, value in (connection_config or {}).items():
        if not _is_secret_key(key, sensitive, declared):
            non_secret[key] = value
        elif value not in (None, "", MASK):
            secret[key] = value
    return non_secret, secret


def mask_connection_config(connection_config: dict | None, source_type: str | None = None) -> dict:
    """Redact a ConnectionProfile.connection_config dict for display. Since
    split_secret_fields already keeps secret fields out of connection_config
    entirely for a connection created/updated through this app, this mostly
    only matters for a value connection_service.py synthesizes a MASK
    placeholder for (see its _connection_to_dict) or an embedded-URI
    credential in a field the schema doesn't know is sensitive — still
    handled here as defense in depth."""
    sensitive = sensitive_keys(source_type)
    declared = _declared_keys(source_type)
    redacted: dict = {}
    for key, value in (connection_config or {}).items():
        if _is_secret_key(key, sensitive, declared):
            redacted[key] = MASK
        elif isinstance(value, str) and _CREDENTIAL_URI_RE.search(value):
            redacted[key] = _scrub_uri_credentials(value)
        else:
            redacted[key] = value
    return redacted


_PROBE_MESSAGE_MAX_LENGTH = 500


def sanitize_probe_message(message: str | None) -> str:
    """Sanitizes a live-probe diagnostic (ConnectionCheck.message, or a raised
    exception's str()) before it gets persisted as
    ConnectionProfile.last_test_message — see
    ConnectionProfileService.probe_and_record (BUG-08). connection_adapters.py's
    probe functions don't interpolate the raw config into their messages, but
    an unanticipated exception's own str() is outside this app's control, so
    this scrubs any embedded-URI credential the same way mask_connection_config
    does and caps the length, rather than trusting every possible exception
    type never to misbehave."""
    if not message:
        return ""
    scrubbed = redact_secrets(message)  # URI credentials, key=value secrets, bearer tokens (BUG-11)
    if len(scrubbed) > _PROBE_MESSAGE_MAX_LENGTH:
        scrubbed = scrubbed[:_PROBE_MESSAGE_MAX_LENGTH] + "…"
    return scrubbed


def redact_connector_config(config: dict | None) -> dict:
    """Redact a generated Kafka Connect connector config (connect_config.py's
    output) for persistence/display. Never call this on the config actually
    sent to Kafka Connect — only on the copy that gets stored or returned."""
    redacted: dict = {}
    for key, value in (config or {}).items():
        if key in SENSITIVE_CONNECTOR_CONFIG_KEYS or looks_like_secret_key(key):
            redacted[key] = MASK
        elif isinstance(value, str) and _CREDENTIAL_URI_RE.search(value):
            redacted[key] = _scrub_uri_credentials(value)
        else:
            redacted[key] = value
    return redacted


def mask_connection_profile(connection: dict) -> dict:
    """Redact a full ConnectionProfile dict (as returned by the connections API):
    connection_config's credential fields are masked; secret_ref is just a
    pointer (Vault path/env name), never the secret itself, so it's fine as-is."""
    return {
        **connection,
        "connection_config": mask_connection_config(
            connection.get("connection_config"), connection.get("source_type")
        ),
    }


def resolve_secret(connection: dict, config_key: str, provider_field: str) -> str | None:
    """The one place a connector config field's real credential value is read.

    When settings.prefer_config_provider_refs is on and the connection has a
    Vault-backed secret_ref, this returns a Kafka Connect ConfigProvider
    placeholder (e.g. "${vault:dataconnect/org/name:password}") instead of the
    raw value — Kafka Connect resolves it itself, so the plaintext secret
    never needs to be transmitted by DataConnect or appear in its logs at all.
    Requires the target Connect cluster to have a matching ConfigProvider
    configured (config.providers=vault, ...); off by default since most
    deployments won't have that wired up yet.

    Otherwise falls back to the raw value in connection_config — which
    requires the caller to have already hydrated it with the connection's
    real secret bundle first (see secret_store.hydrate_connection_secrets;
    connect_config.build_connector does this once, at its own entry point,
    so nothing downstream of it needs to). That value must still only be
    used transiently to build the config sent to Kafka Connect — never
    persisted or returned as-is.
    """
    ref = connection.get("secret_ref")
    if settings.prefer_config_provider_refs and ref and ref.startswith("vault://"):
        vault_path = ref[len("vault://"):]
        return f"${{vault:{vault_path}:{provider_field}}}"
    return (connection.get("connection_config") or {}).get(config_key)


# ── Secrets must not be smuggled into a pipeline's free-form connector_config ─────────────────────
# A pipeline's connector_config is a plain dict the API stores in Postgres as-is, returns in every
# Pipeline response and snapshots into the audit log. Credentials belong on the CONNECTION (split out
# into the encrypted secret store); this is the screen that stops one being typed into the pipeline.

_SECRET_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|access[._-]?key|private[._-]?key|credential|api[._-]?key)", re.IGNORECASE
)
_REFERENCE_PREFIXES = ("secret://", "vault://", "${")  # pointers to a secret, not the secret itself


def _is_reference_or_mask(value: str) -> bool:
    return value in {"", MASK} or value.startswith(_REFERENCE_PREFIXES)


def find_secret_shaped_entries(config: Any, _path: str = "") -> list[str]:
    """Dotted paths of every entry in `config` that looks like an inline credential: a secret-shaped KEY
    holding a real (non-empty, non-mask, non-reference) value, or ANY string value that embeds
    `scheme://user:password@host`. Returns paths only — never values — so the result is safe to put in an
    error message or a log."""
    found: list[str] = []
    if isinstance(config, dict):
        for key, value in config.items():
            path = f"{_path}.{key}" if _path else str(key)
            if _SECRET_KEY_RE.search(str(key)) and isinstance(value, (str, int, float)) and not (
                isinstance(value, str) and _is_reference_or_mask(value)
            ):
                found.append(path)
            else:
                found.extend(find_secret_shaped_entries(value, path))
    elif isinstance(config, (list, tuple)):
        for index, item in enumerate(config):
            found.extend(find_secret_shaped_entries(item, f"{_path}[{index}]"))
    elif isinstance(config, str):
        match = _CREDENTIAL_URI_RE.search(config)
        if match and match.group(3) not in {"", MASK}:
            found.append(_path or "<value>")
    return found
