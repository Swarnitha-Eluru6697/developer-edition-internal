"""Execution worker context: request correlation + submitter roles.

WS-EXEC-005 moves execution into a separate worker process, so the worker
needs, from the row alone:
  - `request_id` / `trace_id` of the submitting API request, so worker logs
    for a run correlate with the request that created it (WS-016);
  - `principal_roles`, the submitter's roles at submit time, so the worker
    can re-check UCS authorization for a connection right before running
    (WS-DATA-007) — a grant revoked while the row waited is enforced.

All nullable: rows submitted before this migration simply run without them.

Revision ID: 0010_execution_worker_context
Revises: 0009_lifecycle_cleanup
Create Date: 2026-10-05
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0010_execution_worker_context"
down_revision: Union[str, None] = "0009_lifecycle_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("request_id", sa.String(64), nullable=True))
    op.add_column("executions", sa.Column("trace_id", sa.String(64), nullable=True))
    op.add_column("executions", sa.Column("principal_roles", sa.String(1000), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "principal_roles")
    op.drop_column("executions", "trace_id")
    op.drop_column("executions", "request_id")
