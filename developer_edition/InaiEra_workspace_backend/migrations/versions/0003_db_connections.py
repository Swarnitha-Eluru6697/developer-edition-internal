"""Add database_connections table and relax language constraint.

Revision ID: 0003_db_connections
Revises: 0002_codegen_chat
Create Date: 2026-07-02
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_db_connections"
down_revision: Union[str, None] = "0002_codegen_chat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "database_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("connection_type", sa.String(20), nullable=False),
        sa.Column("host", sa.String(500)),
        sa.Column("port", sa.Integer),
        sa.Column("database_name", sa.String(255)),
        sa.Column("catalog_name", sa.String(255)),
        sa.Column("vault_secret_path", sa.String(500)),
        sa.Column("use_tls", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("extra_properties", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "connection_type IN ('trino','pg_lake','postgres','flink','other')",
            name="ck_dbconn_type",
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_dbconn_org_name"),
    )
    op.create_index("ix_dbconn_org", "database_connections", ["org_id"])
    op.create_index("ix_dbconn_user", "database_connections", ["created_by_user_id"])

    # Relax language constraint on workspace_files to allow all phase 2+ types
    op.drop_constraint("ck_file_language", "workspace_files")
    op.create_check_constraint(
        "ck_file_language",
        "workspace_files",
        "language IN ('sql','ipynb','py','md','yaml','json','other')",
    )


def downgrade() -> None:
    op.drop_table("database_connections")
    op.drop_constraint("ck_file_language", "workspace_files")
    op.create_check_constraint(
        "ck_file_language",
        "workspace_files",
        "language IN ('sql','ipynb','py','md','yaml','json','other')",
    )
