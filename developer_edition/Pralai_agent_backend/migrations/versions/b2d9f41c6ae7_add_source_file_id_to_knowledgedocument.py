"""keep the original uploaded file against its knowledge document

Revision ID: b2d9f41c6ae7
Revises: f8c31d5e2a70
Create Date: 2026-09-11 15:05:00.000000

Ingestion read a file, extracted its text and discarded the bytes, so
after uploading a PDF on the Documents tab the only copy of that PDF was
still on the uploader's own machine — while the Upload API tab's "Stage
on the platform" button had been putting files in S3 all along, through
routers/uploads.py.

This column points a document at the UploadedFile row holding its
original, so the two paths store files the same way and in the same
place. Nullable, and left null for every existing document: those files
genuinely were not kept, and backfilling anything here would be
inventing a location for bytes that do not exist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2d9f41c6ae7'
down_revision: Union[str, Sequence[str], None] = 'f8c31d5e2a70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('knowledgedocument',
                  sa.Column('source_file_id', sa.String(), nullable=True))
    op.create_index('ix_knowledgedocument_source_file_id', 'knowledgedocument',
                    ['source_file_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_knowledgedocument_source_file_id', table_name='knowledgedocument')
    op.drop_column('knowledgedocument', 'source_file_id')
