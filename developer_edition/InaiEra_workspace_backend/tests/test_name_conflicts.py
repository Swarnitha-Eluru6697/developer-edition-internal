"""W-022: renaming or creating onto a taken name is a stable 409 CONFLICT, never an internal error.

The model has unique (owner, name) for workspaces, (workspace, path, branch) for files and (org, name) for connections; the service
updated the name and flushed, so the database's own error surfaced as a 500. One helper (`flush_or_conflict`) now turns the violation
into the standard 409 — after a friendly up-front check, with the constraint as the backstop for races — for every such write; the
Flink and dbt runners do the same for their updates (their own tests).
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import WorkspaceError, flush_or_conflict
from app.schemas.workspace import WorkspaceCreate, WorkspaceUpdate
from app.services.workspace_service import WorkspaceService


class _PgUnique(Exception):
    """What asyncpg raises for a unique violation: SQLSTATE 23505 and the constraint's name."""
    sqlstate = "23505"

    def __init__(self, constraint_name):
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


class Result:
    def __init__(self, hit): self._hit = hit
    def first(self): return (1,) if self._hit else None
    def scalar_one_or_none(self): return 1 if self._hit else None


class FakeDB:
    def __init__(self, *, name_taken=False, flush_fails=False, workspace=None, constraint="uq_workspace_owner_name"):
        self.name_taken, self.flush_fails, self.ws, self.constraint = name_taken, flush_fails, workspace, constraint
        self.rolled_back = self.flushed = 0

    async def get(self, model, key): return self.ws
    async def execute(self, stmt): return Result(self.name_taken)
    def add(self, row): pass

    async def flush(self):
        self.flushed += 1
        if self.flush_fails:
            raise IntegrityError("INSERT", {}, _PgUnique(self.constraint))

    async def rollback(self): self.rolled_back += 1
    async def refresh(self, row): pass


USER = types.SimpleNamespace(id=uuid.uuid4())


def _ws():
    return types.SimpleNamespace(id=uuid.uuid4(), owner_id=USER.id, name="mine", description=None,
                                 default_warehouse=None, default_kernel_image=None)


def _run(coro):
    return asyncio.run(coro)


def _conflict(exc):
    assert exc.value.status_code == 409 and exc.value.detail["code"] in ("WORKSPACE_NAME_TAKEN", "FILE_PATH_TAKEN", "CONNECTION_NAME_TAKEN")


def test_renaming_a_workspace_onto_an_existing_name_is_a_409():
    ws = _ws()
    db = FakeDB(name_taken=True, workspace=ws)
    with pytest.raises(WorkspaceError) as exc:
        _run(WorkspaceService(db).update(USER, ws.id, WorkspaceUpdate(name="taken")))
    _conflict(exc)
    assert ws.name == "mine" and db.flushed == 0                  # caught by the up-front check: nothing was changed or flushed


def test_a_rename_that_loses_a_race_is_a_409_and_the_transaction_is_rolled_back():
    ws = _ws()
    db = FakeDB(flush_fails=True, workspace=ws)
    with pytest.raises(WorkspaceError) as exc:
        _run(WorkspaceService(db).update(USER, ws.id, WorkspaceUpdate(name="raced")))
    _conflict(exc)
    assert db.rolled_back == 1 and "raced" in exc.value.detail["message"]


def test_renaming_to_the_same_name_or_changing_other_fields_is_not_a_conflict():
    ws = _ws()
    db = FakeDB(name_taken=True, workspace=ws)                     # "taken" must not matter when the name is not changing
    out = _run(WorkspaceService(db).update(USER, ws.id, WorkspaceUpdate(name="mine", description="d")))
    assert out.description == "d" and db.flushed == 1


def test_creating_a_workspace_that_loses_a_race_is_a_409():
    db = FakeDB(flush_fails=True)
    with pytest.raises(WorkspaceError) as exc:
        _run(WorkspaceService(db).create(USER, WorkspaceCreate(name="raced")))
    _conflict(exc)


def test_the_helper_passes_other_errors_through_and_flushes_when_there_is_no_conflict():
    ok = FakeDB()
    _run(flush_or_conflict(ok, "x"))
    assert ok.flushed == 1 and ok.rolled_back == 0

    class Boom(FakeDB):
        async def flush(self): raise RuntimeError("not a uniqueness problem")

    with pytest.raises(RuntimeError):
        _run(flush_or_conflict(Boom(), "x"))


def test_a_new_file_that_loses_the_race_for_its_path_is_a_409(monkeypatch):
    from app.schemas.file import FileCreate
    from app.services.file_service import FileService

    ws = types.SimpleNamespace(id=uuid.uuid4(), owner_id=USER.id, storage_prefix="p", org_id="o")

    class Storage:
        async def put(self, key, data, content_type=None): return "a" * 64

    import app.services.file_service as fs
    monkeypatch.setattr(fs, "get_blob_store", lambda: Storage())
    db = FakeDB(flush_fails=True, constraint="uq_file_path_branch")
    svc = FileService(db, "o")
    with pytest.raises(WorkspaceError) as exc:
        _run(svc.create(ws, FileCreate(path="models/a.sql", language="sql", content="select 1", branch="main")))
    _conflict(exc)
    assert "models/a.sql" in exc.value.detail["message"]


def _drop_user_rows(subject: str) -> None:
    """Remove the test user (and, by cascade, its workspaces) directly: deleting through the API needs object storage."""
    import asyncio
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def go():
        engine = create_async_engine(os.environ["INTEGRATION_DATABASE_URL"], poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("delete from users where external_id = :s"), {"s": subject})
        await engine.dispose()

    asyncio.run(go())


@pytest.mark.integration
def test_workspace_rename_and_conflict_against_a_real_database():
    """A successful rename answers 200 (not MissingGreenlet), and a rename onto an existing name is a 409 — with a real session."""
    import os

    from conftest import _headers
    from fastapi.testclient import TestClient

    from app.main import app

    if not os.environ.get("INTEGRATION_DATABASE_URL"):
        pytest.skip("needs the integration database")
    c = TestClient(app)
    h = _headers(user_name=f"rename-{uuid.uuid4().hex[:6]}", roles=["workspace_admin"])
    a = c.post("/api/v1/workspaces", json={"name": "alpha"}, headers=h)
    b = c.post("/api/v1/workspaces", json={"name": "beta"}, headers=h)
    if a.status_code != 201 or b.status_code != 201:
        pytest.skip(f"workspace creation needs object storage in this environment: {a.status_code} {b.status_code}")
    try:
        ok = c.patch(f"/api/v1/workspaces/{b.json()['id']}", json={"name": "gamma"}, headers=h)
        assert ok.status_code == 200 and ok.json()["name"] == "gamma", ok.text
        clash = c.patch(f"/api/v1/workspaces/{b.json()['id']}", json={"name": "alpha"}, headers=h)
        assert clash.status_code == 409 and clash.json()["detail"]["code"] in ("WORKSPACE_NAME_TAKEN", "FILE_PATH_TAKEN", "CONNECTION_NAME_TAKEN")
    finally:
        _drop_user_rows(h["X-User-Id"])
