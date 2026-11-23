"""connector plugin/version compatibility fields

Revision ID: 0005_connector_plugin_compatibility
Revises: 0004_connector_template_revisions
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_connector_plugin_compatibility"
down_revision = "0004_connector_template_revisions"
branch_labels = None
depends_on = None

# Nullable on both tables, same as current_revision_id in 0004 — existing
# rows are backfilled by app/db/seed_templates.py's startup refresh, not by
# a migration-time default, since the real values are qualification data
# owned by the seed catalog / platform admins, not derivable from the
# schema change itself.
_NEW_COLUMNS = [
    ("connector_plugin", sa.String(length=255)),
    ("connector_version", sa.String(length=50)),
    ("supported_kafka_connect_versions", postgresql.JSONB()),
    ("supported_source_versions", postgresql.JSONB()),
    ("config_schema_version", sa.String(length=20)),
    ("transform_compatibility", postgresql.JSONB()),
    ("migration_notes", sa.Text()),
]


def upgrade() -> None:
    for table in ("dc_connector_templates", "dc_connector_template_revisions"):
        for name, col_type in _NEW_COLUMNS:
            op.add_column(table, sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    for table in ("dc_connector_templates", "dc_connector_template_revisions"):
        for name, _col_type in reversed(_NEW_COLUMNS):
            op.drop_column(table, name)
