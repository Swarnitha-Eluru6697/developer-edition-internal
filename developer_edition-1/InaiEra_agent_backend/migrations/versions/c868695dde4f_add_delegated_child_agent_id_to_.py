"""add delegated child agent id to hitlrequest

Revision ID: c868695dde4f
Revises: 9307453e7348
Create Date: 2026-09-04 21:40:00.000000

§0.83, GAP_REPORT — real, nested-durable Sub-Agent support. A new,
nullable HitlRequest.delegated_child_agent_id column, only ever set
when a nested Sub-Agent's own target genuinely paused on its own real,
separate Temporal workflow - see HitlPause's own docstring
(harness.py) and HitlRequest's own updated docstring (models.py) for
the full real design.

Hand-trimmed down from Alembic's own raw autogenerate output for the
same real reasons as every prior migration this session: unrelated,
pre-existing column-type cosmetic drift, and a real, dangerous false
positive wanting to drop the real partial unique index §0.70 added.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c868695dde4f'
down_revision: Union[str, Sequence[str], None] = '9307453e7348'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('hitlrequest', sa.Column('delegated_child_agent_id', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hitlrequest', 'delegated_child_agent_id')
