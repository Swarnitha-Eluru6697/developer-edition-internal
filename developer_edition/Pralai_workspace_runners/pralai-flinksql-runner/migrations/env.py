from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config, pool
from alembic import context
from app.db.migration_url import sync_migration_url
from app.db.base import Base
import app.models  # noqa: F401 — ensure all models are registered

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """An explicitly-passed sqlalchemy.url (e.g. the test suite's throwaway
    TEST_DATABASE_URL) wins; otherwise DATABASE_URL from the environment,
    exactly as before. Migrations run over the sync psycopg2 driver."""
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL", "")
    return sync_migration_url(url)


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
