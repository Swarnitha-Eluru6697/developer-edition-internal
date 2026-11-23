"""
BUG-03 against a REAL PostgreSQL: a failure while creating or updating a connection's secret rolls the profile back with it.

The in-memory fakes in the other tests cannot prove a rollback (a fake "commit" has nothing to undo), so these run against a real
database and are skipped unless one is provided. Run them with the dev stack up:

    DATACONNECT_DATABASE_URL=postgresql+asyncpg://dataconnect:dataconnect@localhost:5435/dataconnect \\
    DATACONNECT_REAL_DB_TESTS=1 .venv/bin/python -m pytest tests/test_connection_atomicity_real_db.py --no-cov

They create rows under the existing organization `org_1001` with a unique name and delete everything they created.
"""

import os
import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

ORG = "org_1001"
MARKER = "REALDB_SECRET_c41d7e02f9a8"


def _payload(name: str, **config) -> dict:
    return {
        "name": name, "source_type": "postgresql",
        "connection_config": {"hostname": "h", "port": 5432, "database": "d", "username": "u", "password": MARKER, **config},
    }


@pytest.fixture
async def real_db():
    from app.db.session import AsyncSessionLocal, engine
    from app.services.connection_service import connection_service

    await engine.dispose()   # each test has its own event loop; a pooled asyncpg connection from the last one cannot be reused
    name = f"atomicity-{uuid.uuid4().hex[:10]}"

    async def scalar(sql: str, **params):
        async with AsyncSessionLocal() as session:
            return (await session.execute(text(sql), params)).scalar()

    async def profile_count() -> int:
        return await scalar("select count(*) from dc_connection_profiles where name like :n", n=f"{name}%")

    async def secret_rows() -> int:
        return await scalar(
            "select count(*) from dc_connection_secrets s join dc_connection_profiles p on p.id = s.connection_id where p.name like :n",
            n=f"{name}%",
        )

    async def orphan_secret_rows() -> int:   # secret rows whose profile is gone cannot exist (FK); count marker-bearing ones anyway
        return await scalar("select count(*) from dc_connection_secrets where values::text like :m", m=f"%{MARKER}%")

    yield name, connection_service, profile_count, secret_rows, orphan_secret_rows
    async with AsyncSessionLocal() as session:
        await session.execute(text("delete from dc_connection_profiles where name like :n"), {"n": f"{name}%"})
        await session.commit()
    await engine.dispose()


async def test_creation_commits_the_profile_the_encrypted_secret_and_the_reference_together(real_db) -> None:
    name, service, profile_count, secret_rows, _ = real_db

    created = await service.create_connection(ORG, _payload(name))

    assert await profile_count() == 1
    assert await secret_rows() == 1
    assert created["secret_ref"] == f"secret://dataconnect/connections/{created['id']}"
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        stored = (await session.execute(text("select values::text from dc_connection_secrets where connection_id = :i"), {"i": created["id"]})).scalar()
    assert MARKER not in stored                                      # encrypted at rest, not readable JSON


async def test_a_failure_writing_the_secret_rolls_the_profile_back(real_db, monkeypatch) -> None:
    name, service, profile_count, secret_rows, _ = real_db
    from app.services.secret_store import PostgresSecretStore

    async def failing_put(self, session, **kwargs):
        raise RuntimeError("secret write failed")

    monkeypatch.setattr(PostgresSecretStore, "put_in_session", failing_put)

    with pytest.raises(RuntimeError, match="secret write failed"):
        await service.create_connection(ORG, _payload(name))

    assert await profile_count() == 0                                # no profile survived without its secret
    assert await secret_rows() == 0


async def test_a_failure_recording_the_reference_rolls_back_the_profile_and_the_secret(real_db, monkeypatch) -> None:
    name, service, profile_count, secret_rows, orphan_secret_rows = real_db
    from app.repositories.dataconnect import ConnectionProfileRepository

    real_update = ConnectionProfileRepository.update

    async def failing_reference_update(self, row, values):
        if "secret_ref" in values:
            raise RuntimeError("could not record the reference")
        return await real_update(self, row, values)

    monkeypatch.setattr(ConnectionProfileRepository, "update", failing_reference_update)

    with pytest.raises(RuntimeError, match="could not record the reference"):
        await service.create_connection(ORG, _payload(name))

    assert await profile_count() == 0
    assert await secret_rows() == 0
    assert await orphan_secret_rows() == 0                           # the encrypted secret written a moment earlier is gone too


async def test_a_failed_update_leaves_both_the_profile_and_the_secret_as_they_were(real_db, monkeypatch) -> None:
    name, service, profile_count, secret_rows, _ = real_db
    from app.db.session import AsyncSessionLocal
    from app.services.secret_store import PostgresSecretStore

    created = await service.create_connection(ORG, _payload(name))

    async def failing_put(self, session, **kwargs):
        raise RuntimeError("secret write failed")

    monkeypatch.setattr(PostgresSecretStore, "put_in_session", failing_put)
    with pytest.raises(RuntimeError, match="secret write failed"):
        await service.update_connection(ORG, created["id"], _payload(name + "-renamed", hostname="changed-host", password="CHANGED"))

    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("select name, connection_config ->> 'hostname' from dc_connection_profiles where id = :i"), {"i": created["id"]})).one()
    assert row[0] == name                                            # the rename was rolled back
    assert row[1] == "h"                                             # and so was the new host


# ── BUG-03 verification: inject a failure after EVERY step and prove no connection exists without a valid secret ────────────────────

async def _connections_without_a_valid_secret(name: str) -> list[str]:
    """The invariant, asked of the real database: every profile created by a test has a secret_ref AND a stored secret that
    decrypts (AES-GCM authenticates the row, so this fails on a missing, truncated or foreign envelope) to the password that was given."""
    from app.db.session import AsyncSessionLocal
    from app.services.secret_store import secret_store

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text("select id, name, secret_ref from dc_connection_profiles where name like :n"), {"n": f"{name}%"})).all()
    broken = []
    for cid, pname, ref in rows:
        secret = await secret_store.get(ref) if ref else None
        if not ref or not secret or secret.get("password") not in (MARKER, "CHANGED"):
            broken.append(f"{pname} ({cid}): ref={ref!r}, secret={'present' if secret else 'MISSING'}")
    return broken


@pytest.fixture
def inject(monkeypatch):
    """inject("<step>") makes the named step of create/update raise AFTER it has done its work, so a rollback has something to undo."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.repositories.dataconnect import ConnectionProfileRepository
    from app.services.secret_store import PostgresSecretStore

    real_create, real_update = ConnectionProfileRepository.create, ConnectionProfileRepository.update
    real_put, real_commit = PostgresSecretStore.put_in_session, AsyncSession.commit
    armed: dict[str, bool] = {}

    async def create(self, payload):
        row = await real_create(self, payload)
        if armed.get("profile_inserted"):
            raise RuntimeError("injected: after the profile insert")
        return row

    async def update(self, row, values):
        result = await real_update(self, row, values)
        step = "reference_recorded" if "secret_ref" in values else "profile_updated"
        if armed.get(step):
            raise RuntimeError(f"injected: after {step}")
        return result

    async def put(self, session, **kwargs):
        ref = await real_put(self, session, **kwargs)
        if armed.get("secret_written"):
            raise RuntimeError("injected: after the secret write")
        return ref

    async def commit(self):
        if armed.get("at_commit"):
            raise RuntimeError("injected: the commit itself failed")
        return await real_commit(self)

    monkeypatch.setattr(ConnectionProfileRepository, "create", create)
    monkeypatch.setattr(ConnectionProfileRepository, "update", update)
    monkeypatch.setattr(PostgresSecretStore, "put_in_session", put)
    monkeypatch.setattr(AsyncSession, "commit", commit)

    def arm(step: str) -> None:
        armed[step] = True

    def disarm() -> None:
        armed.clear()

    arm.disarm = disarm
    return arm


CREATE_STEPS = ["profile_inserted", "secret_written", "reference_recorded", "at_commit"]
UPDATE_STEPS = ["profile_updated", "secret_written", "at_commit"]


@pytest.mark.parametrize("step", CREATE_STEPS)
async def test_a_failure_after_each_creation_step_leaves_no_connection_at_all(real_db, inject, step) -> None:
    name, service, profile_count, secret_rows, orphan_secret_rows = real_db
    inject(step)

    with pytest.raises(RuntimeError, match="injected"):
        await service.create_connection(ORG, _payload(name))

    inject.disarm()
    assert await profile_count() == 0                                # no connection, ACTIVE or otherwise
    assert await secret_rows() == 0
    assert await orphan_secret_rows() == 0                           # and no encrypted secret left behind either
    assert await _connections_without_a_valid_secret(name) == []

    created = await service.create_connection(ORG, _payload(name))   # the same request, retried, then succeeds cleanly
    assert await profile_count() == 1
    assert await secret_rows() == 1
    assert created["secret_ref"]
    assert await _connections_without_a_valid_secret(name) == []


@pytest.mark.parametrize("step", UPDATE_STEPS)
async def test_a_failure_after_each_update_step_leaves_the_connection_and_its_secret_untouched(real_db, inject, step) -> None:
    name, service, *_ = real_db
    from app.db.session import AsyncSessionLocal

    created = await service.create_connection(ORG, _payload(name))
    inject(step)

    with pytest.raises(RuntimeError, match="injected"):
        await service.update_connection(ORG, created["id"], _payload(name + "-renamed", hostname="changed-host", password="CHANGED"))

    inject.disarm()
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "select name, connection_config ->> 'hostname', secret_ref from dc_connection_profiles where id = :i"), {"i": created["id"]})).one()
    assert (row[0], row[1], row[2]) == (name, "h", created["secret_ref"])   # nothing of the failed edit was kept
    assert await _connections_without_a_valid_secret(name) == []             # the stored secret is still the original, valid one


async def test_a_failure_while_deleting_the_secret_keeps_the_connection_whole(real_db, monkeypatch) -> None:
    """GAP-02: the secret is deleted in the same transaction as the profile, so if that step fails the profile is NOT deleted either:
    the connection stays complete and valid, and the delete can simply be retried."""
    name, service, profile_count, secret_rows, _ = real_db
    from app.services.secret_store import PostgresSecretStore

    created = await service.create_connection(ORG, _payload(name))

    async def failing_delete(self, session, secret_ref):
        raise RuntimeError("secret delete failed")

    monkeypatch.setattr(PostgresSecretStore, "delete_in_session", failing_delete)
    with pytest.raises(RuntimeError, match="secret delete failed"):
        await service.delete_connection(ORG, created["id"])

    assert await profile_count() == 1                                # not half-deleted
    assert await _connections_without_a_valid_secret(name) == []
    monkeypatch.undo()
    await service.delete_connection(ORG, created["id"])              # the retry works
    assert await profile_count() == 0
    assert await secret_rows() == 0


async def test_a_failure_at_the_delete_commit_leaves_the_connection_and_its_secret(real_db, inject) -> None:
    name, service, profile_count, secret_rows, _ = real_db
    created = await service.create_connection(ORG, _payload(name))
    inject("at_commit")

    with pytest.raises(RuntimeError, match="injected"):
        await service.delete_connection(ORG, created["id"])

    inject.disarm()
    assert await profile_count() == 1
    assert await secret_rows() == 1
    assert await _connections_without_a_valid_secret(name) == []


async def test_a_successful_delete_leaves_no_secret_behind(real_db) -> None:
    name, service, profile_count, secret_rows, orphan_secret_rows = real_db
    created = await service.create_connection(ORG, _payload(name))

    await service.delete_connection(ORG, created["id"])

    assert await profile_count() == 0
    assert await secret_rows() == 0
    assert await orphan_secret_rows() == 0
