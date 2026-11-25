"""WS-SEC-002: connection credentials live only in the secrets backend, keyed
per connection (never overwritten by a name clash), removed with their
connection, and legacy plaintext files migrate without ever losing an entry."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.storage as storage_pkg
from app.core.deps import get_db
from app.main import app
from app.models import DatabaseConnection
from app.services.workspace_service import WorkspaceService
from app.storage import secrets as secrets_mod

from conftest import _headers


class FakeBackend:
    def __init__(self, fail_put=(), fail_delete=False):
        self.data: dict = {}
        self.fail_put = set(fail_put)
        self.fail_delete = fail_delete

    async def get(self, path):
        return self.data.get(path)

    async def put(self, path, data):
        if path in self.fail_put:
            raise RuntimeError("backend down")
        self.data[path] = data

    async def delete(self, path):
        if self.fail_delete:
            raise RuntimeError("backend down")
        self.data.pop(path, None)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []

    async def execute(self, stmt, *a, **k):
        # Only the name-uniqueness probe hits execute() in these routes.
        params = stmt.compile().params
        name = next((v for k, v in params.items() if k.startswith("name")), None)
        hit = any(r.name == name for r in self.rows.values())
        return SimpleNamespace(first=lambda: (1,) if hit else None)

    async def get(self, model, key):
        row = self.rows.get(key)
        return row if isinstance(row, model) else None

    def add(self, row):
        # What Postgres server defaults would fill in.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for attr, val in (("is_verified", False), ("created_at", now), ("updated_at", now),
                          ("extra_properties", {})):
            if getattr(row, attr, None) is None:
                setattr(row, attr, val)
        self.rows[row.id] = row

    async def delete(self, row):
        self.deleted.append(row.id)

    async def flush(self):
        pass

    async def refresh(self, row):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.fixture
def env(monkeypatch):
    rows: dict = {}
    backend = FakeBackend()
    db = FakeDB(rows)

    async def _db():
        yield db

    async def _ensure_user(self, principal):
        return SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa", external_id=principal.user_id)
    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(storage_pkg, "get_secrets_backend", lambda: backend)
    from app.services import audit_service

    async def _no_audit(self, **_k):
        pass
    monkeypatch.setattr(audit_service.AuditService, "record", _no_audit)
    yield TestClient(app), rows, backend, db
    app.dependency_overrides.clear()


ADMIN = {"roles": ["workspace_admin"]}
BODY = {"name": "sales", "connection_type": "postgres", "host": "db", "port": 5432,
        "database_name": "sales", "username": "u", "password": "p", "use_tls": False}


def test_create_stores_secret_under_the_connections_own_id(env):
    client, rows, backend, _ = env
    r = client.post("/api/v1/connections", json=BODY, headers=_headers(**ADMIN))
    assert r.status_code == 201, r.text
    conn_id = r.json()["id"]
    (path,) = backend.data
    assert path.endswith(f"/connections/local-dev/{conn_id}") or path.endswith(f"/{conn_id}")
    assert backend.data[path] == {"username": "u", "password": "p"}
    assert rows[next(iter(rows))].vault_secret_path == path


def test_duplicate_name_is_409_and_never_touches_the_existing_secret(env):
    client, rows, backend, _ = env
    assert client.post("/api/v1/connections", json=BODY, headers=_headers(**ADMIN)).status_code == 201
    before = dict(backend.data)
    r = client.post("/api/v1/connections", json={**BODY, "password": "attacker"}, headers=_headers(**ADMIN))
    assert r.status_code == 409
    assert backend.data == before            # original credentials untouched


def test_delete_removes_the_secret(env):
    client, rows, backend, db = env
    conn_id = client.post("/api/v1/connections", json=BODY, headers=_headers(**ADMIN)).json()["id"]
    assert backend.data
    assert client.delete(f"/api/v1/connections/{conn_id}", headers=_headers(**ADMIN)).status_code == 204
    assert backend.data == {}


def test_delete_fails_closed_if_the_secret_cannot_be_removed(env):
    client, rows, backend, db = env
    conn_id = client.post("/api/v1/connections", json=BODY, headers=_headers(**ADMIN)).json()["id"]
    backend.fail_delete = True
    r = client.delete(f"/api/v1/connections/{conn_id}", headers=_headers(**ADMIN))
    assert r.status_code == 502            # request fails → row delete rolls back


# ── Legacy plaintext migration ───────────────────────────────────────────

def _run_migration(monkeypatch, tmp_path, backend):
    monkeypatch.setattr(secrets_mod, "get_settings", lambda: SimpleNamespace(secrets_backend="aws_secrets_manager"))
    monkeypatch.setattr(secrets_mod, "get_secrets_backend", lambda: backend)
    return asyncio.run(secrets_mod.migrate_legacy_plaintext_credentials(str(tmp_path)))


def test_full_migration_removes_the_plaintext_files(monkeypatch, tmp_path):
    (tmp_path / ".db-credentials.json").write_text(json.dumps({"a": {"username": "u", "password": "p"}}))
    (tmp_path / ".git-credentials.json").write_text(json.dumps({"user1:github": "ghp_x"}))
    backend = FakeBackend()
    report = _run_migration(monkeypatch, tmp_path, backend)
    assert report == {"db": {"migrated": 1, "remaining": 0}, "git": {"migrated": 1, "remaining": 0}}
    assert not (tmp_path / ".db-credentials.json").exists()
    assert not (tmp_path / ".git-credentials.json").exists()
    assert backend.data["a"] == {"username": "u", "password": "p"}


def test_partial_failure_keeps_exactly_the_failed_entries(monkeypatch, tmp_path):
    f = tmp_path / ".db-credentials.json"
    f.write_text(json.dumps({"ok": {"username": "u"}, "bad": {"username": "v"}}))
    report = _run_migration(monkeypatch, tmp_path, FakeBackend(fail_put={"bad"}))
    assert report["db"] == {"migrated": 1, "remaining": 1}
    assert json.loads(f.read_text()) == {"bad": {"username": "v"}}   # nothing lost
    # A retry with the backend healthy finishes the job.
    report = _run_migration(monkeypatch, tmp_path, FakeBackend())
    assert report["db"] == {"migrated": 1, "remaining": 0} and not f.exists()


def test_unreadable_file_is_left_in_place(monkeypatch, tmp_path):
    f = tmp_path / ".git-credentials.json"
    f.write_text("{not json")
    report = _run_migration(monkeypatch, tmp_path, FakeBackend())
    assert "error" in report["git"] and f.read_text() == "{not json"
