"""encrypt connection secrets at rest

GAP-01: dc_connection_secrets.values held each connection's credentials as readable JSON. This re-encrypts
every plaintext row into an AES-256-GCM envelope (see app/services/secret_crypto.py). It needs
DATACONNECT_SECRET_ENCRYPTION_KEY — the migration refuses to run without it rather than leave rows plaintext.
Already-encrypted rows are skipped, so it is safe to re-run. Downgrade decrypts back to plaintext (also needs the key).

Revision ID: 0035_encrypt_connection_secrets
Revises: 0034_quality_job_lease
Create Date: 2026-10-01
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "0035_encrypt_connection_secrets"
down_revision = "0034_quality_job_lease"
branch_labels = None
depends_on = None

_TABLE = sa.table(
    "dc_connection_secrets",
    sa.column("connection_id", sa.String),
    sa.column("org_id", sa.String),
    sa.column("values", sa.JSON),
)


def _rows(bind):
    return bind.execute(sa.select(_TABLE.c.connection_id, _TABLE.c.org_id, _TABLE.c["values"])).fetchall()


def _store(bind, connection_id, new_values):
    bind.execute(
        sa.text("UPDATE dc_connection_secrets SET values = CAST(:v AS jsonb) WHERE connection_id = CAST(:c AS uuid)"),
        {"v": json.dumps(new_values), "c": connection_id},
    )


def upgrade() -> None:
    from app.services.secret_crypto import SecretEncryptionError, encrypt_fields, is_encrypted

    bind = op.get_bind()
    rows = _rows(bind)
    todo = [r for r in rows if r.values and not is_encrypted(r.values)]
    if not todo:
        return
    try:
        for row in todo:
            _store(bind, str(row.connection_id), encrypt_fields(dict(row.values), org_id=row.org_id, connection_id=str(row.connection_id)))
    except SecretEncryptionError as exc:
        raise RuntimeError(f"Cannot encrypt existing connection secrets: {exc.message}") from exc


def downgrade() -> None:
    from app.services.secret_crypto import decrypt_fields, is_encrypted

    bind = op.get_bind()
    for row in _rows(bind):
        if row.values and is_encrypted(row.values):
            _store(bind, str(row.connection_id), decrypt_fields(dict(row.values), org_id=row.org_id, connection_id=str(row.connection_id)))
