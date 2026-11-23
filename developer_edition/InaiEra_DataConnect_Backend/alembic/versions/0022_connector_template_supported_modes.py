"""connector template supported modes

GAP-05: preflight needs to know which pipeline modes (cdc/snapshot/batch/
api/stream/validation/replay) a connector template actually supports, so a
pipeline built in an unsupported mode is rejected before deploy rather than
failing against the live Kafka Connect worker. See
app/services/deploy_preflight.py.

Non-nullable with a '[]' default (unlike 0005's compatibility columns,
which are nullable) — the preflight check treats an empty list as
"unrestricted", so there's no separate NULL-vs-empty distinction worth
carrying, and every existing row gets the unrestricted default outright.

Revision ID: 0022_connector_template_supported_modes
Revises: 0021_pipeline_error_handling_policy
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022_connector_template_supported_modes"
down_revision = "0021_pipeline_error_handling_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dc_connector_templates", "dc_connector_template_revisions"):
        op.add_column(
            table,
            sa.Column("supported_modes", postgresql.JSONB(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    for table in ("dc_connector_templates", "dc_connector_template_revisions"):
        op.drop_column(table, "supported_modes")
