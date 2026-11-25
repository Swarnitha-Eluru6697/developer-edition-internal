"""X-004: runs.idempotency_key (+ request hash) and runs.source_tree_sha.

A client-supplied Idempotency-Key makes a retried submission return the run it created (Dagster's submit step retries
used to create a second run); the request hash refuses the same key with a different request. `source_tree_sha` is
the evidence of what was built: Git's tree id of the project folder, reported by the Job after its checkout.

Revision ID: 0010_run_idempotency_evidence
Revises:     0009_run_manifest
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0010_run_idempotency_evidence"
down_revision: Union[str, None] = "0009_run_manifest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("idempotency_key", sa.String(255)))
    op.add_column("runs", sa.Column("idempotency_request_hash", sa.String(64)))
    op.add_column("runs", sa.Column("source_tree_sha", sa.String(64)))
    op.create_index("uq_runs_idempotency_key", "runs", ["project_id", "idempotency_key"], unique=True,
                    postgresql_where=sa.text("idempotency_key IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_runs_idempotency_key", table_name="runs")
    op.drop_column("runs", "source_tree_sha")
    op.drop_column("runs", "idempotency_request_hash")
    op.drop_column("runs", "idempotency_key")
