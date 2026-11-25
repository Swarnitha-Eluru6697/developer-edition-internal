"""quality policy quarantine sink

Revision ID: 0007_quality_quarantine_sink
Revises: 0006_topic_policies
Create Date: 2026-09-15
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_quality_quarantine_sink"
down_revision = "0006_topic_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_quality_policies",
        sa.Column(
            "quarantine_sink",
            sa.String(length=50),
            nullable=False,
            server_default="kafka",
        ),
    )


def downgrade() -> None:
    op.drop_column("dc_quality_policies", "quarantine_sink")
