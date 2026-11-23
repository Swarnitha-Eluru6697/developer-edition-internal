"""agentlog: a real timestamp

AgentLog.ts holds a bare clock string with no date, so log lines could not be ordered
across midnight or filtered by day. Adds a nullable, indexed `at` column: new rows get the
instant they were written, existing rows stay null and are reported as undated rather than
being backfilled with a date nobody recorded.

Revision ID: a1b2c3d4e5f6
Revises: e6b2d84f1c93
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "e6b2d84f1c93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agentlog", sa.Column("at", sa.DateTime(), nullable=True))
    op.create_index("ix_agentlog_at", "agentlog", ["at"])


def downgrade() -> None:
    op.drop_index("ix_agentlog_at", table_name="agentlog")
    op.drop_column("agentlog", "at")
