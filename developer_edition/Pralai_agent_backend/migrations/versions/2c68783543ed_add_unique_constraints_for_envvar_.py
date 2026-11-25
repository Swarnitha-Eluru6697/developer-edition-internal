"""add unique constraints for envvar, agentmemory, agentversion, kb metadata field

Revision ID: 2c68783543ed
Revises: 1e72082664c9
Create Date: 2026-09-04 15:26:35.077773

§0.70, GAP_REPORT — a real, reported gap: several application-level
invariants (EnvVar's (agent_id, key), AgentMemory's (scope_key, key),
AgentVersion's (agent_id, version), KnowledgeMetadataField's (kb_id,
name), and "exactly one is_current=True AgentVersion per agent") were
only ever checked in Python via SELECT-then-INSERT, never enforced by
Postgres itself — two genuinely concurrent requests could both pass
the SELECT check before either committed, producing real duplicate
rows. This migration hand-edited down from Alembic's own raw
autogenerate output, which also picked up unrelated, pre-existing
column-type cosmetic drift (TEXT vs AutoString on a few columns added
by earlier ad-hoc ALTER TABLE statements, before this project had real
migrations) — left out of this migration deliberately, since mixing an
unrelated cosmetic cleanup into a constraint-adding migration is worse
than a separate, later migration for that on its own.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2c68783543ed'
down_revision: Union[str, Sequence[str], None] = '1e72082664c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint('uq_agentmemory_scope_key_key', 'agentmemory', ['scope_key', 'key'])
    op.create_unique_constraint('uq_agentversion_agent_id_version', 'agentversion', ['agent_id', 'version'])
    op.create_unique_constraint('uq_envvar_agent_id_key', 'envvar', ['agent_id', 'key'])
    op.create_unique_constraint('uq_kmf_kb_id_name', 'knowledgemetadatafield', ['kb_id', 'name'])
    # A real partial unique index — "exactly one is_current=True row
    # per agent_id" is not a plain-column uniqueness a SQLModel
    # UniqueConstraint can express (see AgentVersion's own docstring);
    # Postgres's own real WHERE clause on a unique index is what
    # actually enforces this, checked only among is_current=True rows,
    # not across every historical version.
    op.execute(
        'CREATE UNIQUE INDEX uq_agentversion_one_current_per_agent '
        'ON agentversion (agent_id) WHERE is_current = true;'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP INDEX IF EXISTS uq_agentversion_one_current_per_agent;')
    op.drop_constraint('uq_kmf_kb_id_name', 'knowledgemetadatafield', type_='unique')
    op.drop_constraint('uq_envvar_agent_id_key', 'envvar', type_='unique')
    op.drop_constraint('uq_agentversion_agent_id_version', 'agentversion', type_='unique')
    op.drop_constraint('uq_agentmemory_scope_key_key', 'agentmemory', type_='unique')
