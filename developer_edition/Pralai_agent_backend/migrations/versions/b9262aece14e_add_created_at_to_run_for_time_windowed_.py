"""add created_at to run for time-windowed monitoring metrics

Revision ID: b9262aece14e
Revises: 55b72f932d4e
Create Date: 2026-09-04 18:50:00.000000

§0.77, GAP_REPORT — Run's existing `ts` field is only ever a
time-of-day STRING ("%H:%M:%S", no date component), so there was
never a real way to determine which real day a given Run happened on
- exactly what routers/monitoring.py's own real "runsToday" (and any
other real time-windowed metric) needs. Nullable, no server default:
a real, existing row predating this fix has no real, accurate value
to backfill (there's no way to recover a real date that was never
stored) - left NULL rather than fabricating one.

Hand-trimmed down from Alembic's own raw autogenerate output for the
same real reasons as every prior migration this session.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b9262aece14e'
down_revision: Union[str, Sequence[str], None] = '55b72f932d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('run', sa.Column('created_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('run', 'created_at')
