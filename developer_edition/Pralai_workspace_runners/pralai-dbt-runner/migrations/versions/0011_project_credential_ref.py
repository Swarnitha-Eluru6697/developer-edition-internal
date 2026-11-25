"""projects.credential_ref; credentials leave the repository URL (DBT-009).

`git_url` used to be allowed to carry `user:token@host`, which put a credential into this table, API output, logs and each run's Secret. The
runner now refuses such URLs; a project names its credential with `credential_ref` instead (see app/core/git_credentials.py).

This migration (1) adds the nullable `credential_ref`, and (2) rewrites every stored `git_url` that has a secret-capable user info (any on http(s), a password elsewhere) to the plain URL. The token that was
in it is discarded here: a PRIVATE repository registered that way will fail its next run until its operator provisions a credential
(`GIT_CREDENTIAL_<REF>`) and sets `credential_ref` on the project. If two rows would become the same project once their credentials are removed the
migration stops and lists them; remove the duplicate first.

Revision ID: 0011_project_credential_ref
Revises:     0010_run_idempotency_evidence
"""
from __future__ import annotations

from typing import Sequence, Union
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0011_project_credential_ref"
down_revision: Union[str, None] = "0010_run_idempotency_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def _dirty(url: str) -> bool:
    """Same rule as app.core.git_credentials.has_userinfo (copied: migrations must not import app code): any user info on http(s), a
    password on any other scheme. A bare `ssh://git@host` user name is an account name and stays."""
    try:
        parts = urlsplit(url or "")
        if parts.scheme.lower() in ("http", "https"):
            return "@" in (parts.netloc or "")
        return parts.password is not None
    except ValueError:
        return False


def _clean(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    user = "" if parts.scheme.lower() in ("http", "https") or not parts.username else parts.username + "@"
    netloc = user + host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def upgrade() -> None:
    op.add_column("projects", sa.Column("credential_ref", sa.String(64), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, org_id, git_url, git_ref, project_path FROM projects")).fetchall()
    dirty = [r for r in rows if _dirty(r.git_url)]
    taken = {(r.org_id, r.git_url, r.git_ref, r.project_path) for r in rows}
    clashes = []
    for r in dirty:
        key = (r.org_id, _clean(r.git_url), r.git_ref, r.project_path)
        if key in taken:
            clashes.append(f"{r.id} ({_clean(r.git_url)}@{r.git_ref})")
        taken.add(key)
    if clashes:
        raise RuntimeError("these projects become duplicates once the credential is removed from their git_url; remove one of each "
                           "pair and re-run the migration: " + ", ".join(clashes))
    for r in dirty:
        bind.execute(sa.text("UPDATE projects SET git_url = :u WHERE id = :i"), {"u": _clean(r.git_url), "i": r.id})


def downgrade() -> None:
    op.drop_column("projects", "credential_ref")
