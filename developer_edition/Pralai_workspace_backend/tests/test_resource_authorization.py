"""WS-SEC-001 negative tests: another user's or another org's session /
execution can't be read, have its results fetched, be cancelled, be
subscribed to (WS ticket) or be terminated — and a terminated session
can't be used.

Runs the real routes through TestClient with the real identity dependency;
only the DB session is a fake (the models use Postgres-only types, so there's
no SQLite DB to run them against — see tests/test_workspaces.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.deps import get_db
from app.main import app
from app.models import Execution, Session as SessionModel
from app.services.workspace_service import WorkspaceService

from conftest import _headers

CALLER = uuid.uuid4()
OTHER = uuid.uuid4()


class FakeDB:
    """Just enough AsyncSession for the routes under test."""

    def __init__(self, rows: dict):
        self.rows = rows

    async def get(self, model, key):
        row = self.rows.get(key)
        return row if row is not None and isinstance(row, model) else None

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def add(self, _row):
        pass


def _session(owner, status="active"):
    return SessionModel(id=uuid.uuid4(), user_id=owner, workspace_id=uuid.uuid4(), status=status, runtime_metadata={})


def _execution(owner):
    return Execution(
        id=uuid.uuid4(), user_id=owner, session_id=uuid.uuid4(), workspace_id=uuid.uuid4(),
        language="sql", status="completed", code_text="select 1", code_sha256="x", target="lakehouse",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def api(monkeypatch):
    rows: dict = {}

    async def _db():
        yield FakeDB(rows)

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=CALLER, external_id=principal.user_id)

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    yield TestClient(app), rows
    app.dependency_overrides.clear()


def _add(rows, obj):
    rows[obj.id] = obj
    return obj


# ── Cross-user: every action on someone else's resource is a 404 ─────────

def test_cannot_read_other_users_execution(api):
    client, rows = api
    ex = _add(rows, _execution(OTHER))
    assert client.get(f"/api/v1/executions/{ex.id}", headers=_headers()).status_code == 404


def test_cannot_fetch_other_users_result(api):
    client, rows = api
    ex = _add(rows, _execution(OTHER))
    assert client.get(f"/api/v1/executions/{ex.id}/result", headers=_headers()).status_code == 404


def test_cannot_cancel_other_users_execution(api, monkeypatch):
    client, rows = api
    ex = _add(rows, _execution(OTHER))
    cancelled = []
    import app.api.executions as ex_api

    async def _cancel(eid, lang):
        cancelled.append(eid)
    monkeypatch.setattr(ex_api, "get_execution_router", lambda: SimpleNamespace(cancel=_cancel))
    assert client.post(f"/api/v1/executions/{ex.id}/cancel", headers=_headers()).status_code == 404
    assert cancelled == []


def test_cannot_terminate_other_users_session(api):
    client, rows = api
    s = _add(rows, _session(OTHER))
    assert client.delete(f"/api/v1/sessions/{s.id}", headers=_headers()).status_code == 404
    assert s.status == "active"


def test_cannot_subscribe_to_other_users_session(api):
    client, rows = api
    s = _add(rows, _session(OTHER))
    r = client.post("/api/v1/auth/ws-ticket", json={"session_id": str(s.id)}, headers=_headers())
    assert r.status_code == 404


def test_owner_can_read_own_execution(api):
    client, rows = api
    ex = _add(rows, _execution(CALLER))
    r = client.get(f"/api/v1/executions/{ex.id}", headers=_headers())
    assert r.status_code == 200 and r.json()["id"] == str(ex.id)


# ── Cross-org: a tenant this data plane doesn't serve is refused outright ──

@pytest.mark.parametrize("route", [
    ("get", "/api/v1/executions/{id}"),
    ("get", "/api/v1/executions/{id}/result"),
    ("post", "/api/v1/executions/{id}/cancel"),
    ("delete", "/api/v1/sessions/{id}"),
])
def test_other_org_is_refused_even_for_own_rows(api, route):
    client, rows = api
    ex = _add(rows, _execution(CALLER))
    s = _add(rows, _session(CALLER))
    method, path = route
    target = s.id if "sessions" in path else ex.id
    r = getattr(client, method)(path.format(id=target), headers=_headers(org_id="another-org"))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "TENANT_MISMATCH"


def test_other_org_cannot_mint_ws_ticket(api):
    client, rows = api
    s = _add(rows, _session(CALLER))
    r = client.post("/api/v1/auth/ws-ticket", json={"session_id": str(s.id)}, headers=_headers(org_id="another-org"))
    assert r.status_code == 403


def test_missing_tenant_header_is_refused(api):
    client, rows = api
    ex = _add(rows, _execution(CALLER))
    assert client.get(f"/api/v1/executions/{ex.id}", headers=_headers(org_id=None)).status_code == 403


# ── Session lifecycle: terminated sessions can't run work; idle resumes ───

def test_terminated_session_cannot_get_ws_ticket(api):
    client, rows = api
    s = _add(rows, _session(CALLER, status="terminated"))
    r = client.post("/api/v1/auth/ws-ticket", json={"session_id": str(s.id)}, headers=_headers())
    assert r.status_code == 409


def test_terminated_session_cannot_submit_sql(api):
    client, rows = api
    s = _add(rows, _session(CALLER, status="terminated"))
    r = client.post(
        "/api/v1/executions/sql",
        json={"session_id": str(s.id), "code_text": "select 1"},
        headers=_headers(roles=["analyst"]),
    )
    assert r.status_code == 409


def test_idle_session_is_resumed_for_use(api, monkeypatch):
    client, rows = api
    s = _add(rows, _session(CALLER, status="idle"))

    class _Redis:
        async def set(self, *_a, **_k):
            pass
    import app.api.auth as auth_api
    monkeypatch.setattr(auth_api, "get_redis", lambda: _Redis())
    r = client.post("/api/v1/auth/ws-ticket", json={"session_id": str(s.id)}, headers=_headers())
    assert r.status_code == 200
    assert s.status == "active" and s.ended_at is None
