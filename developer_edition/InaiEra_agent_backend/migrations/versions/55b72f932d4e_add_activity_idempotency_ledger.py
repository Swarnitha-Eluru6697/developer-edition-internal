"""add activity idempotency ledger

Revision ID: 55b72f932d4e
Revises: 10c5704936cd
Create Date: 2026-09-04 18:24:39.272693

§0.76, GAP_REPORT — a real, general-purpose idempotency ledger for
Temporal Activity side effects. Temporal retries a failed Activity
automatically (at-least-once execution, not exactly-once), so any
Activity that performs a genuinely non-idempotent side effect (this
platform's own real, concrete example: a chat-history APPEND) needs a
real way to recognize "this exact logical step already happened" and
replay the cached result instead of re-applying it. See
ActivityIdempotency's own model docstring and temporal_workflow.py's
own `_idempotency_key_for` for the full real design.

Hand-trimmed down from Alembic's own raw autogenerate output for the
same real reasons as every prior migration this session: unrelated,
pre-existing column-type cosmetic drift, and a real, dangerous false
positive wanting to drop the real partial unique index §0.70's own
fix added (no first-class SQLModel declaration for autogenerate to
compare a raw-SQL partial index against).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '55b72f932d4e'
down_revision: Union[str, Sequence[str], None] = '10c5704936cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'activityidempotency',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('activityidempotency')
