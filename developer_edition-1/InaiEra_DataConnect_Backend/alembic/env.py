import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from alembic import context
from app import (
    models,  # noqa: F401 -- registers every ORM model onto Base.metadata for autogenerate
)
from app.core.config import settings
from app.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    # Alembic's own alembic_version.version_num is a hardcoded VARCHAR(32)
    # (see alembic/ddl/impl.py's version_table_impl) with no config knob to
    # widen it — but this project's revision ids are the migration's own
    # descriptive filename stem (e.g. "0024_pipeline_schema_introspection_status",
    # 41 chars), well past that limit. Alembic only creates the version table
    # if it doesn't already exist, so pre-creating it here with a wide enough
    # column, before context.run_migrations() ever touches it, is what makes
    # `alembic upgrade head` actually able to run against a fresh database.
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(255) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    ))
    connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # settings.database_url is postgresql+asyncpg://... (this project's engine is
    # async-only — see app/db/session.py) — engine_from_config()'s sync .connect()
    # can't drive an asyncpg URL at all (it raises MissingGreenlet immediately,
    # regardless of the greenlet dependency being present). AsyncEngine.run_sync()
    # is the standard bridge: it runs the ordinarily-sync Alembic migration
    # context inside the async engine's own greenlet.
    connectable: AsyncEngine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
