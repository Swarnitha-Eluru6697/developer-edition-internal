"""pipeline direction

GAP-12: source and sink connectors don't behave the same way, but nothing
recorded which one a pipeline actually was — only inferable by joining to
its ConnectorTemplate.connector_type. This adds an explicit, stored
direction (SOURCE | SINK), backfilling existing rows from their
template's connector_type (defaulting to SOURCE for the rare case of an
orphaned/unmatched template_id, so the column can be NOT NULL).

Revision ID: 0027_pipeline_direction
Revises: 0026_error_classification
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0027_pipeline_direction"
down_revision = "0026_error_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_pipelines", sa.Column("direction", sa.String(length=10), nullable=True))
    op.execute(
        """
        UPDATE dc_pipelines p
        SET direction = UPPER(t.connector_type)
        FROM dc_connector_templates t
        WHERE p.template_id = t.id
        """
    )
    op.execute("UPDATE dc_pipelines SET direction = 'SOURCE' WHERE direction IS NULL")
    op.alter_column("dc_pipelines", "direction", nullable=False)


def downgrade() -> None:
    op.drop_column("dc_pipelines", "direction")
