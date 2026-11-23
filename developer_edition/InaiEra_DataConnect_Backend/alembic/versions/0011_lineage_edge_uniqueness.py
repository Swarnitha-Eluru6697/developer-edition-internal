"""lineage edge uniqueness

Revision ID: 0011_lineage_edge_uniqueness
Revises: 0010_schema_drift_lifecycle
Create Date: 2026-09-15
"""

from alembic import op

revision = "0011_lineage_edge_uniqueness"
down_revision = "0010_schema_drift_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_dc_lineage_edges_identity",
        "dc_lineage_edges",
        ["org_id", "from_asset_id", "to_asset_id", "edge_type", "pipeline_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_dc_lineage_edges_identity", "dc_lineage_edges", type_="unique")
