"""Connection Profile Pydantic schemas with improved validation."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.services.connection_config_schema import NEW_SOURCE_TYPES

SOURCE_TYPES = {
    "postgresql", "mysql", "sqlserver", "oracle", "jdbc",
    "mongodb", "elasticsearch",
    "api", "rest", "webhook", "http",
    "s3", "gcs", "azure_blob", "adls_gen2", "iceberg", "sftp", "file", "ftp",
} | set(NEW_SOURCE_TYPES)   # one type per backing system of the template catalog (app/services/connection_config_schema.py)


# "postgres" is the older spelling still sent by existing clients/scripts and
# stored on older rows; every consumer downstream (discovery, adapters, default
# ports, introspection) already treats the two identically. Accept it on input
# but store the one canonical value, so a source type never exists in two
# spellings — and editing an old "postgres" connection normalises it instead of
# being rejected with a 422.
SOURCE_TYPE_ALIASES = {"postgres": "postgresql"}


def canonical_source_type(value: str) -> str:
    normalized = value.lower()
    return SOURCE_TYPE_ALIASES.get(normalized, normalized)


ConfigFieldType = Literal["string", "number", "boolean", "secret", "enum", "list", "json"]


class ConfigFieldSchema(BaseModel):
    """One field's metadata for a source type's connection_config — the
    single definition driving UI rendering, backend validation, redaction,
    and secret resolution (see app/services/connection_config_schema.py).

    `persist=False` marks a field that must never be exposed in its raw
    form once saved (an API response, a log line, a support bundle) — every
    `type="secret"` field is `persist=False` by construction, but the flag
    is independent of type so a future non-secret-but-still-sensitive field
    (e.g. a raw connection string that embeds credentials) can opt into the
    same treatment without being mistyped as a "secret" field on its own.
    """

    key: str
    label: str
    type: ConfigFieldType
    required: bool = False
    persist: bool = True
    default: Any = None
    placeholder: str | None = None
    options: list[str] | None = None  # only meaningful for type == "enum"
    description: str | None = None


class ConnectionConfigSchema(BaseModel):
    source_type: str
    fields: list[ConfigFieldSchema]


class ConnectionProfileBase(BaseModel):
    """Every client-editable field — GAP-02: `status` is deliberately NOT
    here. It used to be, which meant a routine PUT (via ConnectionProfileCreate,
    reused as the update body) silently reset it to "draft" on every edit,
    since FastAPI/Pydantic fills in a field's default even when the caller's
    JSON never mentioned it. `status` (plus verification_status/
    last_tested_at/last_test_message) is operational state driven only by
    ConnectionProfileService.probe_and_record — see ConnectionProfile below,
    where it's read-only, response-only."""

    name: str = Field(..., min_length=1, max_length=255)
    source_type: str
    connection_config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        default=None,
        description="Vault path or env reference for credentials. e.g. vault://dataconnect/org/name",
    )
    network_profile: str | None = None

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v: str) -> str:
        normalized = canonical_source_type(v)
        if normalized not in SOURCE_TYPES:
            raise ValueError(
                f"source_type '{v}' is not supported. Choose from: {sorted(SOURCE_TYPES)}"
            )
        return normalized


class ConnectionProfileCreate(ConnectionProfileBase):
    id: str | None = None


class ConnectionProfile(ConnectionProfileBase):
    id: str
    org_id: str
    # When it was created (response-only, never client-set).
    created_at: datetime | None = None
    # Operational state, not user configuration (GAP-02) — all response-only, derived from the connection's
    # separate ConnectionVerification record (see ConnectionProfileService.probe_and_record, its only writer)
    # and never accepted from a client request body. `status` is the coarse usability signal ("active" /
    # "error" / "draft" for a connection that is untested or whose settings changed since its last test).
    status: str = "draft"
    verification_status: str = Field(default="unverified", description="unverified | ok | error")
    last_tested_at: datetime | None = None
    last_test_message: str | None = None
    latency_ms: float | None = None
    error_code: str | None = None
    #: true when the last result was recorded against settings that have since changed.
    verification_stale: bool = False

    model_config = {"from_attributes": True}


class ConnectionTestResult(BaseModel):
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiscoveryResult(BaseModel):
    objects: list[dict[str, Any]]
