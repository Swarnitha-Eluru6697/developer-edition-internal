"""add first-class mcp credential columns to agent

Revision ID: 2003baed6edb
Revises: be3d74129def
Create Date: 2026-09-05 18:12:22.420623

§0.101, GAP_REPORT — the actual fix for a real, reported vulnerability:
MCP tokens used to live inside Agent.features["mcp"] — the same real,
live dict AgentVersion.features snapshots wholesale at publish time.
Restoring an OLD version copied that OLD snapshot's own token back
into the LIVE agent, genuinely resurrecting a credential that had
already been rotated/revoked since. These are now real, top-level
Agent columns instead — restore_agent_version/_resolve_and_publish
only ever read/write flow/app_type/features, never these, so a
version restore genuinely cannot touch this credential at all anymore.

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
revision: str = '2003baed6edb'
down_revision: Union[str, Sequence[str], None] = 'be3d74129def'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agent', sa.Column('mcp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('agent', sa.Column('mcp_token_encrypted', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('agent', sa.Column('mcp_token_created_at', sa.DateTime(), nullable=True))
    op.add_column('agent', sa.Column('mcp_token_rotated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agent', 'mcp_token_rotated_at')
    op.drop_column('agent', 'mcp_token_created_at')
    op.drop_column('agent', 'mcp_token_encrypted')
    op.drop_column('agent', 'mcp_enabled')
