"""Replace mutable dq_render_cache with immutable dq_artifact_versions (DQ-ART-004).

dq_render_cache held exactly one row per binding, UPDATEd in place on every
re-render — including a request-specific forced render with one-off DDL
overrides, which overwrote that row for every subsequent request too. Its
rules_hash also never covered target_table, enforcement_mode, the DDL
overrides, or the renderer/engine versions, so a "cache hit" wasn't a real
guarantee the stored artifact matched the current inputs.

dq_render_cache held nothing but regenerable cache data (the docstring on
the old RenderCache model said so explicitly), so this migration drops it
outright rather than migrating rows — the next GET /render for each binding
renders and persists its first ArtifactVersion from scratch.

Revision ID: 0002_artifact_versions
Revises: 0001_initial
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_artifact_versions"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("dq_render_cache")

    op.create_table(
        "dq_artifact_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("binding_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dq_job_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("renderer_version", sa.String(20), nullable=False),
        sa.Column("engine_version", sa.String(50), nullable=False),
        sa.Column("dbt_artifact", sa.Text),
        sa.Column("flinksql_artifact", sa.Text),
        sa.Column("gx_artifact", postgresql.JSONB),
        sa.Column("soda_artifact", sa.Text),
        sa.Column("validation_diagnostics", postgresql.JSONB),
        sa.Column("rendered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # No unique constraint on (binding_id, content_hash): a forced
        # re-render intentionally may insert another row with an unchanged
        # hash (its own definitive, timestamped version for that specific
        # request/apply) — this table is an append-only version history,
        # not a dedup cache.
    )
    op.create_index("ix_dq_artifact_binding_hash", "dq_artifact_versions", ["binding_id", "content_hash"])


def downgrade() -> None:
    op.drop_table("dq_artifact_versions")

    op.create_table(
        "dq_render_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("binding_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dq_job_bindings.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("dbt_artifact", sa.Text),
        sa.Column("flinksql_artifact", sa.Text),
        sa.Column("gx_artifact", postgresql.JSONB),
        sa.Column("soda_artifact", sa.Text),
        sa.Column("rendered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dq_cache_binding", "dq_render_cache", ["binding_id"])
