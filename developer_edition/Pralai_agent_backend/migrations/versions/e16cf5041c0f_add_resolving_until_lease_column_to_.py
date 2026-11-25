"""add resolving_until lease column to hitlrequest

Revision ID: e16cf5041c0f
Revises: f11a036b2e80
Create Date: 2026-09-05 23:45:00.000000

§0.106, GAP_REPORT — the actual fix for a real, reported gap: a
HitlRequest's own atomic claim (pending -> resolving) had no recovery
mechanism if the subsequent resume attempt then genuinely failed
(Temporal temporarily unreachable, a workflow-signaling failure, a
worker outage, a genuine resume exception) — the row could be left
stuck at status="resolving" forever, with no way for a legitimate
retry to ever succeed again. This column is the real, bounded lease
respond_to_hitl now sets at claim time; reconcile_stale_hitl_requests
(routers/execution.py) is the real, periodic reconciler that rolls any
row whose lease has genuinely expired back to "pending".

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

# revision identifiers, used by Alembic.
revision: str = 'e16cf5041c0f'
down_revision: Union[str, Sequence[str], None] = 'f11a036b2e80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('hitlrequest', sa.Column('resolving_until', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hitlrequest', 'resolving_until')
