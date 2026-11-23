"""template draft/publish lifecycle and immutable template versions

Revision ID: a62f91c7e4d2
Revises: c4e8a2d7b915
Create Date: 2026-10-20 10:30:00.000000

Adds an explicit authoring/publishing boundary for both Platform-provided
and Private templates. Existing rows represented live templates, so each is
backfilled as a published TemplateVersion. New edits remain in Template.flow
until explicitly published.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a62f91c7e4d2"
down_revision: Union[str, Sequence[str], None] = "c4e8a2d7b915"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Draft/publish state on the mutable authoring row.
    op.add_column("template", sa.Column("app_type", sa.String(), nullable=True))
    op.add_column("template", sa.Column("status", sa.String(), nullable=False, server_default="published"))
    op.add_column("template", sa.Column("release_managed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("template", sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("template", sa.Column("published_revision", sa.Integer(), nullable=True))
    op.add_column("template", sa.Column("current_version_id", sa.String(), nullable=True))
    op.add_column("template", sa.Column("published_at", sa.DateTime(), nullable=True))
    op.add_column("template", sa.Column("published_by", sa.String(), nullable=True))

    op.create_table(
        "templateversion",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("app_type", sa.String(), nullable=True),
        sa.Column("desc", sa.String(), nullable=False),
        sa.Column("first_party", sa.Boolean(), nullable=False),
        sa.Column("flow", postgresql.JSONB(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("vertical", sa.String(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("published_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["template.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "version", name="uq_templateversion_template_version"),
    )
    op.create_index("ix_templateversion_template_id", "templateversion", ["template_id"])
    op.create_index("ix_templateversion_published_at", "templateversion", ["published_at"])

    # Every pre-lifecycle template was already visible/installable, so preserve
    # that behavior by materializing one immutable published snapshot.
    op.execute("""
        INSERT INTO templateversion
            (id, template_id, version, name, type, app_type, "desc", first_party,
             flow, category, vertical, tags, requirements, published_at, published_by)
        SELECT
            'tplv-' || substr(md5(id || ':' || version), 1, 24),
            id, version, name, type, NULL, "desc", first_party, flow,
            category, vertical, tags, requirements,
            COALESCE(updated_at, created_at, now()),
            CASE WHEN source = 'platform' THEN 'release' ELSE owner END
        FROM template
    """)
    op.execute("""
        UPDATE template
        SET status = 'published',
            release_managed = (source = 'platform'),
            draft_revision = 1,
            published_revision = 1,
            current_version_id = 'tplv-' || substr(md5(id || ':' || version), 1, 24),
            published_at = COALESCE(updated_at, created_at, now()),
            published_by = CASE WHEN source = 'platform' THEN 'release' ELSE owner END
    """)

    # Names are unique only among active templates. Archived templates keep
    # their history but do not reserve a display name forever. The lower()
    # expression makes the database invariant match the API's case-insensitive
    # name check.
    op.drop_constraint("uq_template_name", "template", type_="unique")
    op.create_index(
        "uq_template_active_name_ci",
        "template",
        [sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("status <> 'archived'"),
    )

    op.create_check_constraint(
        "ck_template_status", "template", "status IN ('draft','published','archived')"
    )
    op.create_index("ix_template_status", "template", ["status"])
    op.create_index("ix_template_release_managed", "template", ["release_managed"])
    op.create_index("ix_template_current_version_id", "template", ["current_version_id"])
    op.create_foreign_key(
        "fk_template_current_version_id", "template", "templateversion",
        ["current_version_id"], ["id"],
    )

    # Stable provenance on agents instantiated from a template.
    op.add_column("agent", sa.Column("template_id", sa.String(), nullable=True))
    op.add_column("agent", sa.Column("template_version_id", sa.String(), nullable=True))
    op.create_index("ix_agent_template_id", "agent", ["template_id"])
    op.create_index("ix_agent_template_version_id", "agent", ["template_version_id"])
    op.create_foreign_key("fk_agent_template_id", "agent", "template", ["template_id"], ["id"])
    op.create_foreign_key(
        "fk_agent_template_version_id", "agent", "templateversion",
        ["template_version_id"], ["id"],
    )
    op.execute("""
        UPDATE agent a
        SET template_id = t.id,
            template_version_id = t.current_version_id
        FROM template t
        WHERE a.template_name IS NOT NULL AND a.template_name = t.name
    """)

    # Defaults are useful for inserts through older SQL fixtures, but status is
    # now set explicitly by application code. Keep only safe scalar defaults.
    op.alter_column("template", "status", server_default="draft")


def downgrade() -> None:
    op.drop_constraint("fk_agent_template_version_id", "agent", type_="foreignkey")
    op.drop_constraint("fk_agent_template_id", "agent", type_="foreignkey")
    op.drop_index("ix_agent_template_version_id", table_name="agent")
    op.drop_index("ix_agent_template_id", table_name="agent")
    op.drop_column("agent", "template_version_id")
    op.drop_column("agent", "template_id")

    op.drop_constraint("fk_template_current_version_id", "template", type_="foreignkey")
    op.drop_index("ix_template_current_version_id", table_name="template")
    op.drop_index("ix_template_release_managed", table_name="template")
    op.drop_index("ix_template_status", table_name="template")
    op.drop_constraint("ck_template_status", "template", type_="check")
    op.drop_index("uq_template_active_name_ci", table_name="template")
    # The old schema requires globally unique names. If an archived row reused
    # a name while this migration was active, rename only the archived copy so
    # downgrade remains possible without deleting history.
    op.execute("""
        WITH ranked AS (
            SELECT id, name, status,
                   row_number() OVER (PARTITION BY lower(name) ORDER BY (status <> 'archived') DESC, created_at, id) AS rn
            FROM template
        )
        UPDATE template t
        SET name = t.name || ' (archived ' || substr(md5(t.id), 1, 6) || ')'
        FROM ranked r
        WHERE t.id = r.id AND r.rn > 1
    """)
    op.create_unique_constraint("uq_template_name", "template", ["name"])
    for col in (
        "published_by", "published_at", "current_version_id", "published_revision",
        "draft_revision", "release_managed", "status", "app_type",
    ):
        op.drop_column("template", col)

    op.drop_index("ix_templateversion_published_at", table_name="templateversion")
    op.drop_index("ix_templateversion_template_id", table_name="templateversion")
    op.drop_table("templateversion")
