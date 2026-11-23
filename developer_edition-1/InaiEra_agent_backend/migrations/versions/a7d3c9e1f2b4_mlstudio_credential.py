"""ML Studio endpoint credentials for LLM nodes

Revision ID: a7d3c9e1f2b4
Revises: d95a3f1b7c62
Create Date: 2026-11-06 15:00:00.000000

Models deployed in ML Studio are selectable in the Builder's model
dropdown (as ``mlstudio/<model_id>``). ML Studio authenticates each
endpoint by its own client secret, so the secret is stored per model,
encrypted, in this table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'a7d3c9e1f2b4'
down_revision: Union[str, Sequence[str], None] = 'd95a3f1b7c62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'mlstudiocredential',
        sa.Column('model_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('client_secret', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('model_id'),
    )


def downgrade() -> None:
    op.drop_table('mlstudiocredential')
