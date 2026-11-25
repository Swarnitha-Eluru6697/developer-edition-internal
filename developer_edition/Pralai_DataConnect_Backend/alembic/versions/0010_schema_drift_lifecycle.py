"""schema drift approval durable lifecycle

Revision ID: 0010_schema_drift_lifecycle
Revises: 0009_schema_inference_runs
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010_schema_drift_lifecycle"
down_revision = "0009_schema_inference_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # schema_version_id/pipeline_id referenced tables whose CRUD is still
    # memory-store-backed (SchemaVersion, Pipeline) and so are never actually
    # populated in Postgres — these FKs could never resolve for a real row,
    # and this table is only now being wired up to persist for real.
    op.drop_constraint(
        "dc_schema_drift_approvals_schema_version_id_fkey",
        "dc_schema_drift_approvals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "dc_schema_drift_approvals_pipeline_id_fkey",
        "dc_schema_drift_approvals",
        type_="foreignkey",
    )
    op.create_index(
        "ix_dc_schema_drift_approvals_schema_version_id",
        "dc_schema_drift_approvals",
        ["schema_version_id"],
    )
    op.create_index(
        "ix_dc_schema_drift_approvals_pipeline_id",
        "dc_schema_drift_approvals",
        ["pipeline_id"],
    )
    op.add_column("dc_schema_drift_approvals", sa.Column("run_id", sa.String(length=255)))
    op.add_column("dc_schema_drift_approvals", sa.Column("detected_fingerprint", sa.String(length=64)))
    op.add_column("dc_schema_drift_approvals", sa.Column("current_fingerprint", sa.String(length=64)))
    op.add_column(
        "dc_schema_drift_approvals",
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("dc_schema_drift_approvals", sa.Column("decision", sa.String(length=50)))
    op.add_column(
        "dc_schema_drift_approvals",
        sa.Column("resulting_schema_version_id", postgresql.UUID(as_uuid=False)),
    )


def downgrade() -> None:
    op.drop_column("dc_schema_drift_approvals", "resulting_schema_version_id")
    op.drop_column("dc_schema_drift_approvals", "decision")
    op.drop_column("dc_schema_drift_approvals", "detected_at")
    op.drop_column("dc_schema_drift_approvals", "current_fingerprint")
    op.drop_column("dc_schema_drift_approvals", "detected_fingerprint")
    op.drop_column("dc_schema_drift_approvals", "run_id")
    op.drop_index("ix_dc_schema_drift_approvals_pipeline_id", table_name="dc_schema_drift_approvals")
    op.drop_index("ix_dc_schema_drift_approvals_schema_version_id", table_name="dc_schema_drift_approvals")
    op.create_foreign_key(
        "dc_schema_drift_approvals_pipeline_id_fkey",
        "dc_schema_drift_approvals",
        "dc_pipelines",
        ["pipeline_id"],
        ["id"],
    )
    op.create_foreign_key(
        "dc_schema_drift_approvals_schema_version_id_fkey",
        "dc_schema_drift_approvals",
        "dc_schema_versions",
        ["schema_version_id"],
        ["id"],
    )
