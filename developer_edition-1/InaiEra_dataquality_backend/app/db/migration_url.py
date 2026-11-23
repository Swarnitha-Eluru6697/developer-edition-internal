"""Database URL for Alembic (sync) — see sync_migration_url."""
from __future__ import annotations


def sync_migration_url(url: str) -> str:
    """Alembic runs synchronously, so swap the app's async driver for psycopg2
    — named EXPLICITLY. A bare "postgresql://" resolves to the dialect's default
    driver, and SQLAlchemy 2.1 changed that default from psycopg2 to psycopg
    (v3), which isn't installed: the service then crashed on startup before
    migrating."""
    from sqlalchemy.engine import make_url
    u = make_url(url)
    if u.get_backend_name() == "postgresql":
        u = u.set(drivername="postgresql+psycopg2")
    return u.render_as_string(hide_password=False)
