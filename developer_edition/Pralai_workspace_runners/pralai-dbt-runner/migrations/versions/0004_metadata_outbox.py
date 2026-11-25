"""Add metadata_outbox_events — durable queue for lineage/dataset
registration publication, retried until UCS/Datasets acknowledges
instead of being sent best-effort and dropped on failure.

Revision ID: 0004_metadata_outbox
Revises:     0003_scheduled_occurrences
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_metadata_outbox"
down_revision: Union[str, None] = "0003_scheduled_occurrences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metadata_outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_error", sa.Text),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_metadata_outbox_idempotency_key"),
        sa.CheckConstraint("status IN ('pending', 'delivered')", name="ck_metadata_outbox_status"),
        sa.CheckConstraint("event_type IN ('openlineage', 'dataset_registration')",
                           name="ck_metadata_outbox_event_type"),
    )
    op.create_index(
        "ix_metadata_outbox_pending", "metadata_outbox_events", ["status", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("metadata_outbox_events")
