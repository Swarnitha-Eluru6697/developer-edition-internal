"""add subagent dependency manifest to agentversion

Revision ID: f11a036b2e80
Revises: 1bd0f35eeb21
Create Date: 2026-09-05 22:33:51.040814

§0.104, GAP_REPORT — the actual fix for a real, reported reproducibility
gap: a Sub-Agent node used to always resolve its target's CURRENT
published AgentVersion at execution time — so an unchanged, already-
published parent could silently invoke a materially different child
tomorrow than it did today, the moment the child was independently
republished. This column freezes, at THIS agent's own publish time,
exactly which AgentVersion.id each Sub-Agent node in its flow resolved
to at that moment — {"subagents": [{node_id, agent_id,
agent_version_id, version}, ...]} — so a later republish of the CHILD
can never silently change what an unchanged, already-published PARENT
actually invokes.

This migration is intentionally scoped to ONLY the new column —
`alembic revision --autogenerate` also detected several real,
PRE-EXISTING, unrelated schema-drift items (cosmetic TEXT() vs.
AutoString() type differences, nullable-constraint differences, a
dropped partial index) that have nothing to do with this fix and were
deliberately left out, matching this session's own established
precedent for every prior migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f11a036b2e80'
down_revision: Union[str, Sequence[str], None] = '1bd0f35eeb21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agentversion', sa.Column('subagent_manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agentversion', 'subagent_manifest')
