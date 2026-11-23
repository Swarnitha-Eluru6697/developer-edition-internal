"""add maintenancerun

The scheduled maintenance control loop records each job's outcome here.
Without it, "did retention actually run?" has no answer, and a loop that
stopped weeks ago looks exactly like one that had nothing to do.

Revision ID: d7f4a2b8c916
Revises: c5e8a71d9f34
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d7f4a2b8c916"
down_revision = "c5e8a71d9f34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenancerun",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
    )
    op.create_index("ix_maintenancerun_job", "maintenancerun", ["job"])
    op.create_index("ix_maintenancerun_started_at", "maintenancerun", ["started_at"])
    # The query every missed-run check makes: the latest successful run
    # of one job. Without this index it is a scan of a table that grows
    # by one row per job per interval, forever.
    op.create_index("ix_maintenancerun_job_status_finished",
                    "maintenancerun", ["job", "status", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_maintenancerun_job_status_finished", table_name="maintenancerun")
    op.drop_index("ix_maintenancerun_started_at", table_name="maintenancerun")
    op.drop_index("ix_maintenancerun_job", table_name="maintenancerun")
    op.drop_table("maintenancerun")
