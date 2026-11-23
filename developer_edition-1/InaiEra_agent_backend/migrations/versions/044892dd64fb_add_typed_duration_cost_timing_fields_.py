"""add typed duration cost timing fields to run and trace

Revision ID: 044892dd64fb
Revises: b030f58355d9
Create Date: 2026-09-04 20:02:42.814812

§0.79, GAP_REPORT — real, typed metrics for Run/Trace, replacing
string-only duration/cost/ts for real aggregation, retention, and SLO
queries. duration_ms (BIGINT-equivalent Integer), started_at/
completed_at (TIMESTAMPTZ) are new, nullable columns on both tables —
a real, existing row predating this fix has no real, accurate value
to backfill, left NULL rather than fabricated. run.cost_usd (added
§0.78 as a plain Float) is altered to NUMERIC(12, 6) — the correct
real type for currency, exact base-10 arithmetic instead of binary
floating point. trace.cost_usd is a new NUMERIC(12, 6) column, added
for the same real reason.

Hand-trimmed down from Alembic's own raw autogenerate output for the
same real reasons as every prior migration this session: unrelated,
pre-existing column-type cosmetic drift, and a real, dangerous false
positive wanting to drop the real partial unique index §0.70 added.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '044892dd64fb'
down_revision: Union[str, Sequence[str], None] = 'b030f58355d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('run', sa.Column('duration_ms', sa.Integer(), nullable=True))
    op.add_column('run', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('run', sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        'run', 'cost_usd',
        existing_type=sa.DOUBLE_PRECISION(precision=53),
        type_=sa.Numeric(precision=12, scale=6),
        nullable=True,
        existing_server_default=sa.text("'0'::double precision"),
        postgresql_using='cost_usd::numeric(12,6)',
    )
    op.add_column('trace', sa.Column('duration_ms', sa.Integer(), nullable=True))
    op.add_column('trace', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trace', sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trace', sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trace', 'cost_usd')
    op.drop_column('trace', 'completed_at')
    op.drop_column('trace', 'started_at')
    op.drop_column('trace', 'duration_ms')
    op.alter_column(
        'run', 'cost_usd',
        existing_type=sa.Numeric(precision=12, scale=6),
        type_=sa.DOUBLE_PRECISION(precision=53),
        nullable=False,
        existing_server_default=sa.text("'0'::double precision"),
        postgresql_using='cost_usd::double precision',
    )
    op.drop_column('run', 'completed_at')
    op.drop_column('run', 'started_at')
    op.drop_column('run', 'duration_ms')
