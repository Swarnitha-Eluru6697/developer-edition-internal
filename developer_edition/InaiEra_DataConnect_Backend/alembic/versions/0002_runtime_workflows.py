"""runtime workflow tables

Revision ID: 0002_runtime_workflows
Revises: 0001_initial
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_runtime_workflows"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_schema_drift_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("schema_version_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_schema_versions.id")),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id")),
        sa.Column("drift_events", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("observed_schema", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("requested_by", sa.String(length=255)),
        sa.Column("reviewed_by", sa.String(length=255)),
        sa.Column("review_comment", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("dc_schema_drift_approvals")

