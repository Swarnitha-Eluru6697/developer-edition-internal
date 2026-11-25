"""add version tracking columns to evalrun

Revision ID: be3d74129def
Revises: e40cb55effee
Create Date: 2026-09-05 17:15:40.484297

§0.99, GAP_REPORT — the actual fix for a real, reported gap: webhook
and batch Run/Trace rows never recorded the resolved AgentVersion, and
EvalRun had no version-tracking columns at all. Each eval case also
independently resolved the agent's own current version via its own
separate execute_agent_flow call — a real, concurrent publish landing
mid-run could in principle mean different cases inside the SAME
logical eval run silently scored against genuinely different flow
content. The agent's own version is now resolved exactly once per
run_eval_set invocation and recorded here, the same one, real snapshot
every case in that run is genuinely scored against.

This migration is intentionally scoped to ONLY the new columns —
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
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = 'be3d74129def'
down_revision: Union[str, Sequence[str], None] = 'e40cb55effee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('evalrun', sa.Column('agent_version_id', sa.Integer(), nullable=True))
    op.add_column('evalrun', sa.Column('agent_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('evalrun', sa.Column('flow_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_foreign_key(None, 'evalrun', 'agentversion', ['agent_version_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(None, 'evalrun', type_='foreignkey')
    op.drop_column('evalrun', 'flow_hash')
    op.drop_column('evalrun', 'agent_version')
    op.drop_column('evalrun', 'agent_version_id')
