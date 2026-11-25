"""templates: surrogate id, source (platform/private), category, vertical, version

Revision ID: c4e8a2d7b915
Revises: d3f7a91c2e48
Create Date: 2026-10-20 12:00:00.000000

Until now a template was a row keyed by its display name, read-only
through the product and shipped in code. Two kinds now exist:

  * platform — shipped as package files in the release, upserted by the
    seeder on every deploy/upgrade, read-only to customers;
  * private  — saved in this data plane by a user ("Save as template"),
    stored only here, never touched by the seeder.

Existing rows are all Platform templates, so they are backfilled as
source='platform' with key = slug(name) and id = 'tpl-' || key. The
seeder (which runs at startup) then fills in `requirements` and any
changed metadata from the shipped package files.

`name` stays unique (agents.template_name and the by-name lookups rely
on it); the primary key moves to the new surrogate `id`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c4e8a2d7b915'
down_revision: Union[str, Sequence[str], None] = 'd3f7a91c2e48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('template', sa.Column('id', sa.String(), nullable=True))
    op.add_column('template', sa.Column('key', sa.String(), nullable=True))
    op.add_column('template', sa.Column('source', sa.String(), nullable=False, server_default='platform'))
    op.add_column('template', sa.Column('version', sa.String(), nullable=False, server_default='1.0.0'))
    op.add_column('template', sa.Column('category', sa.String(), nullable=True))
    op.add_column('template', sa.Column('vertical', sa.String(), nullable=True))
    op.add_column('template', sa.Column('tags', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column('template', sa.Column('requirements', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column('template', sa.Column('owner', sa.String(), nullable=True))
    op.add_column('template', sa.Column('hidden', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('template', sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()))
    op.add_column('template', sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()))

    # Backfill: every pre-existing row is a shipped (Platform) template.
    op.execute("""
        UPDATE template SET key = trim(both '-' from lower(regexp_replace(name, '[^a-zA-Z0-9]+', '-', 'g')))
    """)
    op.execute("UPDATE template SET id = 'tpl-' || key")
    op.execute("""
        UPDATE template SET category = CASE name
            WHEN 'Analysis Agent' THEN 'Data'
            WHEN 'Engineer Agent' THEN 'Data'
            WHEN 'ERD Designer Agent' THEN 'Data'
            WHEN 'Scientist Agent' THEN 'Data'
            WHEN 'Conversational & Knowledge' THEN 'Service'
            WHEN 'Task-Automation' THEN 'Operations'
            ELSE 'General' END
    """)

    op.alter_column('template', 'id', nullable=False)
    op.alter_column('template', 'key', nullable=False)
    op.drop_constraint('template_pkey', 'template', type_='primary')
    op.create_primary_key('template_pkey', 'template', ['id'])
    op.create_unique_constraint('uq_template_name', 'template', ['name'])
    op.create_unique_constraint('uq_template_key', 'template', ['key'])
    op.create_check_constraint('ck_template_source', 'template', "source IN ('platform','private')")
    op.create_index('ix_template_source', 'template', ['source'])
    op.create_index('ix_template_category', 'template', ['category'])
    op.create_index('ix_template_vertical', 'template', ['vertical'])


def downgrade() -> None:
    # Private templates have no representation in the old schema (it had
    # no owner and no provenance), so they cannot be carried back.
    op.execute("DELETE FROM template WHERE source = 'private'")
    op.drop_index('ix_template_vertical', table_name='template')
    op.drop_index('ix_template_category', table_name='template')
    op.drop_index('ix_template_source', table_name='template')
    op.drop_constraint('ck_template_source', 'template', type_='check')
    op.drop_constraint('uq_template_key', 'template', type_='unique')
    op.drop_constraint('uq_template_name', 'template', type_='unique')
    op.drop_constraint('template_pkey', 'template', type_='primary')
    op.create_primary_key('template_pkey', 'template', ['name'])
    for col in ('updated_at', 'created_at', 'hidden', 'owner', 'requirements', 'tags',
                'vertical', 'category', 'version', 'source', 'key', 'id'):
        op.drop_column('template', col)
