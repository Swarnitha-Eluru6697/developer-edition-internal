"""topic provisioning policies

Revision ID: 0006_topic_policies
Revises: 0005_connector_plugin_compatibility
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_topic_policies"
down_revision = "0005_connector_plugin_compatibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_topic_policies",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=100), nullable=True),
        sa.Column("is_domain_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("partitions", sa.Integer(), nullable=True),
        sa.Column("replication_factor", sa.Integer(), nullable=True),
        sa.Column("retention_ms", sa.Integer(), nullable=True),
        sa.Column("retention_bytes", sa.Integer(), nullable=True),
        sa.Column("cleanup_policy", sa.String(length=20), nullable=True),
        sa.Column("min_insync_replicas", sa.Integer(), nullable=True),
        sa.Column("compression_type", sa.String(length=20), nullable=True),
        sa.Column("max_message_bytes", sa.Integer(), nullable=True),
        sa.Column("schema_strategy", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dc_topic_policies_org_id", "dc_topic_policies", ["org_id"])

    op.add_column(
        "dc_pipelines",
        sa.Column(
            "topic_policy_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_topic_policies.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dc_pipelines", "topic_policy_id")
    op.drop_index("ix_dc_topic_policies_org_id", table_name="dc_topic_policies")
    op.drop_table("dc_topic_policies")
