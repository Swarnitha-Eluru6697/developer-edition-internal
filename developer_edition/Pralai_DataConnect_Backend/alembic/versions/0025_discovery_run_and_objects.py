"""discovery run and objects

GAP-10: connection discovery's result (potentially tens of thousands of
schemas/tables for a large source) no longer lives entirely inside
Operation.result. dc_discovery_runs is the small, pollable summary;
dc_discovery_objects holds one row per discovered object, paged through by
GET /discovery/{id}/objects instead of read back as one JSON blob.

Revision ID: 0025_discovery_run_and_objects
Revises: 0024_pipeline_schema_introspection_status
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025_discovery_run_and_objects"
down_revision = "0024_pipeline_schema_introspection_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dc_discovery_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("org_id", sa.String(length=100), sa.ForeignKey("dc_organizations.org_id"), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("object_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("continuation", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dc_discovery_runs_org_id", "dc_discovery_runs", ["org_id"])
    op.create_index("ix_dc_discovery_runs_connection_id", "dc_discovery_runs", ["connection_id"])

    op.create_table(
        "dc_discovery_objects",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("discovery_run_id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=100), nullable=False),
        sa.Column("object_type", sa.String(length=20), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
    )
    op.create_index("ix_dc_discovery_objects_discovery_run_id", "dc_discovery_objects", ["discovery_run_id"])
    op.create_index("ix_dc_discovery_objects_org_id", "dc_discovery_objects", ["org_id"])
    op.create_index("ix_dc_discovery_objects_run_id", "dc_discovery_objects", ["discovery_run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_dc_discovery_objects_run_id", table_name="dc_discovery_objects")
    op.drop_index("ix_dc_discovery_objects_org_id", table_name="dc_discovery_objects")
    op.drop_index("ix_dc_discovery_objects_discovery_run_id", table_name="dc_discovery_objects")
    op.drop_table("dc_discovery_objects")

    op.drop_index("ix_dc_discovery_runs_connection_id", table_name="dc_discovery_runs")
    op.drop_index("ix_dc_discovery_runs_org_id", table_name="dc_discovery_runs")
    op.drop_table("dc_discovery_runs")
