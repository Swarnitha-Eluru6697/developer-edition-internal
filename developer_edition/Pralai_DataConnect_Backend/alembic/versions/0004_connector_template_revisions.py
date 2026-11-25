"""connector template revisions

Revision ID: 0004_connector_template_revisions
Revises: 0003_pipeline_runtime_state
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_connector_template_revisions"
down_revision = "0003_pipeline_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_connector_template_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        # Deliberately no FK to dc_connector_templates — this history must
        # outlive the template it was captured from. See the model docstring.
        sa.Column("template_id", sa.String(length=100), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("connector_class", sa.String(length=500), nullable=False),
        sa.Column("default_config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("default_transforms", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("config_schema", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("supported_transforms", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_dc_connector_template_revisions_template_id",
        "dc_connector_template_revisions",
        ["template_id"],
    )

    op.add_column(
        "dc_connector_templates",
        sa.Column(
            "current_revision_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_connector_template_revisions.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dc_connector_templates", "current_revision_id")
    op.drop_index(
        "ix_dc_connector_template_revisions_template_id",
        table_name="dc_connector_template_revisions",
    )
    op.drop_table("dc_connector_template_revisions")
