"""Failure-count offsets are kept per topic, not per pipeline

NEW-04: a connector dead-letter topic can be shared by several pipelines, and the counter now attributes each record to the pipeline whose connector
wrote it. "Everything below this offset has been read and counted" is therefore a fact about the TOPIC (kind, topic, partition); keyed by pipeline it
would be lost when that pipeline was deleted and the rest of the topic counted twice.

Existing rows (0049 keyed them by pipeline): for each (kind, topic, partition) the HIGHEST offset is kept — the pipeline that was counting the topic
had read at least that far — and the pipeline is replaced by its organization. Downgrading restores the per-pipeline shape EMPTY: it is bookkeeping
only, and the counter starts from Kafka's committed offsets again (which is the behaviour before NEW-03).

Revision ID: 0050_failure_count_offsets_per_topic
Revises: 0049_failure_count_offsets
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0050_failure_count_offsets_per_topic"
down_revision = "0049_failure_count_offsets"
branch_labels = None
depends_on = None

TABLE = "dc_failure_count_offsets"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("org_id", sa.String(length=100), nullable=True))
    op.execute(f"UPDATE {TABLE} o SET org_id = p.org_id FROM dc_pipelines p WHERE p.id = o.pipeline_id")
    # One row per (kind, topic, partition): keep the greatest offset (ties: the lowest pipeline id, so the choice is deterministic).
    op.execute(
        f"DELETE FROM {TABLE} a USING {TABLE} b "
        "WHERE a.kind = b.kind AND a.topic = b.topic AND a.kafka_partition = b.kafka_partition "
        "AND (a.next_offset < b.next_offset OR (a.next_offset = b.next_offset AND a.pipeline_id > b.pipeline_id))"
    )
    op.drop_constraint(f"{TABLE}_pkey", TABLE, type_="primary")
    op.drop_column(TABLE, "pipeline_id")                       # also drops its foreign key
    op.alter_column(TABLE, "org_id", nullable=False)
    op.create_primary_key(f"{TABLE}_pkey", TABLE, ["kind", "topic", "kafka_partition"])
    op.create_foreign_key(f"{TABLE}_org_id_fkey", TABLE, "dc_organizations", ["org_id"], ["org_id"], ondelete="CASCADE")


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE}")                          # bookkeeping only: nothing maps a topic's offsets back to one pipeline
    op.drop_constraint(f"{TABLE}_org_id_fkey", TABLE, type_="foreignkey")
    op.drop_constraint(f"{TABLE}_pkey", TABLE, type_="primary")
    op.drop_column(TABLE, "org_id")
    op.add_column(TABLE, sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), nullable=False))
    op.create_foreign_key(f"{TABLE}_pipeline_id_fkey", TABLE, "dc_pipelines", ["pipeline_id"], ["id"], ondelete="CASCADE")
    op.create_primary_key(f"{TABLE}_pkey", TABLE, ["pipeline_id", "kind", "topic", "kafka_partition"])
