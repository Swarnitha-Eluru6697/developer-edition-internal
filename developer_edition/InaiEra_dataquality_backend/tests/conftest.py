"""Test configuration.

DATABASE SAFETY (read before touching any DB fixture)
─────────────────────────────────────────────────────
DB-backed tests NEVER pick a database on their own. They use ONLY the
database named by TEST_DATABASE_URL:

  * TEST_DATABASE_URL unset                      -> DB-backed tests are SKIPPED.
  * database name does not end in ``_test``      -> the whole run FAILS loudly.
  * port is 5437 / 5433 / 5435 (the live DQ, dbt
    runner and FlinkSQL runner Postgres ports), or
    the host is one of the live compose services -> the whole run FAILS loudly.

There is deliberately no default/fallback DSN anywhere in this suite. The
schema is built with the REAL Alembic migrations (``upgrade head``), each
DB-backed test TRUNCATEs the app tables first, and the schema is dropped at
the end of the session — only ever after the guard above has passed (the
guard is re-checked immediately before every destructive statement).

The application engine is created at import time from DATABASE_URL, so
DATABASE_URL is set here, BEFORE the app is imported, to either the guarded
TEST_DATABASE_URL or an unreachable placeholder that can never resolve to a
real server. Nothing the app does at import time may reach a live database.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PORTS = {5437, 5433, 5435}
FORBIDDEN_HOSTS = {
    "dataquality-postgres-1", "dbtrunner-postgres-1", "flinksql-postgres-1",
    "workspace-postgres-1", "ucs-postgres-1", "inaiera_agent_backend-db-1",
}
_UNREACHABLE_DSN = "postgresql+asyncpg://nobody:nobody@127.0.0.1:9/unconfigured_test"


class UnsafeTestDatabase(Exception):
    pass


def check_test_database_url(raw: str) -> str:
    """Return `raw` if it unmistakably names a throwaway *_test Postgres;
    raise UnsafeTestDatabase otherwise. Pure function (unit-tested)."""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(raw)
    except Exception as e:  # noqa: BLE001
        raise UnsafeTestDatabase(f"TEST_DATABASE_URL is not a valid URL ({e})")
    if not url.drivername.startswith("postgresql"):
        raise UnsafeTestDatabase(f"TEST_DATABASE_URL must be PostgreSQL, got {url.drivername!r}")
    name = url.database or ""
    if not name.endswith("_test"):
        raise UnsafeTestDatabase(
            f"TEST_DATABASE_URL database {name!r} does not end with '_test'. The suite DROPS "
            "the schema of the database it is given — use a throwaway *_test database.")
    if url.port in FORBIDDEN_PORTS:
        raise UnsafeTestDatabase(
            f"TEST_DATABASE_URL uses port {url.port}, which is a live stack's Postgres "
            f"(forbidden ports: {sorted(FORBIDDEN_PORTS)}).")
    if (url.host or "") in FORBIDDEN_HOSTS:
        raise UnsafeTestDatabase(f"TEST_DATABASE_URL host {url.host!r} is a live compose service.")
    return raw


def _guarded_url_or_none() -> str | None:
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        return None
    try:
        return check_test_database_url(raw)
    except UnsafeTestDatabase as e:
        pytest.exit(f"\n\n*** REFUSING TO RUN DB TESTS ***\n{e}\n", returncode=3)


_TEST_DB_URL = _guarded_url_or_none()

# Env BEFORE importing the app (Settings is lru_cached on first use).
os.environ.update({
    "ENVIRONMENT": "dev",
    "DATABASE_URL": _TEST_DB_URL or _UNREACHABLE_DSN,
    # Every test gets its own event loop; pooled asyncpg connections bound
    # to a closed loop would break the next test.
    "DATABASE_POOL_DISABLED": "true",
    "REDIS_URL": "redis://127.0.0.1:9/4",
    "CONTROL_PLANE_JWKS_URL": "http://test-jwks/jwks",
    "CONTROL_PLANE_ISSUER": "http://test-issuer",
    "CONTROL_PLANE_AUDIENCE": "workspace.inaiera",
    "ORG_ID": "test-org",
    "SEED_SYSTEM_RULES": "false",
    "LOG_LEVEL": "WARNING",
    "OUTBOX_WORKER_ENABLED": "false",
    # Integrations: real-looking config so startup/readiness checks pass;
    # every outbound call in tests is mocked with respx.
    "UCS_ENABLED": "true",
    "UCS_URL": "http://ucs.test",
    "UCS_SERVICE_JWT_SECRET": "ucs-test-secret-0123456789abcdef0123",
    "DBTRUNNER_ENABLED": "true",
    "DBTRUNNER_URL": "http://dbtrunner.test",
    "DBTRUNNER_SERVICE_JWT_SECRET": "dbt-test-secret-0123456789abcdef0123",
    "FLINKSQL_ENABLED": "true",
    "FLINKSQL_RUNNER_URL": "http://flink.test",
    "FLINKSQL_SERVICE_JWT_SECRET": "flink-test-secret-0123456789abcdef012",
    "INBOUND_SERVICE_JWT_SECRET": "inbound-test-secret-0123456789abcdef",
})
for _k in ("INTERNAL_SERVICE_TOKEN", "UCS_INTERNAL_TOKEN", "DBTRUNNER_INTERNAL_TOKEN",
           "FLINKSQL_INTERNAL_TOKEN"):
    os.environ.pop(_k, None)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.auth import Principal  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.db.migration_url import sync_migration_url

TEST_ORG = "test-org"

# Every application table, children first (TRUNCATE ... CASCADE anyway).
APP_TABLES = (
    "dq_service_token_jti", "dq_outbox", "dq_artifact_applications", "dq_artifact_versions", "dq_job_bindings",
    "dq_ruleset_items", "dq_rulesets", "dq_rules",
)


def make_principal(*, org_id: str = TEST_ORG, roles: list[str] | None = None,
                   permissions: list[str] | None = None, user_id: str = "u1") -> Principal:
    return Principal(user_id=user_id, org_id=org_id,
                     roles=["admin"] if roles is None else roles,
                     email=f"{user_id}@example.com", issued_at=0, expires_at=9999999999,
                     permissions=frozenset(permissions or []))


# ── DB fixtures (opt-in) ─────────────────────────────────────────────────

def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = url
    return cfg


def reset_schema(url: str) -> None:
    """DROP + recreate the public schema. Re-checks the guard first."""
    from sqlalchemy import create_engine, text

    check_test_database_url(url)
    eng = create_engine(sync_migration_url(url), isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        eng.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    if _TEST_DB_URL is None:
        pytest.skip("TEST_DATABASE_URL not set — DB-backed tests need a throwaway *_test Postgres")
    return _TEST_DB_URL


@pytest.fixture(scope="session")
def migrated_db(test_database_url):
    """Schema built once per session by the real migrations; dropped at end."""
    from alembic import command

    reset_schema(test_database_url)
    command.upgrade(alembic_config(test_database_url), "head")
    yield test_database_url
    reset_schema(test_database_url)


@pytest.fixture
async def db(migrated_db):
    """A clean, migrated database for one test (all app tables truncated)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    check_test_database_url(migrated_db)
    eng = create_async_engine(migrated_db, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} CASCADE"))
    await eng.dispose()
    from app.db.session import AsyncSessionLocal
    yield AsyncSessionLocal


# ── Auth / client ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _auth_override():
    app.dependency_overrides[get_current_user] = lambda: make_principal()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def as_principal():
    """Swap the authenticated principal for the rest of the test."""
    def _set(principal: Principal) -> None:
        app.dependency_overrides[get_current_user] = lambda: principal
    return _set


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": "Bearer user-token"})
