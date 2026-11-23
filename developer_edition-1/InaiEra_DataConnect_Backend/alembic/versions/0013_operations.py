"""operations

Revision ID: 0013_operations
Revises: 0012_pipeline_postgres_readiness
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_operations"
down_revision = "0012_pipeline_postgres_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_operations",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("operation_type", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="queued"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dc_operations_org_id", "dc_operations", ["org_id"])
    op.create_index("ix_dc_operations_operation_type", "dc_operations", ["operation_type"])
    op.create_index("ix_dc_operations_resource_id", "dc_operations", ["resource_id"])


def downgrade() -> None:
    op.drop_index("ix_dc_operations_resource_id", table_name="dc_operations")
    op.drop_index("ix_dc_operations_operation_type", table_name="dc_operations")
    op.drop_index("ix_dc_operations_org_id", table_name="dc_operations")
    op.drop_table("dc_operations")
