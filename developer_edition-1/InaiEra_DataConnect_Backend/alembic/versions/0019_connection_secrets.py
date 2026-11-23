"""connection secrets — extract plaintext credentials out of connection_config

GAP-01: connection_config used to (and, for any row created before this
migration, still does) hold raw credential values directly, so a password/
token/key could sit in plaintext in dc_connection_profiles. This migration
adds dc_connection_secrets (see app.models.connection.ConnectionSecret /
app.services.secret_store.py) and backfills it: for every existing
connection profile, whatever fields its source_type's schema marks as
secret (app.services.connection_config_schema.sensitive_keys_for_source_type,
plus the global fallback set in app.services.secrets.SENSITIVE_CONNECTION_KEYS)
are moved out of connection_config and into dc_connection_secrets, and
secret_ref is set to point at them.

This uses today's application code (a one-time backfill, not a repeatable
schema migration) to decide which keys are secret — acceptable here since
it only ever runs once, against data that exists right now.

Revision ID: 0019_connection_secrets
Revises: 0018_operation_claim_lease
Create Date: 2026-09-23
"""

import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019_connection_secrets"
down_revision = "0018_operation_claim_lease"
branch_labels = None
depends_on = None

_SECRET_REF_PREFIX = "secret://dataconnect/connections/"


def upgrade() -> None:
    op.create_table(
        "dc_connection_secrets",
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("values", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["dc_connection_profiles.id"],
            ondelete="CASCADE", name="fk_dc_connection_secrets_connection_id",
        ),
    )
    op.create_index("ix_dc_connection_secrets_org_id", "dc_connection_secrets", ["org_id"])

    # Deferred import: this app code only needs to exist at migration-run
    # time, for this one-time backfill — never imported by this module at
    # collection/autogenerate time.
    from app.services.connection_config_schema import sensitive_keys_for_source_type
    from app.services.secrets import SENSITIVE_CONNECTION_KEYS

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, org_id, source_type, connection_config FROM dc_connection_profiles")
    ).fetchall()
    for row in rows:
        cfg = row.connection_config or {}
        sensitive = SENSITIVE_CONNECTION_KEYS | sensitive_keys_for_source_type(row.source_type)
        secret_fields = {k: v for k, v in cfg.items() if k in sensitive and v not in (None, "", "********")}
        if not secret_fields:
            continue
        remaining = {k: v for k, v in cfg.items() if k not in secret_fields}
        conn.execute(
            sa.text(
                "INSERT INTO dc_connection_secrets (connection_id, org_id, values) "
                "VALUES (:connection_id, :org_id, :values)"
            ),
            {"connection_id": row.id, "org_id": row.org_id, "values": json.dumps(secret_fields)},
        )
        conn.execute(
            sa.text("UPDATE dc_connection_profiles SET connection_config = :cfg, secret_ref = :ref WHERE id = :id"),
            {"cfg": json.dumps(remaining), "ref": f"{_SECRET_REF_PREFIX}{row.id}", "id": row.id},
        )


def downgrade() -> None:
    # Deliberately does NOT restore secret values back into connection_config
    # — that would reintroduce the exact plaintext-in-Postgres issue this
    # migration fixes. Rolling back past this point means re-entering
    # credentials through the API afterward.
    op.drop_table("dc_connection_secrets")
