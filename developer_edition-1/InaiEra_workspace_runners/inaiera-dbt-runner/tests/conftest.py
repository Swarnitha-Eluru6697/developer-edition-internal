"""Pytest fixtures for the dbt Runner service.

These fixtures set up a real RSA key pair, a mock JWKS endpoint, and a JWT
minted with the same kid so tests can exercise the full auth path without
ever talking to the real control plane.

DATABASE SAFETY
---------------
DB-backed tests (the `run_db` fixture and anything using `test_database_url`)
run ONLY against the database named by the TEST_DATABASE_URL environment
variable, and only if that database's name ends in `_test`:

  * TEST_DATABASE_URL unset         -> those tests are SKIPPED.
  * database name not `*_test`      -> the whole session FAILS loudly.

There is deliberately no default/fallback DSN: the schema is dropped at the
end of the session, so pointing this at a real database would destroy it.
Start a throwaway Postgres for the suite, e.g.

    docker run -d --rm --name dbt-runner-testdb -e POSTGRES_USER=test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=dbtrunner_test postgres:16-alpine
    TEST_DATABASE_URL=postgresql+asyncpg://test:test@<host>:5432/dbtrunner_test pytest

The schema is created by running the real Alembic migrations (`upgrade
head`), so every DB-backed test also exercises the migrations.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

# Set env BEFORE importing the app so Settings picks it up. The app's own
# DATABASE_URL is an in-memory SQLite placeholder: nothing the app does at
# import time may ever reach a real Postgres.
os.environ["ENVIRONMENT"] = "dev"
os.environ.setdefault("ORG_ID", "00000000-0000-0000-0000-000000000001")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ.pop("DATABASE_HOST", None)
os.environ.setdefault("RUNNER_MODE", "subprocess")
# Registration normally clones the repo to check the project name; tests use fake URLs.
os.environ.setdefault("VALIDATE_PROJECT_ON_REGISTER", "false")
os.environ.setdefault("CONTROL_PLANE_JWKS_URL", "http://test/jwks")
os.environ.setdefault("CONTROL_PLANE_ISSUER", "http://test")
os.environ.setdefault("CONTROL_PLANE_AUDIENCE", "dbtrunner")
os.environ.setdefault("ARTIFACT_STORE_BACKEND", "none")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.auth import _jwks_cache  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

TEST_ORG_ID = os.environ["ORG_ID"]
# What every fake Git host "resolves" a ref to (DBT-006); also what the fake clones report as their HEAD.
FAKE_COMMIT_SHA = "c0ffee" + "0" * 34
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# Test-database guard
# ---------------------------------------------------------------------
def _guarded_test_database_url() -> str | None:
    """TEST_DATABASE_URL, validated. None when unset (callers skip).
    Aborts the whole session if it names a database that is not *_test."""
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        return None
    try:
        url = make_url(raw)
    except Exception as e:  # noqa: BLE001
        pytest.exit(f"REFUSING TO RUN: TEST_DATABASE_URL is not a valid URL ({e})", returncode=3)
    if not url.drivername.startswith("postgresql"):
        pytest.exit(f"REFUSING TO RUN: TEST_DATABASE_URL must be Postgres, got {url.drivername}",
                    returncode=3)
    if not (url.database or "").endswith("_test"):
        pytest.exit(
            "\n\n*** REFUSING TO RUN DB TESTS ***\n"
            f"TEST_DATABASE_URL points at database '{url.database}', whose name does not end in "
            "'_test'.\nThe test suite DROPS the schema of the database it is given. Use a throwaway "
            "database named '*_test'.\n",
            returncode=3,
        )
    return raw


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _guarded_test_database_url()
    if url is None:
        pytest.skip("TEST_DATABASE_URL not set — DB-backed tests need a throwaway *_test Postgres")
    return url


def _alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = url
    return cfg


def _reset_schema_sync(url: str) -> None:
    """DROP SCHEMA public CASCADE on the (already-verified *_test) DB."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    assert (make_url(url).database or "").endswith("_test"), "refusing to drop a non-_test schema"

    async def _go():
        eng = create_async_engine(url)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(_go())


@pytest.fixture(scope="session")
def migrated_test_database(test_database_url) -> str:
    """Fresh schema built by `alembic upgrade head` once per session, and
    dropped again at the end — only ever on the guarded *_test database."""
    from alembic import command

    _reset_schema_sync(test_database_url)
    command.upgrade(_alembic_config(test_database_url), "head")
    yield test_database_url
    _reset_schema_sync(test_database_url)


_APP_TABLES = (
    "service_token_jti", "metadata_outbox_events", "scheduled_occurrences",
    "lineage_edges", "run_events", "runs", "project_dependents", "projects", "git_credentials",
)


@pytest_asyncio.fixture
async def run_db(migrated_test_database, monkeypatch):
    """A real, migrated Postgres (the guarded *_test database) for tests
    that exercise actual row transitions. Every table is truncated before
    each test so tests never see each other's rows."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    test_engine = create_async_engine(migrated_test_database, future=True)
    async with test_engine.begin() as conn:
        existing = {r[0] for r in await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        tables = [t for t in _APP_TABLES if t in existing]
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))

    test_sessionmaker = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)
    # Modules that bound AsyncSessionLocal at import time need patching
    # directly; everything else resolves app.db.session.AsyncSessionLocal
    # at call time.
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", test_sessionmaker)
    monkeypatch.setattr("app.db.session.engine", test_engine)
    for mod in ("app.services.run_reconciler", "app.services.scheduler",
                "app.services.metadata_outbox", "app.core.deps", "app.main"):
        monkeypatch.setattr(f"{mod}.AsyncSessionLocal", test_sessionmaker, raising=False)

    yield test_sessionmaker

    await test_engine.dispose()


# ---------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------
@pytest.fixture(scope="session")
def rsa_keys() -> tuple[str, str, dict]:
    """Generate one RSA keypair for the whole test session.

    Returns (private_pem, public_pem, jwk_dict_with_kid).
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    pub_jwk = jwk.construct(public_pem, "RS256").to_dict()
    pub_jwk["kid"] = "test-kid-1"
    pub_jwk["alg"] = "RS256"
    pub_jwk["use"] = "sig"
    return private_pem, public_pem, pub_jwk


@pytest.fixture(autouse=True)
def no_real_git_resolution(monkeypatch):
    """Submitting a run resolves the project's ref with `git ls-remote` (DBT-006). Tests register fake repository
    URLs, so none may reach a real Git host: every ref resolves to FAKE_COMMIT_SHA. tests/test_run_manifest.py
    exercises the real resolver against local repositories."""
    async def _resolve(git_url, git_ref, token=None):
        return FAKE_COMMIT_SHA
    monkeypatch.setattr("app.services.run_manifest.resolve_commit", _resolve)


@pytest.fixture(autouse=True)
def prime_jwks_cache(rsa_keys, monkeypatch):
    """Stuff our test public key directly into the JWKS cache so the
    validator finds it without hitting the network."""
    _, _, pub_jwk = rsa_keys
    _jwks_cache._keys = {pub_jwk["kid"]: pub_jwk}
    _jwks_cache._fetched_at = time.time()
    yield
    _jwks_cache._keys = {}
    _jwks_cache._fetched_at = 0.0


def _mint(
    rsa_keys: tuple[str, str, dict],
    *,
    user_id: str | None = None,
    permissions: list[str] | None = None,
    org_id: str | None = None,
    audience: str | None = None,
    scopes: list[str] | None = None,
    expires_in: int = 900,
) -> str:
    private_pem, _, pub_jwk = rsa_keys
    settings = get_settings()
    now = int(time.time())
    claims = {
        "iss": settings.control_plane_issuer,
        "aud": audience or settings.control_plane_audience,
        "sub": user_id or str(uuid.uuid4()),
        "org_id": org_id or settings.org_id,
        "email": "dev@example.com",
        "permissions": list(permissions or []),
        "iat": now,
        "exp": now + expires_in,
    }
    if scopes is not None:
        claims["scope"] = " ".join(scopes)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": pub_jwk["kid"]})


@pytest.fixture
def mint_jwt(rsa_keys):
    """Factory fixture: tests call mint_jwt(permissions=[...]) to get a valid token."""
    def _factory(**kwargs) -> str:
        return _mint(rsa_keys, **kwargs)
    return _factory


@pytest.fixture
def auth_headers(mint_jwt) -> dict[str, str]:
    # A fully authorised caller: the runner's audience, every user-route scope, and every job permission
    # (DBT-001: scopes alone are not enough).
    from tests.perms import ALL_JOB
    scopes = ["dbt.projects.read", "dbt.projects.write", "dbt.runs.read", "dbt.runs.trigger"]
    return {"Authorization": f"Bearer {mint_jwt(scopes=scopes, permissions=ALL_JOB)}"}


@pytest.fixture
def client() -> TestClient:
    """Synchronous test client (lifespan NOT started, so no background
    loops run during tests)."""
    return TestClient(app)
