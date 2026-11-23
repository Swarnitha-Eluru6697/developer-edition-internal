"""
GAP-05 against a REAL PostgreSQL: the secret store's readiness check must work on the real schema (`connection_id` is a UUID column, which
an in-memory fake cannot object to).

    DATACONNECT_DATABASE_URL=postgresql+asyncpg://dataconnect:dataconnect@localhost:5435/dataconnect \\
    DATACONNECT_REAL_DB_TESTS=1 .venv/bin/python -m pytest tests/test_secret_store_real_db.py --no-cov

Read-only: writes nothing.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")


@pytest.fixture
async def engine_reset():
    from app.db.session import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_the_secret_store_is_ready_against_the_real_schema(engine_reset) -> None:
    from app.services.secret_store import PostgresSecretStore

    assert await PostgresSecretStore().check_ready() == {"ok": True, "backend": "postgres"}


async def test_it_is_not_ready_against_the_real_schema_without_an_encryption_key(engine_reset, monkeypatch) -> None:
    from app.core.config import settings
    from app.services.secret_store import PostgresSecretStore

    monkeypatch.setattr(settings, "secret_encryption_key", "")

    assert await PostgresSecretStore().check_ready() == {"ok": False, "backend": "postgres", "error": "SecretEncryptionError"}
