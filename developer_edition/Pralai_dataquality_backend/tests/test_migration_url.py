"""Alembic must use an EXPLICIT psycopg2 driver: SQLAlchemy 2.1 made psycopg
(v3) the default for a bare postgresql:// URL, which isn't installed — the
service then crashed on startup before applying any migration."""
from sqlalchemy.engine import make_url

from app.db.migration_url import sync_migration_url


def test_async_url_becomes_explicit_psycopg2():
    u = make_url(sync_migration_url("postgresql+asyncpg://u:p%40ss@db:5432/app"))
    assert u.drivername == "postgresql+psycopg2"
    assert (u.username, u.password, u.host, u.port, u.database) == ("u", "p@ss", "db", 5432, "app")


def test_bare_postgresql_url_is_pinned_not_left_to_the_default():
    assert make_url(sync_migration_url("postgresql://u:p@db/app")).drivername == "postgresql+psycopg2"


def test_non_postgres_urls_are_untouched():
    assert sync_migration_url("sqlite:///x.db") == "sqlite:///x.db"
