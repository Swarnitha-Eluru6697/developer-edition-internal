"""Alembic environment. Async-aware.

Reads DATABASE_URL from the workspace service settings if set so the same
config drives both runtime and migrations. Falls back to alembic.ini.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
# Import models so they register against Base.metadata
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# URL precedence: an explicit URL handed in programmatically (the test
# suite passes its throwaway *_test database this way via
# config.attributes["sqlalchemy.url"]) wins; otherwise the runtime settings
# (DATABASE_URL, or the discrete DATABASE_HOST/... fields Helm sets) drive
# migrations exactly as they drive the app. alembic.ini's placeholder URL is
# never used.
_explicit_url = config.attributes.get("sqlalchemy.url")
_settings = get_settings()
config.set_main_option(
    "sqlalchemy.url",
    (_explicit_url or _settings.effective_database_url).replace("%", "%%"),
)
_connect_args = {} if _explicit_url else _settings.database_connect_args()

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
