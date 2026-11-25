"""operation claim/lease durability

Revision ID: 0018_operation_claim_lease
Revises: 0017_quality_stream_sample_failures
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_operation_claim_lease"
down_revision = "0017_quality_stream_sample_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_operations",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dc_operations",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_dc_operations_lease_expires_at", "dc_operations", ["lease_expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_dc_operations_lease_expires_at", table_name="dc_operations")
    op.drop_column("dc_operations", "attempt_count")
    op.drop_column("dc_operations", "lease_expires_at")
