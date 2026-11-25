"""pipeline error handling policy

GAP-04: errors.tolerance=all was hard-coded for every pipeline. This adds a
per-pipeline error_handling_policy (FAIL_FAST | DLQ_AND_CONTINUE |
THRESHOLD) plus the two params THRESHOLD needs (max_errors, window) — see
app/schemas/pipeline.py and app/services/pipeline_config.py.

Revision ID: 0021_pipeline_error_handling_policy
Revises: 0020_connection_verification
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0021_pipeline_error_handling_policy"
down_revision = "0020_connection_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_pipelines",
        sa.Column(
            "error_handling_policy", sa.String(length=50), nullable=False, server_default="DLQ_AND_CONTINUE",
        ),
    )
    op.add_column("dc_pipelines", sa.Column("error_threshold_max_errors", sa.Integer(), nullable=True))
    op.add_column("dc_pipelines", sa.Column("error_threshold_window_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("dc_pipelines", "error_threshold_window_seconds")
    op.drop_column("dc_pipelines", "error_threshold_max_errors")
    op.drop_column("dc_pipelines", "error_handling_policy")
