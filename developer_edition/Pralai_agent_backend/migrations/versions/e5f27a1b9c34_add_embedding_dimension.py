"""add embedding_dimension to providermodel

Revision ID: e5f27a1b9c34
Revises: d3b81c07a55e
Create Date: 2026-09-09 12:40:00.000000

Registered models can now be Text Embedding models, not just LLMs, and
an embedding model has one property an LLM does not: the width of the
vector it returns. That number sizes the pgvector column at ingest time
and cannot be changed afterwards without re-embedding everything, so it
is recorded per model.

Nullable on purpose — it is safer to discover the real dimension from
the provider's own first response than to trust a hand-typed value, and
embeddings.py prefers a live measurement over this column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e5f27a1b9c34'
down_revision: Union[str, Sequence[str], None] = 'd3b81c07a55e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('providermodel', sa.Column('embedding_dimension', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('providermodel', 'embedding_dimension')
