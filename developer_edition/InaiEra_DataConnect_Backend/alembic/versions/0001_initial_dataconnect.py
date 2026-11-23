"""initial dataconnect schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_organizations",
        sa.Column("org_id", sa.String(length=100), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_connector_templates",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("connector_type", sa.String(length=50), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("connector_class", sa.String(length=500), nullable=False),
        sa.Column("runtime_pool_type", sa.String(length=100), nullable=False),
        sa.Column("config_schema", postgresql.JSONB(), nullable=False),
        sa.Column("default_config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("default_transforms", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_connection_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("connection_config", postgresql.JSONB(), nullable=False),
        sa.Column("secret_ref", sa.String(length=1000)),
        sa.Column("network_profile", sa.String(length=255)),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_transform_policies",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("transforms", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_schema_versions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("domain", sa.String(length=100), nullable=False),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("entity", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("schema_format", sa.String(length=50), nullable=False, server_default="json_schema"),
        sa.Column("schema_definition", postgresql.JSONB(), nullable=False),
        sa.Column("compatibility", sa.String(length=50), nullable=False, server_default="BACKWARD"),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "domain", "source_system", "entity", "version"),
    )
    op.create_table(
        "dc_quality_policies",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("rules", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("on_failure", sa.String(length=100), nullable=False, server_default="route_to_quarantine"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_pipelines",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("template_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_connector_templates.id")),
        sa.Column("connection_profile_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_connection_profiles.id")),
        sa.Column("transform_policy_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_transform_policies.id")),
        sa.Column("schema_version_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_schema_versions.id")),
        sa.Column("quality_policy_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_quality_policies.id")),
        sa.Column("zone", sa.String(length=50), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=False),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("entity", sa.String(length=100), nullable=False),
        sa.Column("mode", sa.String(length=50), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("topic_name", sa.String(length=500), nullable=False),
        sa.Column("connector_name", sa.String(length=500), nullable=False),
        sa.Column("connector_config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("generated_config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("runtime_pool_id", postgresql.UUID(as_uuid=False)),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "topic_name"),
        sa.UniqueConstraint("org_id", "connector_name"),
    )
    op.create_table(
        "dc_assets",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("asset_type", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("qualified_name", sa.String(length=1000), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "qualified_name"),
    )
    op.create_table(
        "dc_pipeline_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id")),
        sa.Column("run_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("records_read", sa.BigInteger(), server_default="0"),
        sa.Column("records_written", sa.BigInteger(), server_default="0"),
        sa.Column("records_failed", sa.BigInteger(), server_default="0"),
        sa.Column("error_message", sa.Text()),
        sa.Column("runtime_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "dc_lineage_edges",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("from_asset_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_assets.id")),
        sa.Column("to_asset_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_assets.id")),
        sa.Column("edge_type", sa.String(length=100), nullable=False),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id")),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_quality_results",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_pipelines.id")),
        sa.Column("quality_policy_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("dc_quality_policies.id")),
        sa.Column("topic_name", sa.String(length=500)),
        sa.Column("rule_name", sa.String(length=255)),
        sa.Column("severity", sa.String(length=50)),
        sa.Column("status", sa.String(length=50)),
        sa.Column("failed_count", sa.BigInteger(), server_default="0"),
        sa.Column("total_count", sa.BigInteger(), server_default="0"),
        sa.Column("sample_failures", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("measured_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "dc_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id")),
        sa.Column("actor", sa.String(length=255)),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=100)),
        sa.Column("resource_id", sa.String(length=255)),
        sa.Column("before_state", postgresql.JSONB()),
        sa.Column("after_state", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in [
        "dc_audit_logs",
        "dc_quality_results",
        "dc_lineage_edges",
        "dc_pipeline_runs",
        "dc_assets",
        "dc_pipelines",
        "dc_quality_policies",
        "dc_schema_versions",
        "dc_transform_policies",
        "dc_connection_profiles",
        "dc_connector_templates",
        "dc_organizations",
    ]:
        op.drop_table(table)

