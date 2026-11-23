"""
BUG-03: a connection, its secret and its secret_ref are never left half done.

Profile + encrypted secret + secret_ref are written on ONE session and committed ONCE (PostgreSQL is the only secret backend).

These run against the in-memory fakes (conftest.py). That a failure really rolls the profile back is proven against a real database
in tests/test_connection_atomicity_real_db.py.
"""

from typing import Any

import pytest
from conftest import _FakeSession

import app.services.connection_service as connection_service_module
import app.services.secret_store as secret_store_module
from app.services.connection_service import connection_service

ORG = "org_prov"
MARKER = "PROV_TEST_SECRET_3e7a91c25b8d4f60a2c1e9d83b7f4a56"
PAYLOAD = {
    "name": "erp-db", "source_type": "postgresql",
    "connection_config": {"hostname": "db.internal", "port": 5432, "database": "erp", "username": "svc", "password": MARKER},
}


class RecordingSession(_FakeSession):
    """Counts commits and remembers each instance, so a test can see how many sessions and commits a create used."""

    instances: list["RecordingSession"] = []

    def __init__(self) -> None:
        self.commits = 0
        RecordingSession.instances.append(self)

    async def commit(self) -> None:
        self.commits += 1


class MemorySecretTable:
    """The PostgreSQL secret store: writes on the CALLER's session (put_in_session) and records which session that was."""

    def __init__(self) -> None:
        self.by_connection: dict[str, dict[str, Any]] = {}
        self.sessions_used: list[Any] = []

    async def put_in_session(self, session: Any, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        self.sessions_used.append(session)
        self.by_connection[connection_id] = dict(fields)
        return f"secret://dataconnect/connections/{connection_id}"

    async def put(self, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        raise AssertionError("PostgreSQL storage must write through put_in_session, on the transaction's own session")

    async def get(self, secret_ref: str | None) -> dict[str, Any] | None:
        return self.by_connection.get((secret_ref or "").rsplit("/", 1)[-1])

    async def delete(self, secret_ref: str | None) -> None:
        # reached only through delete_in_session: a delete must never be a separate step after the profile's commit
        self.by_connection.pop((secret_ref or "").rsplit("/", 1)[-1], None)

    async def delete_in_session(self, session: Any, secret_ref: str | None) -> None:
        self.sessions_used.append(session)
        await self.delete(secret_ref)


@pytest.fixture
def table(monkeypatch) -> MemorySecretTable:
    store = MemorySecretTable()
    monkeypatch.setattr(connection_service_module, "secret_store", store)
    monkeypatch.setattr(secret_store_module, "secret_store", store)
    return store


# ── one transaction ────────────────────────────────────────────────────────────────────────────────────


async def test_with_postgresql_storage_the_profile_secret_and_reference_share_one_session_and_one_commit(
    fake_postgres, table, monkeypatch,
) -> None:
    RecordingSession.instances.clear()
    monkeypatch.setattr(connection_service_module, "AsyncSessionLocal", lambda: RecordingSession())

    created = await connection_service.create_connection(ORG, dict(PAYLOAD, connection_config=dict(PAYLOAD["connection_config"])))

    assert len(RecordingSession.instances) == 1                      # one session for the whole creation
    assert RecordingSession.instances[0].commits == 1                # committed exactly once
    assert table.sessions_used == [RecordingSession.instances[0]]    # the secret was written on THAT session
    assert created["secret_ref"] == f"secret://dataconnect/connections/{created['id']}"
    assert table.by_connection[created["id"]]["password"] == MARKER


async def test_with_postgresql_storage_an_update_is_also_one_session_and_one_commit(fake_postgres, table, monkeypatch) -> None:
    created = await connection_service.create_connection(ORG, dict(PAYLOAD, connection_config=dict(PAYLOAD["connection_config"])))
    RecordingSession.instances.clear()
    table.sessions_used.clear()
    monkeypatch.setattr(connection_service_module, "AsyncSessionLocal", lambda: RecordingSession())

    await connection_service.update_connection(ORG, created["id"], {
        "name": "erp-db-2", "source_type": "postgresql",
        "connection_config": {"hostname": "db2.internal", "port": 5432, "database": "erp", "username": "svc", "password": "new-" + MARKER},
    })

    assert len(RecordingSession.instances) == 1
    assert RecordingSession.instances[0].commits == 1
    assert table.sessions_used == [RecordingSession.instances[0]]
    assert table.by_connection[created["id"]]["password"] == "new-" + MARKER


async def test_a_creation_with_no_secret_needs_no_secret_store_at_all(fake_postgres, table) -> None:
    created = await connection_service.create_connection(ORG, {
        "name": "files", "source_type": "file", "connection_config": {"path": "/data/in"},
    })
    assert created["secret_ref"] is None
    assert table.by_connection == {}


async def test_a_delete_removes_the_profile_and_the_secret_in_one_session_and_one_commit(fake_postgres, table, monkeypatch) -> None:
    created = await connection_service.create_connection(ORG, dict(PAYLOAD, connection_config=dict(PAYLOAD["connection_config"])))
    RecordingSession.instances.clear()
    table.sessions_used.clear()
    monkeypatch.setattr(connection_service_module, "AsyncSessionLocal", lambda: RecordingSession())

    await connection_service.delete_connection(ORG, created["id"])

    assert len(RecordingSession.instances) == 1
    assert RecordingSession.instances[0].commits == 1                # no second durable step after the profile's commit
    assert table.sessions_used == [RecordingSession.instances[0]]    # the secret was removed on that same session
    assert table.by_connection == {}
