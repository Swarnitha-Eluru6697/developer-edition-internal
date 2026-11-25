"""Add flink_deployments.idempotency_key/stop_idempotency_key plus a
partial-unique "one active generation per job" index (RUN-FLINK-014) — a
real DB uniqueness constraint, not just the optimistic status-guarded
UPDATE in job_service.deploy()/stop(), so concurrent deploy/stop calls
for the same job can never both create/claim a submission, and a client
retrying with the same idempotency key is guaranteed to land on the same
row instead of racing a duplicate one into existence.

Revision ID: 0005_deploy_stop_idempotency
Revises:     0004_deployment_desired_state
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0005_deploy_stop_idempotency"
down_revision: Union[str, None] = "0004_deployment_desired_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# RUN-FLINK-014: a database that accumulated more than one 'submitting'
# row per job before this unique index existed would fail to build it.
# Keep the newest such row per job and mark the older ones 'failed' (rows
# are kept for history, never deleted). Shared with 0006, which re-runs it
# defensively for databases that already applied this revision.
DEDUPE_SUBMITTING_SQL = """
UPDATE flink_deployments d
   SET status = 'failed',
       error_message = COALESCE(d.error_message || ' | ', '')
                       || 'superseded duplicate submitting row (de-duplicated by migration)'
  FROM (
        SELECT id,
               row_number() OVER (PARTITION BY job_id
                                  ORDER BY created_at DESC, id DESC) AS rn
          FROM flink_deployments
         WHERE status = 'submitting'
       ) ranked
 WHERE d.id = ranked.id
   AND ranked.rn > 1
"""


def upgrade() -> None:
    op.execute(DEDUPE_SUBMITTING_SQL)
    op.add_column("flink_deployments", sa.Column("idempotency_key", sa.String(255)))
    op.add_column("flink_deployments", sa.Column("stop_idempotency_key", sa.String(255)))

    op.create_index(
        "uq_flink_dep_active_generation", "flink_deployments", ["job_id"],
        unique=True, postgresql_where=sa.text("status = 'submitting'"),
    )
    op.create_index(
        "uq_flink_dep_idempotency_key", "flink_deployments", ["job_id", "idempotency_key"],
        unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_flink_dep_stop_idempotency_key", "flink_deployments", ["job_id", "stop_idempotency_key"],
        unique=True, postgresql_where=sa.text("stop_idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_flink_dep_stop_idempotency_key", table_name="flink_deployments")
    op.drop_index("uq_flink_dep_idempotency_key", table_name="flink_deployments")
    op.drop_index("uq_flink_dep_active_generation", table_name="flink_deployments")
    op.drop_column("flink_deployments", "stop_idempotency_key")
    op.drop_column("flink_deployments", "idempotency_key")
