"""pipeline runtime reconciliation state

Revision ID: 0003_pipeline_runtime_state
Revises: 0002_runtime_workflows
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_pipeline_runtime_state"
down_revision = "0002_runtime_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_pipeline_runtime_state",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), nullable=False),
        sa.Column("pipeline_id", sa.String(length=64), nullable=False),
        sa.Column("connector_name", sa.String(length=500), nullable=False),
        sa.Column("desired_state", sa.String(length=50), nullable=False),
        sa.Column("desired_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("config_hash", sa.String(length=64)),
        sa.Column("observed_state", sa.String(length=50), nullable=False, server_default="unknown"),
        sa.Column("observed_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("last_runtime_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("pipeline_id"),
    )
    op.create_index(
        "ix_dc_pipeline_runtime_state_org_id", "dc_pipeline_runtime_state", ["org_id"]
    )
    op.create_index(
        "ix_dc_pipeline_runtime_state_pipeline_id", "dc_pipeline_runtime_state", ["pipeline_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_dc_pipeline_runtime_state_pipeline_id", table_name="dc_pipeline_runtime_state")
    op.drop_index("ix_dc_pipeline_runtime_state_org_id", table_name="dc_pipeline_runtime_state")
    op.drop_table("dc_pipeline_runtime_state")
