"""git connections for committing agent YAML

Revision ID: d3f7a91c2e48
Revises: b7d2e4f9a013
Create Date: 2026-10-16 12:00:00.000000

A GitConnection is a GitHub / GitLab / Bitbucket account (personal access
token, encrypted) that exported agents are committed to.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd3f7a91c2e48'
down_revision: Union[str, Sequence[str], None] = 'b7d2e4f9a013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'gitconnection',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('base_url', sa.String(), nullable=True),
        sa.Column('username', sa.String(), nullable=True),
        # Ciphertext, never plaintext — see models.GitConnection.
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('account', sa.String(), nullable=True),
        sa.Column('default_repo', sa.String(), nullable=True),
        sa.Column('default_branch', sa.String(), nullable=True),
        sa.Column('default_folder', sa.String(), nullable=True),
        sa.Column('commit_on_download', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('gitconnection')
