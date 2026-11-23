"""A pipeline's runtime state records when its quality worker is stuck

The quality stream worker never commits past a record it can neither handle nor dead-letter (BUG-03), so a dead-letter-topic outage
stalls its partition indefinitely. GAP-03 makes that visible: the worker stamps `quality_blocked_since` when the current record
first fails and keeps `quality_blocked_heartbeat_at` fresh while it stays stuck, and the reconciler reports the pipeline DEGRADED
once the stall passes a configured age. Both columns are NULL when nothing is stuck, so existing rows need no backfill.

Revision ID: 0045_quality_worker_blocked
Revises: 0044_drop_topic_separator
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0045_quality_worker_blocked"
down_revision = "0044_drop_topic_separator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_pipeline_runtime_state", sa.Column("quality_blocked_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dc_pipeline_runtime_state", sa.Column("quality_blocked_heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("dc_pipeline_runtime_state", "quality_blocked_heartbeat_at")
    op.drop_column("dc_pipeline_runtime_state", "quality_blocked_since")
