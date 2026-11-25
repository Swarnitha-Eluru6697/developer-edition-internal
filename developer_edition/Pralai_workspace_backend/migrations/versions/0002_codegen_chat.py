"""Codegen chat — codegen_sessions, codegen_messages.

Adds the embedded-in-Workspace chat used to generate SQL, dbt, and
FlinkSQL code via Claude. See app/services/codegen.py.

Revision ID: 0002_codegen_chat
Revises: 0001_initial
Create Date: 2026-06-30

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_codegen_chat"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "codegen_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspace_files.id", ondelete="SET NULL")),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("engine", sa.String(30)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("target IN ('sql', 'dbt', 'flinksql')", name="ck_codegen_target"),
    )
    op.create_index("ix_codegen_sessions_workspace", "codegen_sessions", ["workspace_id"])
    op.create_index("ix_codegen_sessions_user", "codegen_sessions", ["user_id"])

    op.create_table(
        "codegen_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("codegen_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", postgresql.JSONB, nullable=False),
        sa.Column("generated_code", sa.Text),
        sa.Column("generated_path", sa.String(1024)),
        sa.Column("validation_status", sa.String(20)),
        sa.Column("validation_detail", postgresql.JSONB),
        sa.Column("model", sa.String(100)),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant', 'tool')", name="ck_codegen_message_role"),
    )
    op.create_index("ix_codegen_messages_session", "codegen_messages", ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_table("codegen_messages")
    op.drop_table("codegen_sessions")
