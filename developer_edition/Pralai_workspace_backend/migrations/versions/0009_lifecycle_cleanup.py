"""Restart-safe codegen turn tracking for lifecycle reconciliation.

WS-LIFE-013: a codegen turn's assistant reply was only ever persisted at the
very end of the agent loop — a mid-turn crash left a permanently dangling
conversation (a user prompt with no reply and no way to tell "still running"
from "died") with no way to detect or recover it. This adds:

  - `status` on codegen_messages ('running' | 'completed' | 'failed'), so an
    assistant placeholder row can be written up front and updated in place,
    the same way `executions.status` already works.
  - `ws_session_id`, the caller's live workspace-session id at submit time,
    so a reconciler that finds a stale 'running' row knows which session
    channel to publish the resulting codegen.failed event on (WS-AI-011's
    channel is keyed by that id, not by the codegen session's own id).

No FK on ws_session_id: it is only used to pick a Redis pub/sub channel to
notify on, never joined against `sessions` — a dangling value after that
session row's own lifecycle ends is harmless (nothing is listening on it).

Revision ID: 0009_lifecycle_cleanup
Revises: 0008_codegen_org_settings
Create Date: 2026-09-16
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_lifecycle_cleanup"
down_revision: Union[str, None] = "0008_codegen_org_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "codegen_messages",
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
    )
    op.add_column(
        "codegen_messages",
        sa.Column("ws_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_codegen_message_status",
        "codegen_messages",
        "status IN ('running', 'completed', 'failed')",
    )
    # Reconciler scan: "running assistant messages older than X" — mirrors
    # ix_executions_pending's shape for the same query pattern.
    op.create_index(
        "ix_codegen_messages_running",
        "codegen_messages",
        ["created_at"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_codegen_messages_running", table_name="codegen_messages")
    op.drop_constraint("ck_codegen_message_status", "codegen_messages", type_="check")
    op.drop_column("codegen_messages", "ws_session_id")
    op.drop_column("codegen_messages", "status")
