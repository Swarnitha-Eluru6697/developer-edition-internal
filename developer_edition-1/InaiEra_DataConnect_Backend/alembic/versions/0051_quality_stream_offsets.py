"""Remember which Kafka offsets a pipeline's quality statistics already cover

NEW-05: the quality stream worker records a record's statistics and only afterwards commits its Kafka offset, so a crash in between replayed the
record and counted it a second time (total/failed counts, the failure sample, the per-rule counts). This table holds, per pipeline/topic/partition,
the next offset NOT yet counted; it is updated in the same transaction as the statistics, so a replay is recognised and not counted again. New and
empty: nothing to backfill. The first run after upgrade reads from Kafka's committed offset as before and records its progress from then on.

Revision ID: 0051_quality_stream_offsets
Revises: 0050_failure_count_offsets_per_topic
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0051_quality_stream_offsets"
down_revision = "0050_failure_count_offsets_per_topic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_quality_stream_offsets",
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("topic", sa.String(255), primary_key=True),
        sa.Column("kafka_partition", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("next_offset", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dc_quality_stream_offsets")
