"""quality stream stats sample failures

Revision ID: 0017_quality_stream_sample_failures
Revises: 0016_quality_stream_stats
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017_quality_stream_sample_failures"
down_revision = "0016_quality_stream_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_quality_stream_stats",
        sa.Column(
            "sample_failures", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
    )


def downgrade() -> None:
    op.drop_column("dc_quality_stream_stats", "sample_failures")
