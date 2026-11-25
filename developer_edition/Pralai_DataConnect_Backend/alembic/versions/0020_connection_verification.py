"""connection verification diagnostics

BUG-08: a live connectivity probe that raised (a timeout, DNS failure, ...)
used to disappear into a bare except/pass, leaving no diagnostic at all.
These columns are what ConnectionProfileService.probe_and_record persists
now, whether the probe returns cleanly or raises.

Revision ID: 0020_connection_verification
Revises: 0019_connection_secrets
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_connection_verification"
down_revision = "0019_connection_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_connection_profiles",
        sa.Column("verification_status", sa.String(length=50), nullable=False, server_default="unverified"),
    )
    op.add_column(
        "dc_connection_profiles",
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dc_connection_profiles",
        sa.Column("last_test_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dc_connection_profiles", "last_test_message")
    op.drop_column("dc_connection_profiles", "last_tested_at")
    op.drop_column("dc_connection_profiles", "verification_status")
