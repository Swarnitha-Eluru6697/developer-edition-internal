"""WS-AUTH-010: browser auth is an HttpOnly cookie, and a WebSocket connects
only with a single-use, 30-second ticket bound to org + user + session —
expired, replayed, wrong-session, wrong-org and ended-session tickets are
refused, and ending a session closes its open sockets."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.auth as auth_api
import app.ws.gateway as gateway
from app.core.deps import get_db
from app.main import app
from app.models import Session as SessionModel
from app.services.workspace_service import WorkspaceService

from conftest import TEST_ORG_ID, _headers, subject_for

OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    async def get(self, model, key, **_k):
        row = self.rows.get(key)
        return row if isinstance(row, model) else None

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def flush(self): pass
    async def commit(self): pass
    async def rollback(self): pass
    def add(self, _r): pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def env(monkeypatch):
    rows: dict = {}
    # One fake server, a fresh client per call: the app runs on the
    # TestClient's own event loop, and an asyncio client (pub/sub in
    # particular) must not be shared across loops.
    server = fakeredis.FakeServer()

    def new_client():
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    redis = new_client()

    async def _db():
        yield FakeDB(rows)

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=OWNER, external_id=principal.user_id)
    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(auth_api, "get_redis", new_client)
    monkeypatch.setattr(gateway, "get_redis", new_client)
    import app.services.execution_router as er
    monkeypatch.setattr(er, "get_redis", new_client)
    monkeypatch.setattr(gateway, "AsyncSessionLocal", lambda: FakeDB(rows))

    async def _no_audit(self, **_k):
        pass
    from app.services import audit_service
    monkeypatch.setattr(audit_service.AuditService, "record", _no_audit)
    yield TestClient(app), rows, redis
    app.dependency_overrides.clear()


def _session(rows, status="active", owner=OWNER):
    s = SessionModel(id=uuid.uuid4(), user_id=owner, workspace_id=uuid.uuid4(), status=status, runtime_metadata={})
    rows[s.id] = s
    return s


def _ticket(client, session_id):
    r = client.post("/api/v1/auth/ws-ticket", json={"session_id": str(session_id)}, headers=_headers())
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def _refused(client, path):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(path) as ws:
            ws.receive_text()
    return ei.value.code


# ── Session cookie ───────────────────────────────────────────────────────

def test_exchange_requires_a_bearer(env):
    client, _, _ = env
    assert client.post("/api/v1/auth/exchange", headers=_headers()).status_code == 401


# (A verified exchange, and every rejected token, are covered by tests/test_auth_exchange.py.)


# ── Ticket minting ───────────────────────────────────────────────────────

def test_ticket_is_bound_and_short_lived(env):
    client, rows, redis = env
    s = _session(rows)
    t = _ticket(client, s.id)
    import asyncio
    raw = asyncio.run(redis.get(f"ws-ticket:{t}"))
    ttl = asyncio.run(redis.ttl(f"ws-ticket:{t}"))
    data = json.loads(raw)
    assert data == {"org_id": TEST_ORG_ID, "user_id": str(OWNER), "external_user_id": subject_for("test-user"), "session_id": str(s.id)}
    assert 0 < ttl <= auth_api.WS_TICKET_TTL_SECONDS


def test_no_ticket_for_someone_elses_or_an_ended_session(env):
    client, rows, _ = env
    other = _session(rows, owner=uuid.uuid4())
    ended = _session(rows, status="terminated")
    for s, code in ((other, 404), (ended, 409)):
        r = client.post("/api/v1/auth/ws-ticket", json={"session_id": str(s.id)}, headers=_headers())
        assert r.status_code == code


# ── Connecting ───────────────────────────────────────────────────────────

def test_valid_ticket_connects_once(env):
    client, rows, _ = env
    s = _session(rows)
    t = _ticket(client, s.id)
    with client.websocket_connect(f"/ws/sessions/{s.id}?ticket={t}") as ws:
        assert json.loads(ws.receive_text())["type"] == "session.ready"
    # Replay of the same ticket is refused (single-use, GETDEL).
    assert _refused(client, f"/ws/sessions/{s.id}?ticket={t}") == 1008


def test_unknown_or_expired_ticket_is_refused(env):
    client, rows, redis = env
    s = _session(rows)
    t = _ticket(client, s.id)
    import asyncio
    asyncio.run(redis.delete(f"ws-ticket:{t}"))          # what expiry looks like
    assert _refused(client, f"/ws/sessions/{s.id}?ticket={t}") == 1008
    assert _refused(client, f"/ws/sessions/{s.id}?ticket=made-up") == 1008


def test_ticket_for_another_session_is_refused(env):
    client, rows, _ = env
    mine, other = _session(rows), _session(rows)
    t = _ticket(client, mine.id)
    assert _refused(client, f"/ws/sessions/{other.id}?ticket={t}") == 1008


def test_ticket_from_another_org_is_refused(env):
    client, rows, redis = env
    s = _session(rows)
    import asyncio
    asyncio.run(redis.set("ws-ticket:forged", json.dumps({
        "org_id": "another-org", "user_id": str(OWNER), "external_user_id": "x", "session_id": str(s.id),
    }), ex=30))
    assert _refused(client, f"/ws/sessions/{s.id}?ticket=forged") == 1008


def test_session_ended_after_minting_is_refused(env):
    client, rows, _ = env
    s = _session(rows)
    t = _ticket(client, s.id)
    s.status = "terminated"
    assert _refused(client, f"/ws/sessions/{s.id}?ticket={t}") == 1008


def test_ending_the_session_closes_its_open_socket(env):
    client, rows, redis = env
    s = _session(rows)
    t = _ticket(client, s.id)
    import asyncio
    with client.websocket_connect(f"/ws/sessions/{s.id}?ticket={t}") as ws:
        assert json.loads(ws.receive_text())["type"] == "session.ready"
        assert client.delete(f"/api/v1/sessions/{s.id}", headers=_headers()).status_code == 204
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "session.terminated"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == gateway.SESSION_TERMINATED_CLOSE_CODE
