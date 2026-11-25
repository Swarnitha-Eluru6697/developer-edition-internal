"""quality execution jobs

Revision ID: 0008_quality_execution_jobs
Revises: 0007_quality_quarantine_sink
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_quality_execution_jobs"
down_revision = "0007_quality_quarantine_sink"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_quality_execution_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id"), nullable=False),
        sa.Column(
            "quality_policy_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_quality_policies.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="queued"),
        sa.Column("record_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("result_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_quality_results.id")),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dc_quality_execution_jobs_org_id", "dc_quality_execution_jobs", ["org_id"])
    op.create_index("ix_dc_quality_execution_jobs_pipeline_id", "dc_quality_execution_jobs", ["pipeline_id"])


def downgrade() -> None:
    op.drop_index("ix_dc_quality_execution_jobs_pipeline_id", table_name="dc_quality_execution_jobs")
    op.drop_index("ix_dc_quality_execution_jobs_org_id", table_name="dc_quality_execution_jobs")
    op.drop_table("dc_quality_execution_jobs")
