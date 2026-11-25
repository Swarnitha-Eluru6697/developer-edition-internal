"""Persist max_rows/timeout_seconds on executions.

WS-EXEC-005: submission is moving off the request-blocking path onto a
background controller/worker that can pick up (or recover, after a restart)
an execution using only its DB row. Without these two columns the row wasn't
a fully self-contained command — max_rows/timeout_seconds only ever existed
as in-memory call arguments, lost the moment the request that submitted them
returned.

Revision ID: 0006_execution_command_columns
Revises: 0005_flinksql_file_language
Create Date: 2026-09-17
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0006_execution_command_columns"
down_revision: Union[str, None] = "0005_flinksql_file_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("max_rows", sa.Integer, nullable=False, server_default="10000"),
    )
    op.add_column(
        "executions",
        sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="300"),
    )


def downgrade() -> None:
    op.drop_column("executions", "timeout_seconds")
    op.drop_column("executions", "max_rows")
