"""Execution cancel intent and worker lease live in Postgres (W-017, W-018).

  - `cancel_requested_at`: a user's cancel request on a running execution. It used to be a Redis key only, so a Redis
    restart/flush/outage lost it and the reconciler then recorded the run as INTERRUPTED (or let it carry on).
  - `lease_owner`, `lease_expires_at`: which worker owns a running execution, and until when. They are written by the
    same UPDATE that claims the row, so a row can no longer be `running` without an owner (it used to be claimed in
    Postgres and only then given a lease in Redis).

All nullable and additive: a row claimed by an older worker has no lease columns and is recovered by the old budget rule
(started_at + timeout + grace) until it finishes.

Revision ID: 0011_execution_cancel_lease
Revises: 0010_execution_worker_context
Create Date: 2026-10-14
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0011_execution_cancel_lease"
down_revision: Union[str, None] = "0010_execution_worker_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("executions", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("executions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "lease_expires_at")
    op.drop_column("executions", "lease_owner")
    op.drop_column("executions", "cancel_requested_at")
