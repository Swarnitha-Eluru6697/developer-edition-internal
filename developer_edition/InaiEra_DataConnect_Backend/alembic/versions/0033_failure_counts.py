"""failure counts

FE-BUG-17: exact, windowed per-rule (validation) and per-cause (ingestion)
failure counts in one-minute buckets, replacing counts derived from a bounded
sample. See app/models/failure_count.py.

Revision ID: 0033_failure_counts
Revises: 0032_pipeline_active_operation
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_failure_counts"
down_revision = "0032_pipeline_active_operation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_failure_counts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column(
            "pipeline_id", postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_pipelines.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("pipeline_id", "kind", "key", "bucket_start", name="uq_dc_failure_counts_bucket"),
    )
    op.create_index("ix_dc_failure_counts_org_id", "dc_failure_counts", ["org_id"])
    op.create_index("ix_dc_failure_counts_pipeline_id", "dc_failure_counts", ["pipeline_id"])
    op.create_index("ix_dc_failure_counts_org_kind_bucket", "dc_failure_counts", ["org_id", "kind", "bucket_start"])


def downgrade() -> None:
    op.drop_index("ix_dc_failure_counts_org_kind_bucket", table_name="dc_failure_counts")
    op.drop_index("ix_dc_failure_counts_pipeline_id", table_name="dc_failure_counts")
    op.drop_index("ix_dc_failure_counts_org_id", table_name="dc_failure_counts")
    op.drop_table("dc_failure_counts")
