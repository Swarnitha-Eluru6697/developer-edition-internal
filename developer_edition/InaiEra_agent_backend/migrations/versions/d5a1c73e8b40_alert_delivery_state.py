"""alert delivery state — which channels an alert actually reached

The Open tab now means "sent to a channel and not yet acknowledged or
resolved", so whether delivery succeeded is part of the alert's state
rather than only a log line on the destination.

It also makes retry possible. An alert raised while no channel existed,
or while one was down, previously had no record of having missed its
delivery — so it could never be sent, and under the new Open-tab rule it
would have been permanently invisible.

Revision ID: d5a1c73e8b40
Revises: c4f8b21d90e7
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d5a1c73e8b40"
down_revision = "c4f8b21d90e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert", sa.Column("delivered_at", sa.DateTime(), nullable=True))
    op.add_column("alert", sa.Column(
        "delivered_to", postgresql.JSONB(), nullable=False, server_default="[]"))
    # The Open tab filters on exactly this, and it is the query that runs
    # on every page load.
    op.create_index("ix_alert_state_acked", "alert", ["state", "acked"])


def downgrade() -> None:
    op.drop_index("ix_alert_state_acked", table_name="alert")
    op.drop_column("alert", "delivered_to")
    op.drop_column("alert", "delivered_at")
