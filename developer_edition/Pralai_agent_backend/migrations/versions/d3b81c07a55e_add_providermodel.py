"""add providermodel for user-registered models

Revision ID: d3b81c07a55e
Revises: c7a4d1f92b08
Create Date: 2026-09-08 23:55:00.000000

The Integrations page offered an "Add model" control per provider, but it
was a decorative <span> with no click handler, no endpoint, and nowhere
to persist to — the model catalog lived only as Python dicts in
model_adapter.py, and call_llm refuses any model missing from them. This
table is where a registered model goes so call_llm can resolve it.

api_model_id is the vendor's own identifier, which is what the SDK is
called with; name is just the label the builder shows. Prices are
per-million-tokens to match _DEFAULT_PRICE_TABLE's units and are
nullable — an unpriced model contributes zero cost rather than a
fabricated figure.

Scoped to this one table, matching the precedent of every prior
migration here: autogenerate also reports pre-existing, unrelated drift
(TEXT vs AutoString, nullability) that is not this change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'd3b81c07a55e'
down_revision: Union[str, Sequence[str], None] = 'c7a4d1f92b08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'providermodel',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('provider_name', sa.String(), nullable=False),
        sa.Column('api_model_id', sa.String(), nullable=False),
        sa.Column('types', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('input_price_per_mtok', sa.Float(), nullable=True),
        sa.Column('output_price_per_mtok', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['provider_name'], ['modelprovider.name'], ),
        sa.PrimaryKeyConstraint('name'),
    )
    op.create_index(op.f('ix_providermodel_provider_name'), 'providermodel', ['provider_name'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_providermodel_provider_name'), table_name='providermodel')
    op.drop_table('providermodel')
