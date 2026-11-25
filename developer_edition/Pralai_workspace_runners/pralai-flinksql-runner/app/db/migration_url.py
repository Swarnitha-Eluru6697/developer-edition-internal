"""Database URL for Alembic (sync) — see sync_migration_url."""
from __future__ import annotations


def sync_migration_url(url: str) -> str:
    """Name psycopg2 EXPLICITLY. A bare "postgresql://" resolves to the
    dialect's default driver, and SQLAlchemy 2.1 changed that default to
    psycopg (v3), which isn't installed — a fresh image build would then fail
    to migrate (and crash the entrypoint) before applying a single revision."""
    if not url:
        return url
    from sqlalchemy.engine import make_url
    u = make_url(url)
    if u.get_backend_name() == "postgresql":
        u = u.set(drivername="postgresql+psycopg2")
    return u.render_as_string(hide_password=False)
