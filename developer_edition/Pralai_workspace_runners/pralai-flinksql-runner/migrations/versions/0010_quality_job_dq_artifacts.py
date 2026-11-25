"""flink_jobs.dq_artifacts: which DQ artifact a quality job's SQL was generated from (X-004).

The Data Quality service creates and updates the QUALITY job it renders for a binding. It now says which artifact (id, content
hash, renderer version, ruleset and its version) that SQL came from; the runner stores it here, and every deployment of the
job freezes it into its manifest (`manifest.dq_artifacts`, until now always null = "not recorded"). Only the DQ service may
set it (it is evidence a user must not be able to claim). NULL for every other job and for quality jobs created before DQ
sent it. Additive: nothing is rewritten.

Revision ID: 0010_quality_job_dq_artifacts
Revises:     0009_quality_outbox_backoff
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_quality_job_dq_artifacts"
down_revision: Union[str, None] = "0009_quality_outbox_backoff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flink_jobs", sa.Column("dq_artifacts", postgresql.JSONB(astext_type=sa.Text())))


def downgrade() -> None:
    op.drop_column("flink_jobs", "dq_artifacts")
