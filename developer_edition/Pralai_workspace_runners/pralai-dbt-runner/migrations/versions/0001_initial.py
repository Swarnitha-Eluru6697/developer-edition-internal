"""Initial schema — projects, runs, run_events, lineage_edges, git_credentials.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-30

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True)),
        sa.Column("registered_by_user_id", sa.String(255)),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("git_url", sa.Text, nullable=False),
        sa.Column("git_ref", sa.String(255), nullable=False, server_default="main"),
        sa.Column("engine", sa.String(20), nullable=False, server_default="trino"),
        sa.Column("schedule_cron", sa.String(120)),
        sa.Column("schedule_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_synced_sha", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("engine IN ('trino', 'pg_lake')", name="ck_project_engine"),
        sa.UniqueConstraint("org_id", "git_url", "git_ref", name="uq_project_repo_ref"),
    )
    op.create_index("ix_projects_org", "projects", ["org_id"])
    op.create_index("ix_projects_workspace", "projects", ["workspace_id"])

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("triggered_by_user_id", sa.String(255)),
        sa.Column("git_sha", sa.String(40)),
        sa.Column("select", sa.String(500)),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("models_total", sa.Integer),
        sa.Column("models_succeeded", sa.Integer),
        sa.Column("models_failed", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_run_status",
        ),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled', 'api')", name="ck_run_trigger"),
    )
    op.create_index("ix_runs_project_created", "runs", ["project_id", "created_at"])
    op.create_index(
        "ix_runs_pending", "runs", ["status"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("node_id", sa.String(255)),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_run_events_run", "run_events", ["run_id", "created_at"])

    op.create_table(
        "lineage_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_table", sa.String(500), nullable=False),
        sa.Column("target_table", sa.String(500), nullable=False),
        sa.Column("relationship_type", sa.String(20), nullable=False, server_default="ref"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_lineage_run", "lineage_edges", ["run_id"])
    op.create_index("ix_lineage_project", "lineage_edges", ["project_id"])

    op.create_table(
        "git_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("instance_url", sa.String(500)),
        sa.Column("vault_path", sa.String(500), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "provider", "instance_url", name="uq_dbtrunner_git_cred"),
    )


def downgrade() -> None:
    for tbl in ("git_credentials", "lineage_edges", "run_events", "runs", "projects"):
        op.drop_table(tbl)
