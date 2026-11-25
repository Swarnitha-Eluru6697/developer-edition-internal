"""add draft_revision to agent for optimistic locking

Revision ID: 10ba1218a8eb
Revises: 2c68783543ed
Create Date: 2026-09-04 15:56:10.597308

§0.71, GAP_REPORT — a real, reported gap: every Builder node/edge
mutation did a genuine read-full-flow / modify-local-copy / write-
full-flow round trip with no revision check at all, so two concurrent
Builder tabs editing the same agent could silently overwrite each
other's real changes with no detection. This adds the real revision
counter every mutating endpoint now checks against (see
routers/agents.py's own _save_flow and its real, optional If-Match
header handling).

A real, necessary server_default='1' — this table already has real,
existing rows; adding a NOT NULL column without one would fail against
them. Hand-trimmed down from Alembic's own raw autogenerate output,
which also picked up: unrelated, pre-existing column-type cosmetic
drift (TEXT vs AutoString) already deliberately left out of the prior
migration for the same real reason; and a real, dangerous false
positive — autogenerate wanted to DROP the real partial unique index
this session's own §0.70 fix added (uq_agentversion_one_current_per_agent),
because that index was created directly via raw SQL (a partial index
has no first-class SQLModel/Python declaration to compare against) and
autogenerate's own diff has no way to know it's intentional. Left out
here entirely — dropping it would have silently reintroduced the exact
real race §0.70 closed.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '10ba1218a8eb'
down_revision: Union[str, Sequence[str], None] = '2c68783543ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('agent', sa.Column('draft_revision', sa.Integer(), nullable=False, server_default='1'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('agent', 'draft_revision')
