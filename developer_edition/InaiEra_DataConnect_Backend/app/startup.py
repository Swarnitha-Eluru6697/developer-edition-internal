"""
Backend release/startup contract — this is what the Dockerfile actually
runs (see CMD), not uvicorn directly:

    validate configuration
    -> verify PostgreSQL is reachable
    -> run/verify database migrations (alembic upgrade head; a virgin database is created from the models and stamped at head)
    -> verify the connector-template catalog is seeded at the expected version
    -> hand off to the API server (exec, not spawn — see main())

Every step raises on failure, and main() exits non-zero rather than
catching and continuing. Previously the only database initialization this
backend did at startup was inside FastAPI's lifespan hook, wrapped in
`try/except Exception: logger.exception(...)` — a broken database
connection, or migrations that had never been applied, still let the API
process start and accept traffic. A container that fails closed (exits, so
the orchestrator's restart/health policy takes over) is safer than one that
starts serving against a schema it never verified.

FastAPI's own lifespan hook (app/main.py) still does its own best-effort
template-catalog load on top of this — that one stays deliberately
permissive (falls back to an in-memory seed rather than failing) because it
also has to support `uvicorn app.main:app` invoked directly in local dev,
and the test suite's TestClient, neither of which go through this script or
necessarily have a database at all. This script is the strict contract for
the actual deployed container; the lifespan hook is a dev-mode convenience,
not a second enforcement point.
"""

import asyncio
import os
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def validate_configuration() -> None:
    """Fail fast on configuration that would make the rest of this contract
    meaningless, or unsafe, to run."""
    from app.core.config import settings

    problems: list[str] = []
    if not settings.database_url:
        problems.append("DATACONNECT_DATABASE_URL is not set")
    if settings.env == "production" and settings.jwt_secret == "CHANGEME":
        problems.append("DATACONNECT_JWT_SECRET is still the insecure default in a production environment")
    if settings.env != "development" and not settings.secret_encryption_key.strip():
        problems.append(
            "DATACONNECT_SECRET_ENCRYPTION_KEY is not set — connection credentials would have no encryption at rest"
        )
    try:
        from app.services.secret_crypto import validate_configuration as validate_secret_encryption

        validate_secret_encryption()
    except Exception as exc:  # noqa: BLE001 - report it with the other problems, never the key itself
        problems.append(f"secret encryption key invalid: {exc}")
    if settings.topic_default_min_insync_replicas > settings.topic_default_replication_factor:
        problems.append(
            f"DATACONNECT_TOPIC_DEFAULT_MIN_INSYNC_REPLICAS ({settings.topic_default_min_insync_replicas}) exceeds "
            f"DATACONNECT_TOPIC_DEFAULT_REPLICATION_FACTOR ({settings.topic_default_replication_factor}): no topic could ever "
            "be written to with acks=all"
        )
    from app.services.connect_runtime import configuration_problems as connect_pool_problems

    problems.extend(connect_pool_problems())
    # Alerts: signed Acknowledge / Resolve links. A public URL without a secret would mean no buttons for no visible reason; a short secret
    # is guessable, and anyone holding a forged link could acknowledge or resolve alerts without signing in.
    if settings.alerts_public_url.strip() and not settings.alerts_action_secret:
        problems.append("DATACONNECT_ALERTS_PUBLIC_URL is set but DATACONNECT_ALERTS_ACTION_SECRET is not: alert buttons cannot be signed")
    if settings.alerts_action_secret and len(settings.alerts_action_secret) < 32:
        problems.append("DATACONNECT_ALERTS_ACTION_SECRET is shorter than 32 characters")
    if problems:
        raise RuntimeError("Configuration invalid:\n  - " + "\n  - ".join(problems))
    logger.info("startup.configuration_valid", env=settings.env)


async def verify_postgres(max_attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Bounded retry — Postgres in a freshly-started compose/k8s stack can
    take a few seconds to accept connections, but this must not hang the
    container forever: after max_attempts it raises."""
    from sqlalchemy import text

    from app.db.session import engine

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("startup.postgres_reachable", attempt=attempt)
            return
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "startup.postgres_unreachable", attempt=attempt, max_attempts=max_attempts, error=str(exc)
            )
            if attempt < max_attempts:
                await asyncio.sleep(delay_seconds)
    raise RuntimeError(f"PostgreSQL not reachable after {max_attempts} attempts") from last_exc


async def _is_virgin_database() -> bool:
    """True when the database has no DataConnect schema at all: no alembic_version table and no `dc_*` table."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            found = (await conn.execute(text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema() "
                "AND (table_name = 'alembic_version' OR table_name LIKE 'dc\\_%')"
            ))).scalar_one()
        return found == 0
    finally:
        await engine.dispose()


async def _create_schema_from_models() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app import models  # noqa: F401 - registers every ORM model on Base.metadata
    from app.core.config import settings
    from app.db.base import Base

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def run_migrations() -> None:
    """Bring the schema to head. Alembic itself raises on failure — nothing here catches that; a migration failure must stop the
    container from starting, not just get logged and skipped.

    A VIRGIN database (no DataConnect tables at all) is not migrated from revision 0001: that chain is a series of deltas written
    against a database `Base.metadata.create_all` had already built (it cannot build one from nothing — it fails at 0042, and would
    produce a schema that differs from the models: UUID instead of VARCHAR template ids, missing indexes, different nullability). So a
    virgin database gets its schema from the models, exactly as docker-compose.fresh.yml documents, and is STAMPED at head so every
    later release migrates it normally. An existing database is upgraded as before."""
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    if asyncio.run(_is_virgin_database()):
        logger.info("startup.virgin_database_initialising_from_models")
        asyncio.run(_create_schema_from_models())
        command.stamp(cfg, "head")
        logger.info("startup.migrations_applied", mode="virgin_database_stamped_at_head")
        return
    command.upgrade(cfg, "head")
    logger.info("startup.migrations_applied", mode="upgrade")


async def verify_seed_version() -> None:
    """The connector-template catalog must be present and match every
    template this build expects. A mismatch means the schema/seed data has
    drifted from what this running code assumes — not something to start
    the API against silently."""
    from sqlalchemy import select

    from app.db.seed_templates import TEMPLATE_SEED, init_template_catalog
    from app.db.session import AsyncSessionLocal
    from app.models.catalog import ConnectorTemplate

    await init_template_catalog()
    expected_ids = {t["id"] for t in TEMPLATE_SEED}
    async with AsyncSessionLocal() as session:
        existing_ids = set((await session.execute(select(ConnectorTemplate.id))).scalars().all())
    missing = expected_ids - existing_ids
    if missing:
        raise RuntimeError(
            f"Connector template catalog is missing expected templates: {sorted(missing)}"
        )
    logger.info("startup.seed_verified", template_count=len(expected_ids))


def main() -> None:
    from app.core.logging import configure_logging

    configure_logging()

    try:
        validate_configuration()
        asyncio.run(verify_postgres())
        run_migrations()
        asyncio.run(verify_seed_version())
    except Exception:
        logger.exception("startup.failed")
        sys.exit(1)

    logger.info("startup.contract_complete")

    # Hand off to uvicorn as this SAME process (exec, not subprocess) — no
    # wrapper process left supervising it, so signals (SIGTERM on a
    # deploy/scale-down) reach uvicorn directly instead of being absorbed
    # by a shell or Python parent that doesn't forward them.
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"])


if __name__ == "__main__":
    main()
