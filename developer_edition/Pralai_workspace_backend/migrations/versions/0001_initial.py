"""Initial schema — users, workspaces, files, git, sessions, executions, audit.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-12

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
    # Required for gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ---- users ----
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("external_id", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )

    # ---- workspaces ----
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("workspace_type", sa.String(20), nullable=False, server_default="personal"),
        sa.Column("storage_prefix", sa.String(500), nullable=False),
        sa.Column("default_warehouse", sa.String(255)),
        sa.Column("default_kernel_image", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_id", "name", name="uq_workspace_owner_name"),
    )
    op.create_index("ix_workspaces_owner", "workspaces", ["owner_id"])

    # ---- workspace_files ----
    op.create_table(
        "workspace_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False, server_default="main"),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64)),
        sa.Column("last_committed_sha", sa.String(40)),
        sa.Column("is_dirty", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "path", "branch", name="uq_file_path_branch"),
        sa.CheckConstraint(
            "language IN ('sql', 'ipynb', 'py', 'md', 'yaml', 'json', 'other')",
            name="ck_file_language",
        ),
    )
    op.create_index("ix_files_workspace_lang", "workspace_files",
                    ["workspace_id", "language"])

    # ---- git_credentials ----
    op.create_table(
        "git_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_user_id", sa.String(255), nullable=False),
        sa.Column("provider_username", sa.String(255)),
        sa.Column("instance_url", sa.String(500)),
        sa.Column("vault_path", sa.String(500), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "provider", "instance_url", name="uq_git_cred"),
    )
    op.create_index("ix_git_creds_user", "git_credentials", ["user_id"])

    # ---- git_bindings ----
    op.create_table(
        "git_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("repo_url", sa.Text, nullable=False),
        sa.Column("repo_full_name", sa.String(500)),
        sa.Column("default_branch", sa.String(255), nullable=False, server_default="main"),
        sa.Column("current_branch", sa.String(255), nullable=False, server_default="main"),
        sa.Column("last_synced_sha", sa.String(40)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )

    # ---- sessions ----
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("runtime_metadata", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sessions_user_active", "sessions", ["user_id"],
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index("ix_sessions_idle_cleanup", "sessions", ["last_activity_at"],
                    postgresql_where=sa.text("status = 'active'"))

    # ---- executions ----
    op.create_table(
        "executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("workspace_files.id", ondelete="SET NULL")),
        sa.Column("cell_id", sa.String(64)),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("code_text", sa.Text, nullable=False),
        sa.Column("code_sha256", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("row_count", sa.Integer),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("language IN ('sql', 'ipynb_cell')", name="ck_execution_language"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'timeout')",
            name="ck_execution_status",
        ),
    )
    op.create_index("ix_executions_user_created", "executions",
                    ["user_id", "created_at"])
    op.create_index("ix_executions_session", "executions", ["session_id"])
    op.create_index("ix_executions_pending", "executions", ["status"],
                    postgresql_where=sa.text("status IN ('pending', 'running')"))

    # ---- execution_outputs ----
    op.create_table(
        "execution_outputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("output_type", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("execution_id", "chunk_index", name="uq_output_chunk"),
    )
    op.create_index("ix_outputs_expiry", "execution_outputs", ["expires_at"])

    # ---- audit_logs ----
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(50)),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True)),
        sa.Column("ip_address", postgresql.INET),
        sa.Column("user_agent", sa.Text),
        sa.Column("metadata", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_user_time", "audit_logs", ["user_id", "created_at"])
    op.create_index("ix_audit_action", "audit_logs", ["action", "created_at"])
    op.create_index("ix_audit_resource", "audit_logs", ["resource_type", "resource_id"])


def downgrade() -> None:
    for tbl in (
        "audit_logs", "execution_outputs", "executions", "sessions",
        "git_bindings", "git_credentials", "workspace_files",
        "workspaces", "users",
    ):
        op.drop_table(tbl)
