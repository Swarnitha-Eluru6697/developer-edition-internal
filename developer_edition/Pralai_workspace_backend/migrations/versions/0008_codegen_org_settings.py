"""Org-level codegen governance settings.

WS-AI-011: whether codegen may forward the current file's content to the
external model provider was previously an unconditional client default with
no server-side policy decision behind it. This adds the settings row that
decision reads from — fail-closed (allow_external_context=False,
allowed_providers=[]) until an org admin explicitly turns it on.

Revision ID: 0008_codegen_org_settings
Revises: 0007_git_binding_status
Create Date: 2026-09-16
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_codegen_org_settings"
down_revision: Union[str, None] = "0007_git_binding_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "codegen_org_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False, unique=True),
        sa.Column("allow_external_context", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("allowed_providers", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("redact_secrets", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("codegen_org_settings")
