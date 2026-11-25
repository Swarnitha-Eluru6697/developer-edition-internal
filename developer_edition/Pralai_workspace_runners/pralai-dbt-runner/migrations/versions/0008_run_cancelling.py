"""DBT-005: a 'cancelling' run status and runs.cancel_requested_at.

Cancelling a running run used to flip it to 'cancelled' and only then try (best effort) to delete its Kubernetes
Job, so a run could read 'cancelled' while its Job kept executing. Now running -> cancelling -> cancelled: the
run becomes 'cancelled' only once the cluster confirms the Job is gone.

Revision ID: 0008_run_cancelling
Revises:     0007_project_path
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0008_run_cancelling"
down_revision: Union[str, None] = "0007_project_path"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_run_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_run_status", "runs",
        "status IN ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled')",
    )
    op.drop_index("ix_runs_pending", table_name="runs")
    op.create_index("ix_runs_pending", "runs", ["status"],
                    postgresql_where=sa.text("status IN ('queued', 'running', 'cancelling')"))


def downgrade() -> None:
    # A run still 'cancelling' is reported as cancelled: the older schema has no way to say "in progress".
    op.execute("UPDATE runs SET status = 'cancelled', completed_at = COALESCE(completed_at, now()) "
               "WHERE status = 'cancelling'")
    op.drop_index("ix_runs_pending", table_name="runs")
    op.create_index("ix_runs_pending", "runs", ["status"],
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_constraint("ck_run_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_run_status", "runs", "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
    )
    op.drop_column("runs", "cancel_requested_at")
