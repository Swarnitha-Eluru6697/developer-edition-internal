"""W-004: logout always clears the cookie and is idempotent; identity and audit are best-effort.

Before: the route required a valid current user (and a database) before deleting the cookie, so an expired or malformed credential
got a 401 and the stale cookie stayed in the browser. Now it needs no authentication: the cookie is always deleted, and the audit
event is recorded only when the gateway vouches for a caller, in its own session, and can never change the outcome.
"""
from __future__ import annotations

import types

import pytest
from conftest import _headers
from fastapi.routing import APIRoute

from app.api import auth as auth_api
from app.core.auth import SESSION_COOKIE_NAME
from app.core.config import get_settings


def _cleared(r) -> bool:
    cookies = r.headers.get_list("set-cookie")
    return any(c.startswith(f"{SESSION_COOKIE_NAME}=") and ('Max-Age=0' in c or 'expires=' in c.lower()) for c in cookies)


def test_the_route_has_no_authentication_dependency():
    route = next(r for r in auth_api.router.routes if isinstance(r, APIRoute) and r.path.endswith("/logout"))
    assert not route.dependant.dependencies, "logout must not depend on a valid session"


@pytest.mark.parametrize("headers", [
    {},                                                                     # no credential at all
    _headers(secret="wrong-secret"),                                        # a request that did not come through the gateway
    _headers(user_id="not-a-uuid"),                                         # a malformed identity
    _headers(org_id="someone-elses-org"),                                   # another tenant
    _headers(user_name=None),                                               # missing identity
], ids=["no-credential", "wrong-secret", "malformed-subject", "wrong-tenant", "no-user"])
def test_logout_clears_the_cookie_whatever_credential_it_is_given(client, headers):
    client.cookies.set(SESSION_COOKIE_NAME, "expired.or.malformed")
    r = client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _cleared(r), r.headers.get_list("set-cookie")
    assert r.headers["cache-control"] == "no-store"


def test_the_cleared_cookie_is_on_the_path_it_was_set_on(client):
    import re
    r = client.post("/api/v1/auth/logout")
    assert any(re.search(r"Path=/(;|$)", c) for c in r.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE_NAME))


def test_logout_is_idempotent(client):
    for _ in range(3):
        r = client.post("/api/v1/auth/logout")
        assert r.status_code == 200 and _cleared(r)


def test_a_broken_audit_or_database_never_keeps_the_cookie_alive(client, monkeypatch):
    async def boom(request):
        raise RuntimeError("database is down")

    monkeypatch.setattr(auth_api, "_audit_logout", boom)
    r = client.post("/api/v1/auth/logout", headers=_headers())
    assert r.status_code == 200 and _cleared(r)


def test_a_vouched_caller_is_audited_in_its_own_session(client, monkeypatch):
    recorded = []

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): recorded.append("commit")

    import app.db.session as dbs
    monkeypatch.setattr(dbs, "AsyncSessionLocal", lambda: FakeSession())

    async def ensure_user(self, principal):
        recorded.append(("user", principal.user_id))
        return types.SimpleNamespace(id="row-1")

    async def record(self, **kw):
        recorded.append(("audit", kw["action"], kw["user_id"]))

    monkeypatch.setattr(auth_api.WorkspaceService, "ensure_user", ensure_user)
    monkeypatch.setattr(auth_api.AuditService, "record", record)
    r = client.post("/api/v1/auth/logout", headers=_headers(user_name="alice"))
    assert r.status_code == 200 and _cleared(r)
    assert ("audit", "auth.logout", "row-1") in recorded and "commit" in recorded


def test_an_unverifiable_caller_is_not_audited(client, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_api.AuditService, "record", lambda *a, **k: calls.append(1))
    client.post("/api/v1/auth/logout", headers=_headers(secret="wrong-secret"))
    assert calls == []


def test_the_deleted_cookie_carries_the_attributes_it_was_set_with(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "prod")
    r = client.post("/api/v1/auth/logout")
    c = next(c for c in r.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE_NAME))
    import re
    assert "Secure" in c and "HttpOnly" in c and "SameSite=strict" in c
    assert re.search(r"Path=/(;|$)", c), c            # the exact path the cookie was set on, or the browser keeps it
