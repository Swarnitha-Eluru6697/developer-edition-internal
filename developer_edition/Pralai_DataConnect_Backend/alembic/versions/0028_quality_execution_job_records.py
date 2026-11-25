"""quality execution job records

GAP-13: a QualityExecutionJob's `records` payload used to live only in the
submitting process's memory (passed straight into the asyncio.create_task
running the job), so a restart lost it outright — a queued/running job
could only be reaped as failed, never resumed. This adds a separate,
TTL'd table for that payload (already size-bounded at submission time by
settings.quality_job_max_records) so it survives a restart, without making
QualityExecutionJob itself hold an unbounded column.

Revision ID: 0028_quality_execution_job_records
Revises: 0027_pipeline_direction
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0028_quality_execution_job_records"
down_revision = "0027_pipeline_direction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_quality_execution_job_records",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_quality_execution_jobs.id", ondelete="CASCADE"), nullable=False, unique=True,
        ),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("records", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_dc_quality_execution_job_records_job_id", "dc_quality_execution_job_records", ["job_id"], unique=True,
    )
    op.create_index("ix_dc_quality_execution_job_records_org_id", "dc_quality_execution_job_records", ["org_id"])
    op.create_index(
        "ix_dc_quality_execution_job_records_expires_at", "dc_quality_execution_job_records", ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dc_quality_execution_job_records_expires_at", table_name="dc_quality_execution_job_records")
    op.drop_index("ix_dc_quality_execution_job_records_org_id", table_name="dc_quality_execution_job_records")
    op.drop_index("ix_dc_quality_execution_job_records_job_id", table_name="dc_quality_execution_job_records")
    op.drop_table("dc_quality_execution_job_records")
