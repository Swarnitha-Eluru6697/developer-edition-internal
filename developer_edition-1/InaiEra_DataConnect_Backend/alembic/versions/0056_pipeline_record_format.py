"""Pipelines record how their connector writes records: v1 (as before) or v2 (exact decimals, keyed JDBC rows, CDC rows with a schema, upserting sinks)

The 2026-11-02 MySQL large-data test found that money became a double (Postgres NUMERIC), that MySQL's exact DECIMAL could not be read by the quality
worker, that a restarted JDBC source left duplicate rows in an insert-only sink, and that no database sink could read a CDC topic. Fixing that changes
the connector config, and the reconciler re-applies every pipeline's config on each tick, so the change is tied to a per-pipeline setting instead: every
pipeline that exists now keeps "v1" (its topics and tables keep their shape), every pipeline created from now on is "v2". See
app/models/pipeline.py (record_format) and app/services/connect_config.py.

Revision ID: 0056_pipeline_record_format
Revises: 0055_alerts
Create Date: 2026-11-02
"""

import sqlalchemy as sa

from alembic import op

revision = "0056_pipeline_record_format"
down_revision = "0055_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows get v1 through the column default; new ones v2 from then on.
    op.add_column("dc_pipelines", sa.Column("record_format", sa.String(length=8), nullable=False, server_default="v1"))
    op.alter_column("dc_pipelines", "record_format", server_default="v2")
    op.create_check_constraint("ck_dc_pipelines_record_format", "dc_pipelines", "record_format IN ('v1', 'v2')")


def downgrade() -> None:
    op.drop_constraint("ck_dc_pipelines_record_format", "dc_pipelines", type_="check")
    op.drop_column("dc_pipelines", "record_format")
