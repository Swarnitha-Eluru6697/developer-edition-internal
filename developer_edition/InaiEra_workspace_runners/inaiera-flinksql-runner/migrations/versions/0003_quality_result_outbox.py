"""Add quality_result_outbox — durable staging store for quality violation
results (DQ-RESULT-009).

Revision ID: 0003_quality_result_outbox
Revises:     0002_quality_fields
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_quality_result_outbox"
down_revision: Union[str, None] = "0002_quality_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "quality_result_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                 server_default=sa.text("gen_random_uuid()")),
        # The producing Kafka record's own topic-partition-offset. Reprocessing
        # the exact same Kafka message (e.g. after a crash before the offset
        # was committed) always derives the same key, so the unique
        # constraint below makes a retried insert a safe no-op instead of a
        # duplicate UCS result.
        sa.Column("idempotency_key", sa.String(500), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'delivered')", name="ck_quality_outbox_status"),
        sa.UniqueConstraint("idempotency_key", name="uq_quality_outbox_idempotency_key"),
    )
    op.create_index("ix_quality_outbox_status", "quality_result_outbox", ["status"])


def downgrade() -> None:
    op.drop_index("ix_quality_outbox_status", table_name="quality_result_outbox")
    op.drop_table("quality_result_outbox")
