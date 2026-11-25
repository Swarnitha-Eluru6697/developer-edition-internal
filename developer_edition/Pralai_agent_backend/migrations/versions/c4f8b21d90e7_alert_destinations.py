"""alert destinations — Slack and Teams channels

Alerts existed only inside the app: rows were shown on the Alerts page
and nothing ever left the platform. This adds the channels alerts can be
sent to, with per-destination filters so one channel can take
high-severity alerts for one agent without every other notice following
it there.

The webhook URL is a credential — anyone holding one can post into that
channel — so it is stored encrypted, the same way MCP tokens are, and a
separate non-secret `url_hint` exists for the UI to show.

Revision ID: c4f8b21d90e7
Revises: b7e2c04af913
"""
import sqlalchemy as sa
from alembic import op

revision = "c4f8b21d90e7"
down_revision = "b7e2c04af913"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alertdestination",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="slack"),
        sa.Column("webhook_url_encrypted", sa.String(), nullable=True),
        sa.Column("url_hint", sa.String(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_severity", sa.String(), nullable=False, server_default="low"),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agent.id"), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("last_detail", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    # Matching an alert asks "which destinations want this agent's
    # alerts", so agent_id is the column that gets filtered on.
    op.create_index("ix_alertdestination_agent_id", "alertdestination", ["agent_id"])
    # The same channel should not be registered twice under one name —
    # two rows would double-post every matching alert.
    op.create_unique_constraint("uq_alertdestination_name", "alertdestination", ["name"])

    # Only these three are valid, and an unrecognised value would either
    # silently pick the Slack payload shape or drop every alert.
    op.create_check_constraint(
        "ck_alertdestination_kind", "alertdestination", "kind IN ('slack','teams')")
    op.create_check_constraint(
        "ck_alertdestination_min_severity", "alertdestination",
        "min_severity IN ('low','medium','high')")


def downgrade() -> None:
    op.drop_index("ix_alertdestination_agent_id", table_name="alertdestination")
    op.drop_table("alertdestination")
