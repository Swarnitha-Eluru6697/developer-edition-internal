"""schema inference runs

Revision ID: 0009_schema_inference_runs
Revises: 0008_quality_execution_jobs
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009_schema_inference_runs"
down_revision = "0008_quality_execution_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_schema_inference_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("sample_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dc_schema_inference_runs_org_id", "dc_schema_inference_runs", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_dc_schema_inference_runs_org_id", table_name="dc_schema_inference_runs")
    op.drop_table("dc_schema_inference_runs")
