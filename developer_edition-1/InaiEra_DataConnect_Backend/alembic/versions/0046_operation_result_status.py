"""Operations record their result-contract outcome separately from their execution outcome

An Operation's `status` says whether the action ran. Until now a result that failed its contract (or was too large) was dropped,
logged, and the operation was stored as a plain `succeeded` with no result — indistinguishable from a healthy one unless someone read
the logs (GAP-04). `result_status` ('valid' | 'invalid') now records the contract outcome on its own; an invalid one also carries
error_code OPERATION_RESULT_INVALID while status stays 'succeeded'.

Backfill: every operation type returns a result, so a succeeded row that has one was valid, and a succeeded row with none was one
whose result was rejected. Rows that never succeeded (failed / stale / queued / running) stay NULL.

Revision ID: 0046_operation_result_status
Revises: 0045_quality_worker_blocked
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0046_operation_result_status"
down_revision = "0045_quality_worker_blocked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_operations", sa.Column("result_status", sa.String(20), nullable=True, comment="valid | invalid"))
    op.create_check_constraint("ck_dc_operations_result_status", "dc_operations", "result_status IN ('valid', 'invalid')")
    op.execute("UPDATE dc_operations SET result_status = 'valid' WHERE status = 'succeeded' AND result IS NOT NULL")
    op.execute("UPDATE dc_operations SET result_status = 'invalid' WHERE status = 'succeeded' AND result IS NULL")


def downgrade() -> None:
    op.drop_constraint("ck_dc_operations_result_status", "dc_operations", type_="check")
    op.drop_column("dc_operations", "result_status")
