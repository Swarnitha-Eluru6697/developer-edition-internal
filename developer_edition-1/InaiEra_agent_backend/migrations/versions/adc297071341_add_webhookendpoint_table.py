"""add webhookendpoint table

Revision ID: adc297071341
Revises: efca80a4fdb0
Create Date: 2026-09-05 16:21:31.577805

§0.97, GAP_REPORT — the actual, first-class webhook registration the
report asked for. Webhook EXECUTION already correctly ran a published
agent's own frozen AgentVersion.flow, but webhook DISCOVERY scanned
the live, mutable Agent.flow directly for a plain string match — a
Builder editing a trigger node's own webhook_token on the live draft,
with no new publish, silently changed which token production actually
responded to, even though the flow that token invoked stayed genuinely
frozen. It was also an unindexed O(agents x nodes) full scan on every
single incoming webhook request.

This table is the one, real, authoritative source for token
resolution — never a JSON flow scan again. token_hash (a real sha256
digest, not the raw token) is what's compared against an incoming
request; see WebhookEndpoint's own model docstring for the full real
design.

This migration is intentionally scoped to ONLY the new table —
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
revision: str = 'adc297071341'
down_revision: Union[str, Sequence[str], None] = 'efca80a4fdb0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'webhookendpoint',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('agent_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('agent_version_id', sa.Integer(), nullable=False),
        sa.Column('trigger_node_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['agent_id'], ['agent.id'], ),
        sa.ForeignKeyConstraint(['agent_version_id'], ['agentversion.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_webhookendpoint_agent_id'), 'webhookendpoint', ['agent_id'], unique=False)
    op.create_index(op.f('ix_webhookendpoint_token_hash'), 'webhookendpoint', ['token_hash'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_webhookendpoint_token_hash'), table_name='webhookendpoint')
    op.drop_index(op.f('ix_webhookendpoint_agent_id'), table_name='webhookendpoint')
    op.drop_table('webhookendpoint')
