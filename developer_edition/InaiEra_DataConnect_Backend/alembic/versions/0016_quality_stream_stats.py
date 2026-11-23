"""quality stream stats

Revision ID: 0016_quality_stream_stats
Revises: 0015_seed_default_org
Create Date: 2026-09-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_quality_stream_stats"
down_revision = "0015_seed_default_org"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_quality_stream_stats",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column(
            "pipeline_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_pipelines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quality_policy_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_quality_policies.id")),
        sa.Column("total_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("pipeline_id", name="uq_dc_quality_stream_stats_pipeline_id"),
    )
    op.create_index("ix_dc_quality_stream_stats_org_id", "dc_quality_stream_stats", ["org_id"])
    op.create_index("ix_dc_quality_stream_stats_pipeline_id", "dc_quality_stream_stats", ["pipeline_id"])


def downgrade() -> None:
    op.drop_index("ix_dc_quality_stream_stats_pipeline_id", table_name="dc_quality_stream_stats")
    op.drop_index("ix_dc_quality_stream_stats_org_id", table_name="dc_quality_stream_stats")
    op.drop_table("dc_quality_stream_stats")
