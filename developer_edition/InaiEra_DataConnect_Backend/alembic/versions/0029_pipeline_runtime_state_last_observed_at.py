"""pipeline runtime state last_observed_at

FE-BUG-07: dc_pipeline_runtime_state.last_reconciled_at updates every
reconciler tick this row is claimed, whether or not that tick actually got
a real answer from Kafka Connect (see runtime_reconciler._reconcile_row) —
so it means "last time we tried", not "last time we knew the truth". This
adds the distinct column for that: last_observed_at only advances when
observed_state was actually derived from a genuine Connect response (or a
genuine "not there" from the connector list), so the frontend can honestly
show "Last observed 8 min ago" instead of falsely implying a fresh
observation from a database column that was really just touched by a
failed attempt.

Revision ID: 0029_pipeline_runtime_state_last_observed_at
Revises: 0028_quality_execution_job_records
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "0029_pipeline_runtime_state_last_observed_at"
down_revision = "0028_quality_execution_job_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_pipeline_runtime_state",
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dc_pipeline_runtime_state", "last_observed_at")
