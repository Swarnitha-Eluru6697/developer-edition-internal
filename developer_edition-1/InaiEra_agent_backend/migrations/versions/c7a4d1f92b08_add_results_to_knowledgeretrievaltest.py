"""add results snapshot to knowledgeretrievaltest

Revision ID: c7a4d1f92b08
Revises: 8abfb57664d5
Create Date: 2026-09-07 23:05:00.000000

The Retrieval Testing "Records" table listed past queries but stored
only their result COUNT, so clicking a record had nothing to show. This
column persists the retrieved chunks as they actually scored at the time
of the run.

Deliberately a stored snapshot rather than re-running the query on
click: a re-run would genuinely return something different once
documents are added, disabled or deleted, or once the Settings tab's
retrieval method / weights / score threshold change — and presenting
that as "the results of the run from last Tuesday" would be a lie.

Nullable with no backfill, on purpose. Rows written before this column
existed have no results to recover; nothing kept them. The UI tells that
case apart from "ran and matched nothing" using the existing
result_count, and offers to re-run those explicitly instead of
pretending it has their history.

Scoped to only this column, matching the precedent of every prior
migration here: autogenerate also reports pre-existing, unrelated
schema drift (TEXT vs AutoString, nullability) that is not this fix.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7a4d1f92b08'
down_revision: Union[str, Sequence[str], None] = '8abfb57664d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('knowledgeretrievaltest',
                  sa.Column('results', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('knowledgeretrievaltest', 'results')
