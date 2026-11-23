"""add idempotency support to hitlrequest

Revision ID: 10c5704936cd
Revises: 10ba1218a8eb
Create Date: 2026-09-04 17:20:00.000000

§0.74, GAP_REPORT — real, atomic-claim + idempotency support for HITL
responses: idempotency_key (the caller-supplied key that resolved this
row, if any) and resolved_result (the real, cached response payload a
repeated request carrying the same key gets replayed back, instead of
either a confusing rejection or, without the real atomic-claim fix
alongside this, genuinely resuming the same paused workflow twice).

Hand-trimmed down from Alembic's own raw autogenerate output for the
same real reasons as every prior migration this session: unrelated,
pre-existing column-type cosmetic drift, and a real, dangerous false
positive wanting to drop §0.70's own real partial unique index (no
first-class SQLModel declaration for autogenerate to compare a raw-SQL
partial index against).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '10c5704936cd'
down_revision: Union[str, Sequence[str], None] = '10ba1218a8eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('hitlrequest', sa.Column('idempotency_key', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('hitlrequest', sa.Column('resolved_result', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hitlrequest', 'resolved_result')
    op.drop_column('hitlrequest', 'idempotency_key')
