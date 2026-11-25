"""connection verification split out of the connection profile

GAP-02: a connection's probe OUTCOME (status, last tested, latency, error code, message) was stored as columns on
dc_connection_profiles and written through the same generic update path as a user's edits. It now lives in its own
table with a single writer, and the profile gains config_revision so an edit to how the connection connects
invalidates an old result. Existing results are carried over (as of revision 1, which every existing profile
starts at); the old columns are dropped.

Revision ID: 0036_connection_verification
Revises: 0035_encrypt_connection_secrets
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036_connection_verifications_table"
down_revision = "0035_encrypt_connection_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_connection_verifications",
        sa.Column(
            "connection_id", postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_connection_profiles.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("verified_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dc_connection_verifications_org_id", "dc_connection_verifications", ["org_id"])
    op.add_column(
        "dc_connection_profiles", sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    # Carry over every result a connection already has. A profile that was never tested has no
    # last_tested_at and gets no row (= "unverified").
    op.execute(
        """
        INSERT INTO dc_connection_verifications
            (connection_id, org_id, status, tested_at, message, verified_revision, error_code)
        SELECT id, org_id,
               CASE WHEN verification_status = 'ok' THEN 'ok' ELSE 'error' END,
               last_tested_at, last_test_message, 1,
               CASE WHEN verification_status = 'ok' THEN NULL ELSE 'PROBE_FAILED' END
        FROM dc_connection_profiles
        WHERE last_tested_at IS NOT NULL
        """
    )
    for column in ("status", "verification_status", "last_tested_at", "last_test_message"):
        op.drop_column("dc_connection_profiles", column)


def downgrade() -> None:
    op.add_column("dc_connection_profiles", sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"))
    op.add_column(
        "dc_connection_profiles",
        sa.Column("verification_status", sa.String(length=50), nullable=False, server_default="unverified"),
    )
    op.add_column("dc_connection_profiles", sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dc_connection_profiles", sa.Column("last_test_message", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE dc_connection_profiles p
        SET verification_status = v.status,
            status = CASE WHEN v.status = 'ok' THEN 'active' ELSE 'error' END,
            last_tested_at = v.tested_at, last_test_message = v.message
        FROM dc_connection_verifications v WHERE v.connection_id = p.id
        """
    )
    op.drop_column("dc_connection_profiles", "config_revision")
    op.drop_index("ix_dc_connection_verifications_org_id", table_name="dc_connection_verifications")
    op.drop_table("dc_connection_verifications")
