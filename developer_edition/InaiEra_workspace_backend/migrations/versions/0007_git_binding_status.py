"""Explicit provisioning/failed state on git_bindings.

WS-GIT-009: a repo clone+import that partially failed used to leave the
binding row looking indistinguishable from a fully-synced one — the only
record of failure was a one-time API response nobody persisted. This adds a
status the UI can actually check.

Revision ID: 0007_git_binding_status
Revises: 0006_execution_command_columns
Create Date: 2026-09-16
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0007_git_binding_status"
down_revision: Union[str, None] = "0006_execution_command_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "git_bindings",
        sa.Column("status", sa.String(20), nullable=False, server_default="ready"),
    )
    op.add_column(
        "git_bindings",
        sa.Column("status_reason", sa.Text, nullable=True),
    )
    op.create_check_constraint(
        "ck_git_binding_status",
        "git_bindings",
        "status IN ('provisioning', 'ready', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_git_binding_status", "git_bindings", type_="check")
    op.drop_column("git_bindings", "status_reason")
    op.drop_column("git_bindings", "status")
