"""quality execution job lease

BUG-02: QualityExecutionJob now runs on the same claim/lease model as Operation instead of a bare
asyncio.create_task — lease_expires_at + attempt_count let a surviving worker reclaim a job whose
process died, without two live workers ever running the same job. See app/models/quality.py.

Revision ID: 0034_quality_job_lease
Revises: 0033_failure_counts
Create Date: 2026-10-01
"""

import sqlalchemy as sa

from alembic import op

revision = "0034_quality_job_lease"
down_revision = "0033_failure_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_quality_execution_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "dc_quality_execution_jobs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_dc_quality_execution_jobs_lease_expires_at", "dc_quality_execution_jobs", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_dc_quality_execution_jobs_lease_expires_at", table_name="dc_quality_execution_jobs")
    op.drop_column("dc_quality_execution_jobs", "attempt_count")
    op.drop_column("dc_quality_execution_jobs", "lease_expires_at")
