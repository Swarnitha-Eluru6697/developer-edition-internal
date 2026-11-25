"""runtime-configurable upload destinations

Revision ID: c5e8a71d9f34
Revises: b2d9f41c6ae7
Create Date: 2026-09-12 10:20:00.000000

Where uploaded files go was decided entirely by .env — one bucket per
deployment, changeable only by editing a file on the server and
restarting the stack. This table makes a destination a real, stored,
credentialed thing that can be added and tested from the upload dialog.

uploadedfile.storage_destination_id records which one holds a given
object. Null means the environment-configured destination, which is what
every existing row used, so nothing needs backfilling — and nothing
silently claims to live in a bucket it was never written to.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c5e8a71d9f34'
down_revision: Union[str, Sequence[str], None] = 'b2d9f41c6ae7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'storagedestination',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False, server_default='s3'),
        sa.Column('bucket', sa.String(), nullable=False),
        sa.Column('region', sa.String(), nullable=True),
        sa.Column('endpoint_url', sa.String(), nullable=True),
        # Ciphertext, never plaintext — see models.StorageDestination.
        sa.Column('access_key_id', sa.String(), nullable=True),
        sa.Column('secret_access_key', sa.String(), nullable=True),
        sa.Column('session_token', sa.String(), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('uploadedfile',
                  sa.Column('storage_destination_id', sa.String(), nullable=True))
    op.create_index('ix_uploadedfile_storage_destination_id', 'uploadedfile',
                    ['storage_destination_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_uploadedfile_storage_destination_id', table_name='uploadedfile')
    op.drop_column('uploadedfile', 'storage_destination_id')
    op.drop_table('storagedestination')
