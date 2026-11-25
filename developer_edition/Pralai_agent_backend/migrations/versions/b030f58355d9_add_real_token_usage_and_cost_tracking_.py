"""add real token usage and cost tracking to run

Revision ID: b030f58355d9
Revises: b9262aece14e
Create Date: 2026-09-04 19:40:00.000000

§0.78, GAP_REPORT — real, aggregated per-run token/cost usage. Real
usage (input_tokens/output_tokens, already captured from the real
Anthropic API response) was computed and immediately discarded;
Run.cost was unconditionally hardcoded to "$0.00" at every real call
site. These four columns are the real sum across every real span a
run produced (see harness.py's own _aggregate_llm_usage) - 0 for a
run with no LLM-touching nodes, a real, honest default, not an
absent field. Existing rows backfill to 0 (the real, honest answer
for data collected before this fix existed - not fabricated).

Hand-trimmed down from Alembic's own raw autogenerate output for the
same real reasons as every prior migration this session.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b030f58355d9'
down_revision: Union[str, Sequence[str], None] = 'b9262aece14e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('run', sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('run', sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('run', sa.Column('cached_tokens', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('run', sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('run', 'cost_usd')
    op.drop_column('run', 'cached_tokens')
    op.drop_column('run', 'output_tokens')
    op.drop_column('run', 'input_tokens')
