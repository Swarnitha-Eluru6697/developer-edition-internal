"""Connection Profile model — org-scoped source system credentials."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class ConnectionProfile(Base, UuidPk, TimestampMixin):
    """
    Stores connectivity details for a source system (database, API, file store).

    Sensitive credentials are NOT stored here — enforced, not just documented:
    app/services/secrets.py's split_secret_fields() strips every
    credential-bearing field out of connection_config before
    app/services/connection_service.py ever persists it, and hands them to
    app/services/secret_store.py instead (see ConnectionSecret below), which
    is what `secret_ref` actually points at for a connection created through
    this app. The real value is read back only transiently, right before a
    live operation needs it (a Kafka Connect deploy, a live connectivity
    probe, source introspection) — see secret_store.hydrate_connection_secrets.

    Supported source_type values:
        RDBMS   : postgres, mysql, sqlserver, oracle, db2
        NoSQL   : mongodb, elasticsearch
        File    : s3, gcs, azure_blob, sftp, ftp
        HTTP    : api, rest, webhook
    """

    __tablename__ = "dc_connection_profiles"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(100))
    connection_config: Mapped[dict] = mapped_column(
        JSONB,
        comment="Non-secret config only: hostname, port, database, poll interval, etc.",
    )
    secret_ref: Mapped[str | None] = mapped_column(
        String(1000),
        comment=(
            "Pointer to where the real credential lives — this app's own "
            "secret://dataconnect/connections/<id> (see ConnectionSecret / "
            "app/services/secret_store.py), or a manually-provisioned "
            "vault://... path for a Vault ConfigProvider-resolved connection."
        ),
    )
    network_profile: Mapped[str | None] = mapped_column(
        String(255),
        comment="Named network route (VPN, private link, etc.) if required",
    )
    created_by: Mapped[str | None] = mapped_column(String(255))

    # GAP-02: bumped whenever something that affects HOW THIS CONNECTION CONNECTS changes (source_type,
    # connection_config, network_profile, or a new secret). A ConnectionVerification records the revision it
    # tested; if the profile has moved on since, that result describes a configuration that no longer exists
    # and the connection reads as unverified again until it is re-tested. A rename does not bump it.
    config_revision: Mapped[int] = mapped_column(default=1, server_default="1")


class ConnectionVerification(Base, TimestampMixin):
    """
    GAP-02: the OUTCOME of the most recent live probe of one connection — operational state, kept apart from
    ConnectionProfile (user configuration). The two used to share one row, and verification was written through
    the same generic update path as a user's edits; now this table has exactly one writer
    (ConnectionVerificationRepository.record, called only by ConnectionProfileService.probe_and_record) and a
    client request body can never reach it.

    A connection with no row here has never been tested ("unverified"). `verified_revision` is the
    ConnectionProfile.config_revision that was probed — see that column for how a later edit invalidates this.
    CASCADE on delete: the result has no meaning without its connection.
    """

    __tablename__ = "dc_connection_verifications"

    connection_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("dc_connection_profiles.id", ondelete="CASCADE"), primary_key=True,
    )
    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    status: Mapped[str] = mapped_column(String(20), comment="ok | error")
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(100), comment="Stable machine-readable reason; NULL when ok")
    message: Mapped[str | None] = mapped_column(
        Text, comment="Sanitized diagnostic from the probe (see secrets.sanitize_probe_message)",
    )
    verified_revision: Mapped[int] = mapped_column(default=1)


class ConnectionSecret(Base, TimestampMixin):
    """
    The actual credential values for one ConnectionProfile — split out of
    connection_config at write time (app/services/secrets.py's
    split_secret_fields) so a password/token/key never sits in the JSON blob
    the connections API returns, audits, or that a generated connector
    config gets built from. See app/services/secret_store.py, this table's
    only reader/writer: no route reads it directly, and it's fetched only
    transiently, at the point a live operation genuinely needs the real
    value (see hydrate_connection_secrets).

    connection_id IS the primary key, not a separate id: a connection has at
    most one secret bundle, so this is a plain one-to-one, not a
    one-to-many needing its own identity. CASCADE on delete — this row has
    no meaning once its connection profile is gone.

    The only secret backend is this table (app/services/secret_store.py's PostgresSecretStore): the AES-256-GCM encrypted bundle,
    written in the SAME transaction as the connection profile and its secret_ref (BUG-03). A Vault-backed store does not exist yet.
    """

    __tablename__ = "dc_connection_secrets"

    connection_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("dc_connection_profiles.id", ondelete="CASCADE"), primary_key=True,
    )
    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
