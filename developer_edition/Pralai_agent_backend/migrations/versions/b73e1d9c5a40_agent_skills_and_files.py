"""agent node skills and reference files

Revision ID: b73e1d9c5a40
Revises: a62f91c7e4d2
Create Date: 2026-10-20 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b73e1d9c5a40"
down_revision: Union[str, Sequence[str], None] = "a62f91c7e4d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agentskill",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.String(), nullable=False, server_default=""),
        sa.Column("object_key", sa.String(), nullable=False, server_default=""),
        sa.Column("storage_backend", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "name", name="uq_agentskill_agent_name"),
    )
    op.create_index("ix_agentskill_agent_id", "agentskill", ["agent_id"])
    op.create_table(
        "agentfile",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False, server_default="document"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extracted_text", sa.String(), nullable=False, server_default=""),
        sa.Column("object_key", sa.String(), nullable=False, server_default=""),
        sa.Column("storage_backend", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agentfile_agent_id", "agentfile", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agentfile_agent_id", table_name="agentfile")
    op.drop_table("agentfile")
    op.drop_index("ix_agentskill_agent_id", table_name="agentskill")
    op.drop_table("agentskill")
