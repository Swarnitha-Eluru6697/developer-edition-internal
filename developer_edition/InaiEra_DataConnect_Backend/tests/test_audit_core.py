from datetime import UTC

from fastapi.testclient import TestClient
from gateway_auth import GatewayIdentity, Principal

from app.core.actor import get_actor
from app.main import app

client = TestClient(app)


def _principal(username: str, user_id: str = "") -> Principal:
    return Principal(GatewayIdentity(username=username, user_id=user_id))


def test_get_actor_is_the_gateway_username() -> None:
    assert get_actor(_principal("alice@example.com", "u-1")) == "alice@example.com"


def test_get_actor_falls_back_to_user_id_then_unknown() -> None:
    assert get_actor(_principal("", "u-1")) == "u-1"
    assert get_actor(_principal("")) == "unknown"


def test_record_audit_persists_a_row(monkeypatch) -> None:
    """record_audit itself needs a live Postgres to actually persist (same
    limitation as every other Postgres-backed write introduced this
    session) — this verifies its call shape against a captured
    AuditLogRepository.create instead."""
    import app.core.audit as audit_module

    captured = {}

    class _FakeRepo:
        def __init__(self, session):
            pass

        async def create(self, values):
            captured.update(values)
            return type("Row", (), values)()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(audit_module, "AuditLogRepository", _FakeRepo)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", lambda: _FakeSession())

    import asyncio

    asyncio.run(
        audit_module.record_audit(
            org_id="org-1", actor="alice", action="created",
            resource_type="connection", resource_id="c1",
            after_state={"name": "x"},
        )
    )

    assert captured["org_id"] == "org-1"
    assert captured["actor"] == "alice"
    assert captured["action"] == "created"
    assert captured["resource_type"] == "connection"
    assert captured["resource_id"] == "c1"
    assert captured["after_state"] == {"name": "x"}
    assert captured["before_state"] is None
    assert captured["committed"] is True


def test_record_audit_makes_a_datetime_field_json_safe(monkeypatch) -> None:
    """Regression test: every route that calls record_audit() passes it the
    same plain dict it returns to its own client (e.g.
    mask_connection_profile's output — see connections.py's create_connection),
    which routinely carries a real datetime (last_tested_at, set moments
    earlier by probe_and_record). Pydantic/FastAPI knows how to encode that
    for the HTTP response, but handing the raw dict straight to asyncpg for
    a JSONB column does not — every connection creation crashed with
    "Object of type datetime is not JSON serializable" (the create itself
    had already committed, so the resource existed but the request came
    back as a 500) until record_audit started running before_state/
    after_state through jsonable_encoder itself."""
    import asyncio
    from datetime import datetime

    import app.core.audit as audit_module

    captured = {}

    class _FakeRepo:
        def __init__(self, session):
            pass

        async def create(self, values):
            captured.update(values)
            return type("Row", (), values)()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr(audit_module, "AuditLogRepository", _FakeRepo)
    monkeypatch.setattr(audit_module, "AsyncSessionLocal", lambda: _FakeSession())

    when = datetime(2026, 9, 24, 0, 27, 26, 355582, tzinfo=UTC)
    asyncio.run(
        audit_module.record_audit(
            org_id="org-1", actor="alice", action="created",
            resource_type="connection", resource_id="c1",
            after_state={"id": "c1", "last_tested_at": when, "last_test_message": "ok"},
        )
    )

    # The whole point: this must be a plain JSON-safe value (a string), not
    # the raw datetime object that broke json.dumps for the real JSONB write.
    assert captured["after_state"]["last_tested_at"] == when.isoformat()
    assert isinstance(captured["after_state"]["last_tested_at"], str)
    # Round-trips through the stdlib json module exactly like the real
    # asyncpg JSONB codec would — the actual crash this reproduces.
    import json

    json.dumps(captured["after_state"])
