"""pipeline postgres readiness

Revision ID: 0012_pipeline_postgres_readiness
Revises: 0011_lineage_edge_uniqueness
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_pipeline_postgres_readiness"
down_revision = "0011_lineage_edge_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # dc_pipelines was created (0001_initial_dataconnect.py) matching an ORM
    # model that PipelineService never actually wrote through — it's missing
    # two fields the real (memory-store) pipeline row always carries, and one
    # existing column's type doesn't match what's actually stored in it.
    op.add_column(
        "dc_pipelines",
        sa.Column(
            "template_revision_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("dc_connector_template_revisions.id"),
        ),
    )
    op.add_column(
        "dc_pipelines",
        sa.Column("template_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    # TopicPolicy CRUD is still memory-store-backed (out of scope here), so
    # this FK could never resolve against an id that only exists in memory.
    op.drop_constraint("dc_pipelines_topic_policy_id_fkey", "dc_pipelines", type_="foreignkey")
    # runtime_pool_id actually holds a fixed pool-name string (e.g.
    # "connect-cdc-pool"), never a real UUID — the original UUID column type
    # would reject every value this codebase has ever written to it.
    op.alter_column(
        "dc_pipelines",
        "runtime_pool_id",
        existing_type=postgresql.UUID(as_uuid=False),
        type_=sa.String(length=255),
        postgresql_using="runtime_pool_id::text",
    )


def downgrade() -> None:
    op.alter_column(
        "dc_pipelines",
        "runtime_pool_id",
        existing_type=sa.String(length=255),
        type_=postgresql.UUID(as_uuid=False),
        postgresql_using="runtime_pool_id::uuid",
    )
    op.create_foreign_key(
        "dc_pipelines_topic_policy_id_fkey",
        "dc_pipelines",
        "dc_topic_policies",
        ["topic_policy_id"],
        ["id"],
    )
    op.drop_column("dc_pipelines", "template_snapshot")
    op.drop_column("dc_pipelines", "template_revision_id")
