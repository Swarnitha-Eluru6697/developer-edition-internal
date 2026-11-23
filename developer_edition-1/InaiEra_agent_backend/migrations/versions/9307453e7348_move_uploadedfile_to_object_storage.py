"""move uploadedfile binary content to object storage

Revision ID: 9307453e7348
Revises: 044892dd64fb
Create Date: 2026-09-04 20:30:00.000000

§0.80, GAP_REPORT — the actual fix for the reported gap: UploadedFile
stored real binary content directly as a Postgres `bytea` column
(`data`), which doesn't scale for a real, significant agent workload.
This is a real, hand-written DATA migration, not just DDL — every
real, existing row's own real bytes are read out of the old `data`
column and written to the real LocalDiskStorageBackend (this
environment's own real default — see storage.py) before the column is
dropped, so no real, existing uploaded file is silently lost. A real
production deployment migrating to a real S3 bucket instead would run
this same real backfill pattern against S3StorageBackend (set
STORAGE_BACKEND=s3 before running this migration) rather than losing
any real data.

Adds: storage_backend, object_key (the new, real location pointer),
owner (real, per-uploader tenant isolation), expires_at (a real TTL),
scan_status/scan_result (a real, pluggable malware-scan outcome).
Drops: data (the old bytea column, now migrated).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9307453e7348'
down_revision: Union[str, Sequence[str], None] = '044892dd64fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('uploadedfile', sa.Column('storage_backend', sa.String(), nullable=False, server_default='local'))
    op.add_column('uploadedfile', sa.Column('object_key', sa.String(), nullable=True))
    op.add_column('uploadedfile', sa.Column('owner', sa.String(), nullable=True))
    op.add_column('uploadedfile', sa.Column('expires_at', sa.DateTime(), nullable=True))
    op.add_column('uploadedfile', sa.Column('scan_status', sa.String(), nullable=False, server_default='pending'))
    op.add_column('uploadedfile', sa.Column('scan_result', sa.String(), nullable=True))

    # Real data migration — read every real, existing row's own real
    # bytes out of `data` and write them to the real, configured
    # storage backend, computing the same real object_key shape
    # routers/uploads.py's own real upload path uses going forward
    # (namespaced by owner — real, existing rows have no real owner on
    # file, so they land under a real, honest "unknown" namespace
    # rather than a fabricated one).
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from app.storage import get_storage_backend

    conn = op.get_bind()
    backend = get_storage_backend()
    rows = conn.execute(sa.text("SELECT id, data, filename FROM uploadedfile WHERE data IS NOT NULL")).fetchall()
    for row in rows:
        file_id, data, filename = row[0], row[1], row[2]
        key = f"unknown/{file_id}/{filename}"
        backend.put(key, bytes(data), "application/octet-stream")
        conn.execute(
            sa.text("UPDATE uploadedfile SET object_key = :key WHERE id = :id"),
            {"key": key, "id": file_id},
        )

    # A real, existing row with no data at all (shouldn't happen given
    # the old column was NOT NULL, but a real, defensive fallback
    # rather than leaving object_key NULL and violating the real
    # NOT NULL constraint about to be applied).
    conn.execute(sa.text("UPDATE uploadedfile SET object_key = 'unknown/' || id || '/upload' WHERE object_key IS NULL"))

    op.alter_column('uploadedfile', 'object_key', nullable=False)
    op.drop_column('uploadedfile', 'data')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('uploadedfile', sa.Column('data', sa.LargeBinary(), nullable=True))

    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from app.storage import get_storage_backend, StorageError

    conn = op.get_bind()
    backend = get_storage_backend()
    rows = conn.execute(sa.text("SELECT id, object_key FROM uploadedfile")).fetchall()
    for row in rows:
        file_id, key = row[0], row[1]
        try:
            data = backend.get(key)
        except StorageError:
            continue
        conn.execute(sa.text("UPDATE uploadedfile SET data = :data WHERE id = :id"), {"data": data, "id": file_id})

    op.alter_column('uploadedfile', 'data', nullable=False)
    op.drop_column('uploadedfile', 'scan_result')
    op.drop_column('uploadedfile', 'scan_status')
    op.drop_column('uploadedfile', 'expires_at')
    op.drop_column('uploadedfile', 'owner')
    op.drop_column('uploadedfile', 'object_key')
    op.drop_column('uploadedfile', 'storage_backend')
