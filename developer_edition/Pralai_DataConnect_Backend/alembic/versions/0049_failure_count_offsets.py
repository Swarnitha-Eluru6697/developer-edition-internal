"""Remember which Kafka offsets the failure counters have already counted

NEW-03: the DLQ counter wrote its counts to PostgreSQL and only then committed its Kafka offsets, so a crash in between replayed a batch and
counted it twice. This table holds, per pipeline/topic/partition, the next offset NOT yet counted; it is updated in the same transaction as the
counts, so a replay is recognised and skipped. A new, empty table: nothing to backfill. The first run after upgrade reads from Kafka's
committed offset as before and records its progress from then on.

Revision ID: 0049_failure_count_offsets
Revises: 0048_pipeline_quality_mode
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0049_failure_count_offsets"
down_revision = "0048_pipeline_quality_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_failure_count_offsets",
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.Column("topic", sa.String(255), primary_key=True),
        sa.Column("kafka_partition", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("next_offset", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dc_failure_count_offsets")
