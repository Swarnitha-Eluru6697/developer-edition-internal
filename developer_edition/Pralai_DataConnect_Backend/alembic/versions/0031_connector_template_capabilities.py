"""connector template dlq/schema-discovery capabilities

FE-BUG-23: the Pipeline Designer assumed every connector supported DLQ
routing and server-side schema auto-discovery, since neither had any
template-level flag to say otherwise (unlike supported_modes/
supported_transforms, which already existed — see 0022). These two new
columns, plus supported_modes/supported_transforms, are what
app/schemas/catalog.py's computed `capabilities` field is built from.

Non-nullable with a 'true' default, not 'false': every template today
behaves as if both were supported (DLQ routing is offered unconditionally
in the Designer; schema introspection is attempted unconditionally in
PipelineService.autocreate_schema_from_source), so defaulting existing rows
to True preserves that behavior exactly. Curating a specific template to
False (see app/db/seed_templates.py) is an explicit, deliberate opt-out.

Revision ID: 0031_connector_template_capabilities
Revises: 0030_pipeline_revision
Create Date: 2026-09-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0031_connector_template_capabilities"
down_revision = "0030_pipeline_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dc_connector_templates", "dc_connector_template_revisions"):
        op.add_column(table, sa.Column("supports_dlq", sa.Boolean(), nullable=False, server_default=sa.true()))
        op.add_column(table, sa.Column("supports_schema_discovery", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    for table in ("dc_connector_templates", "dc_connector_template_revisions"):
        op.drop_column(table, "supports_schema_discovery")
        op.drop_column(table, "supports_dlq")
