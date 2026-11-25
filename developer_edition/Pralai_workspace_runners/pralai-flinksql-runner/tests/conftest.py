"""Test configuration — mirrors workspace-service and dbt-runner conventions.

DATABASE SAFETY (read before touching any DB fixture)
─────────────────────────────────────────────────────
DB-backed tests NEVER pick a database on their own. They use only
TEST_DATABASE_URL, are skipped when it is unset, and the run is REFUSED
(pytest.fail) if that URL's database name does not end in ``_test`` or it
points at the local runner stack's live Postgres (host port 5435). There is
deliberately no hardcoded DSN anywhere in this test suite: a previous
revision pointed fixtures at localhost:5435/flinksql and its teardown
(``drop_all``) destroyed a developer's live job registry.

The schema is created by running the real Alembic migrations (so the
migrations themselves are exercised), each test's rows are TRUNCATEd after
the test, and the whole schema is dropped at the end of the session — only
ever after the ``_test`` guard above has passed.
"""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.update({
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "REDIS_URL": "redis://localhost:6379/3",
    "FLINK_GATEWAY_URL": "http://localhost:8083",
    "FLINK_REST_URL": "http://localhost:8081",
    "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
    "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
    "SCHEMA_REGISTRY_URL": "http://localhost:8085",
    "POLARIS_REST_URI": "http://localhost:8181/api/catalog",
    "ICEBERG_WAREHOUSE": "/tmp/test-warehouse",
    "CHECKPOINT_STORAGE_PATH": "/tmp/test-checkpoints",
    "CONTROL_PLANE_JWKS_URL": "http://test-jwks/jwks",
    "CONTROL_PLANE_ISSUER": "http://test-issuer",
    "CONTROL_PLANE_AUDIENCE": "flinksql-runner",
    "INTERNAL_SERVICE_TOKEN": "test-internal-token",
    "ENVIRONMENT": "dev",
    "LOG_LEVEL": "WARNING",
    # The app's background loops (metrics poller, reconciler, quality
    # consumer) need a real Postgres for leader election; the shared
    # TestClient runs against in-memory SQLite, so they stay off there.
    # Tests exercise those loops directly against the throwaway test DB.
    "BACKGROUND_TASKS_ENABLED": "false",
    "READINESS_CHECK_FLINK": "false",
})
# Never inherit a real database URL into the test process's app settings.
os.environ.pop("ORG_ID", None)

from app.main import app  # noqa: E402 — import after env setup
from app.db.migration_url import sync_migration_url

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module that binds AsyncSessionLocal / engine at import time — the DB
# fixture repoints all of them at the throwaway test database.
_SESSION_PATCH_TARGETS = (
    "app.db.session.AsyncSessionLocal",
    "app.services.deployment_reconciler.AsyncSessionLocal",
    "app.services.metrics_poller.AsyncSessionLocal",
    "app.services.quality_consumer.AsyncSessionLocal",
    "app.services.preview_audit.AsyncSessionLocal",
    "app.main.AsyncSessionLocal",
    "app.core.deps.AsyncSessionLocal",
)


# ─────────────────────────────────────────────────────────────────────────
# Throwaway-DB guard
# ─────────────────────────────────────────────────────────────────────────
def assert_safe_test_database_url(url: str) -> str:
    """Return `url` if it is unmistakably a throwaway test database;
    otherwise FAIL the run loudly. Never skip silently on an unsafe URL."""
    from sqlalchemy.engine import make_url

    try:
        u = make_url(url)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"REFUSING DB tests: TEST_DATABASE_URL is not a valid URL ({e})", pytrace=False)
    name = u.database or ""
    if not u.drivername.startswith("postgresql"):
        pytest.fail(f"REFUSING DB tests: TEST_DATABASE_URL must be PostgreSQL, got {u.drivername!r}",
                    pytrace=False)
    if not name.endswith("_test"):
        pytest.fail(
            f"REFUSING DB tests: TEST_DATABASE_URL database {name!r} does not end with '_test'. "
            "These tests TRUNCATE and DROP their schema — they must never run against a "
            "real database.", pytrace=False,
        )
    if u.port == 5435 or (u.host in ("localhost", "127.0.0.1", "host.docker.internal")
                          and u.port in (None, 5435)):
        pytest.fail(
            "REFUSING DB tests: TEST_DATABASE_URL points at the local runner stack's live "
            "Postgres (port 5435). Use a dedicated throwaway Postgres instead.", pytrace=False,
        )
    return url


def _test_database_url_or_skip() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — DB-backed tests need a throwaway Postgres "
                    "whose database name ends with '_test'")
    return assert_safe_test_database_url(url)


def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", sync_migration_url(url))
    cfg.attributes["configure_logger"] = False
    return cfg


def _reset_schema(url: str) -> None:
    """DROP and recreate the public schema — re-checks the _test guard
    itself immediately before doing anything destructive."""
    from sqlalchemy import create_engine, text

    assert_safe_test_database_url(url)
    engine = create_engine(sync_migration_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def migrated_test_db():
    """Session-wide: the throwaway test DB, migrated to head by the real
    Alembic migrations. Schema dropped again at session end."""
    from alembic import command

    url = _test_database_url_or_skip()
    _reset_schema(url)
    command.upgrade(alembic_config(url), "head")
    yield url
    _reset_schema(url)


@pytest.fixture
async def pg_db(migrated_test_db, monkeypatch):
    """Per-test async sessionmaker bound to the throwaway test DB, patched
    into every module that holds AsyncSessionLocal. Rows are truncated
    after each test."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.services import deployment_reconciler, job_service, metrics_poller

    engine = create_async_engine(migrated_test_db, poolclass=NullPool)
    sessionmaker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    for target in _SESSION_PATCH_TARGETS:
        monkeypatch.setattr(target, sessionmaker, raising=False)
    monkeypatch.setattr("app.db.session.engine", engine)
    job_service._ACTIVE_DEPLOYMENT_IDS.clear()
    deployment_reconciler._IN_FLIGHT.clear()
    deployment_reconciler._semaphore = None
    deployment_reconciler._wake = None
    metrics_poller._last_retention_sweep = None

    yield sessionmaker

    # Let deploy/stop tasks the reconciler started finish before wiping rows.
    pending = [t for t in list(deployment_reconciler._IN_FLIGHT.values()) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=10)
    job_service._ACTIVE_DEPLOYMENT_IDS.clear()
    deployment_reconciler._IN_FLIGHT.clear()

    async with engine.begin() as conn:
        tables = (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ))).scalars().all()
        if tables:
            await conn.execute(text(
                "TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"
            ))
    await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────
# HTTP client fixtures
# ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def auth_headers():
    """Returns a header dict with a mock JWT.
    Real validation skipped in tests — override validate_jwt in conftest for CI."""
    return {"Authorization": "Bearer dev-test-token"}


@pytest.fixture
def internal_headers():
    return {"X-Internal-Service-Token": "test-internal-token"}
