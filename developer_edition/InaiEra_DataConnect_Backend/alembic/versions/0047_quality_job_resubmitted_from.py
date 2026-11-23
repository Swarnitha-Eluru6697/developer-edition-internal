"""A quality execution job can record the failed job it replaces

When a job's staged input is gone (QUALITY_JOB_INPUT_MISSING) it can be neither retried nor resumed; the caller resubmits the batch
(POST /quality-execution-jobs/{id}/resubmit), which creates a NEW job. `resubmitted_from` links that job back to the one it replaces.
Nullable, no backfill.

Revision ID: 0047_quality_job_resubmitted_from
Revises: 0046_operation_result_status
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0047_quality_job_resubmitted_from"
down_revision = "0046_operation_result_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_quality_execution_jobs", sa.Column("resubmitted_from", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("dc_quality_execution_jobs", "resubmitted_from")
