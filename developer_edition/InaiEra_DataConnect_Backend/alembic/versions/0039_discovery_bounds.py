"""discovery runs: truncation flag, and a readable paging order for their objects

GAP-10: a run can now be marked `truncated` when the source exposed more objects than the configured cap, and its
objects are paged in (schema, name, id) order instead of by random id — the index below backs that key.

Revision ID: 0039_discovery_bounds
Revises: 0038_source_error_policies
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0039_discovery_bounds"
down_revision = "0038_source_error_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_discovery_runs",
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.drop_index("ix_dc_discovery_objects_run_id", table_name="dc_discovery_objects")
    op.create_index(
        "ix_dc_discovery_objects_run_order", "dc_discovery_objects",
        ["discovery_run_id", sa.text("coalesce(schema_name, '')"), "name", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dc_discovery_objects_run_order", table_name="dc_discovery_objects")
    op.create_index("ix_dc_discovery_objects_run_id", "dc_discovery_objects", ["discovery_run_id", "id"])
    op.drop_column("dc_discovery_runs", "truncated")
