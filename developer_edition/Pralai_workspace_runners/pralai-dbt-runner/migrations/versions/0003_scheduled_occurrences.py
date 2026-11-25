"""Add scheduled_occurrences — unique (project_id, scheduled_fire_time)
so two scheduler replicas cannot both fire the same cron occurrence.

Revision ID: 0003_scheduled_occurrences
Revises:     0002_dq_integration
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_scheduled_occurrences"
down_revision: Union[str, None] = "0002_dq_integration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_fire_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("runs.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "scheduled_fire_time", name="uq_scheduled_occurrence"),
    )
    op.create_index("ix_scheduled_occurrences_project", "scheduled_occurrences", ["project_id"])


def downgrade() -> None:
    op.drop_table("scheduled_occurrences")
