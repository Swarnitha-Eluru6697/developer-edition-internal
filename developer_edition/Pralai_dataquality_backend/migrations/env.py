import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from app.db.migration_url import sync_migration_url
from app.db.base import Base
import app.models  # noqa: F401

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # An explicit URL handed in programmatically (the test harness passes the
    # guarded TEST_DATABASE_URL this way) wins over the process environment,
    # so a stray DATABASE_URL in the environment can never redirect a test
    # migration at a different database.
    url = config.attributes.get("sqlalchemy.url")
    if not url:
        from app.core.config import get_settings
        url = get_settings().effective_database_url() if os.environ.get("DATABASE_URL") else ""
    if not url:
        raise RuntimeError("No database URL configured for migrations (DATABASE_URL)")
    return sync_migration_url(url)


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": _database_url()}, prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
