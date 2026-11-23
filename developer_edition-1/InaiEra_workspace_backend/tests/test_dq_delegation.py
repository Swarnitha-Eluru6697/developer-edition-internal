"""X-001: Workspace calls the DQ service with a short-lived DELEGATED token, never the user's own credential.

Before: the DQ proxy forwarded the browser's session token (or Authorization header) to DQ unchanged, and DQ accepted it
because its audience was Workspace's own. Now each call carries a token Workspace mints: HS256 with DQ_DELEGATION_JWT_SECRET,
aud "dataquality", sub = the gateway-authenticated user (+ roles / org / email), act.sub "workspace", and the DQ scope(s) the
route needs. DQ limits the user's role permissions to that scope, so the scope can only narrow what the user may do.
"""
from __future__ import annotations

import ast
import os
import pathlib
import uuid

import httpx
import pytest
from conftest import subject_for
from jose import jwt

from app.api import dq_proxy
from app.core.config import get_settings
from app.core.service_tokens import DQ_AUDIENCE

DQ_SECRET = "workspace-to-dq-delegation-secret"
BROWSER_BEARER = "browser.session.credential-that-must-never-reach-dq"
RID, SID, IID, BID = (str(uuid.uuid4()) for _ in range(4))
ROLES = ["data_engineer"]
READ, RW, RWC, BIND, APPLY, SYNC = ("dq.read", "dq.rules.write", "dq.rules.write dq.custom_sql.write", "dq.bind",
                                    "dq.apply", "dq.sync")

DQ_ROUTES = [
    ("GET", "/api/v1/dq/rules", READ),
    ("POST", "/api/v1/dq/rules", RWC),
    ("GET", f"/api/v1/dq/rules/{RID}", READ),
    ("PATCH", f"/api/v1/dq/rules/{RID}", RWC),
    ("POST", f"/api/v1/dq/rules/{RID}/approve", RWC),
    ("DELETE", f"/api/v1/dq/rules/{RID}", RWC),
    ("GET", "/api/v1/dq/rulesets", READ),
    ("POST", "/api/v1/dq/rulesets", RW),
    ("GET", f"/api/v1/dq/rulesets/{SID}", READ),
    ("PATCH", f"/api/v1/dq/rulesets/{SID}", RW),
    ("DELETE", f"/api/v1/dq/rulesets/{SID}", RW),
    ("GET", f"/api/v1/dq/rulesets/{SID}/capability", READ),
    ("GET", f"/api/v1/dq/rulesets/{SID}/items", READ),
    ("POST", f"/api/v1/dq/rulesets/{SID}/items", RW),
    ("PATCH", f"/api/v1/dq/rulesets/{SID}/items/{IID}", RW),
    ("DELETE", f"/api/v1/dq/rulesets/{SID}/items/{IID}", RW),
    ("GET", "/api/v1/dq/bindings", READ),
    ("POST", "/api/v1/dq/bindings", BIND),
    ("GET", f"/api/v1/dq/bindings/{BID}", READ),
    ("DELETE", f"/api/v1/dq/bindings/{BID}", BIND),
    ("POST", f"/api/v1/dq/bindings/{BID}/resolve", BIND),
    ("POST", f"/api/v1/dq/bindings/{BID}/apply", APPLY),
    ("GET", f"/api/v1/dq/render/{BID}", READ),
    ("POST", f"/api/v1/dq/render/{BID}/refresh", READ),
    ("POST", "/api/v1/dq/sync/ucs", SYNC),
]


@pytest.fixture
def secrets(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dq_delegation_jwt_secret", DQ_SECRET)
    monkeypatch.setattr(s, "dq_service_url", "http://dq.test")
    return s


@pytest.fixture
def upstream(monkeypatch, secrets):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[] if req.method == "GET" else {})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def _call(client, gateway_headers, method, path, roles=ROLES):
    headers = {**gateway_headers(user_name="alice", roles=roles), "Authorization": f"Bearer {BROWSER_BEARER}"}
    client.cookies.set("workspace_session", BROWSER_BEARER)
    try:
        return client.request(method, path, headers=headers, json={} if method in ("POST", "PATCH") else None)
    finally:
        client.cookies.clear()


def _claims(req: httpx.Request) -> dict:
    return jwt.decode(req.headers["Authorization"].removeprefix("Bearer "), DQ_SECRET, algorithms=["HS256"],
                      audience=DQ_AUDIENCE, options={"require_exp": True, "require_iat": True, "require_jti": True})


@pytest.mark.parametrize("method,path,scope", DQ_ROUTES, ids=[f"{m} {p.split('/dq')[1]}" for m, p, _ in DQ_ROUTES])
def test_the_dq_proxy_sends_a_delegated_token_with_the_routes_scope(client, gateway_headers, upstream, secrets, method, path, scope):
    r = _call(client, gateway_headers, method, path)
    assert r.status_code < 400, r.text
    (req,) = upstream
    c = _claims(req)
    assert c["scope"] == scope
    assert (c["iss"], c["act"], c["sub"], c["roles"], c["org_id"]) == (
        "workspace", {"sub": "workspace"}, subject_for("alice"), ROLES, secrets.org_id)   # the SUBJECT, never the username (W-020)
    assert 0 < c["exp"] - c["iat"] <= 120


def test_the_browsers_own_credential_never_reaches_dq(client, gateway_headers, upstream):
    _call(client, gateway_headers, "GET", "/api/v1/dq/rules")
    (req,) = upstream
    assert BROWSER_BEARER not in repr(dict(req.headers)) + (req.content or b"").decode(errors="replace")
    assert "cookie" not in {k.lower() for k in req.headers}


def test_every_call_has_its_own_token(client, gateway_headers, upstream):
    for _ in range(2):
        _call(client, gateway_headers, "GET", "/api/v1/dq/rules")
    assert _claims(upstream[0])["jti"] != _claims(upstream[1])["jti"]


def test_the_dq_proxy_fails_closed_without_a_delegation_secret(client, gateway_headers, upstream, monkeypatch):
    monkeypatch.setattr(get_settings(), "dq_delegation_jwt_secret", None)
    r = _call(client, gateway_headers, "GET", "/api/v1/dq/rules")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SERVICE_AUTH_NOT_CONFIGURED"
    assert upstream == [], "nothing may be sent to DQ, and above all not the user's credential"


def test_a_token_for_dq_is_useless_at_a_runner(client, gateway_headers, upstream):
    _call(client, gateway_headers, "GET", "/api/v1/dq/rules")
    with pytest.raises(jwt.JWTError):
        jwt.decode(upstream[0].headers["Authorization"].removeprefix("Bearer "), DQ_SECRET, algorithms=["HS256"],
                   audience="dbtrunner")


def test_the_validate_route_still_uses_its_own_service_token_not_a_delegation(client, gateway_headers, secrets, monkeypatch):
    """/validate/rule is a service-to-service call (W-002); it carries X-Internal-Service-Token, never a user's delegation."""
    monkeypatch.setattr(get_settings(), "dq_service_jwt_secret", "svc-secret-for-dq-validate-0123456789")
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"valid": True, "errors": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = client.post("/api/v1/dq/validate/rule", json={"rule_type": "not_null", "dimension": "completeness"},
                    headers=gateway_headers(user_name="alice", roles=ROLES))
    assert r.status_code < 400, r.text
    (req,) = seen
    assert "authorization" not in {k.lower() for k in req.headers} and req.headers["X-Internal-Service-Token"]


# ── drift guards ─────────────────────────────────────────────────────────
def test_every_dq_proxy_route_is_in_the_scope_table():
    routed = {(m, r.path) for r in dq_proxy.router.routes for m in r.methods} - {("POST", "/api/v1/dq/validate/rule")}
    table = {(m, p.replace(RID, "{rule_id}").replace(SID, "{ruleset_id}").replace(IID, "{item_id}")
              .replace(BID, "{binding_id}")) for m, p, _ in DQ_ROUTES}
    assert routed == table, f"unscoped or stale routes: {routed ^ table}"


def _proxy_calls() -> list[ast.Call]:
    tree = ast.parse(pathlib.Path(dq_proxy.__file__).read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_proxy"]


def test_the_guard_sees_the_proxy_calls_and_each_names_a_scope_constant():
    calls = _proxy_calls()
    assert len(calls) == len(DQ_ROUTES)
    for c in calls:
        assert len(c.args) == 4 and isinstance(c.args[3], ast.Name) and c.args[3].id.startswith("DQ_"), ast.dump(c)[:120]


# Cross-repo: the scope a proxied route grants must cover the permission the DQ route REALLY requires.
DQ_REPO = pathlib.Path(os.environ.get("DQ_REPO", pathlib.Path(__file__).resolve().parents[2] / "InaiEra_dataquality_backend"))
_ANNOT = {"Reader": "dq.read", "RulesWriter": "dq.rules.write", "Binder": "dq.bind", "Applier": "dq.apply",
          "Syncer": "dq.sync"}
_PREFIX = {"rules.py": "/api/v1/rules", "rulesets.py": "/api/v1/rulesets", "bindings.py": "/api/v1/bindings",
           "render.py": "/api/v1/render", "sync.py": "/api/v1/sync"}


def _dq_required_permissions() -> dict[tuple[str, str], str]:
    need: dict[tuple[str, str], str] = {}
    for fname, prefix in _PREFIX.items():
        tree = ast.parse((DQ_REPO / "app" / "api" / fname).read_text())
        for fn in [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]:
            perm = next((_ANNOT[a.annotation.id] for a in fn.args.args
                         if isinstance(a.annotation, ast.Name) and a.annotation.id in _ANNOT), None)
            for dec in fn.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("get", "post", "patch", "delete"):
                    path = dec.args[0].value if dec.args else ""
                    need[(dec.func.attr.upper(), prefix + path)] = perm
    return need


@pytest.mark.skipif(not (DQ_REPO / "app" / "api" / "rules.py").exists(), reason="the DQ repo is not checked out next to this one")
def test_each_scope_covers_the_permission_the_dq_route_requires():
    need = _dq_required_permissions()
    assert len(need) > 20                                   # the guard sees DQ's routes
    for method, path, scope in DQ_ROUTES:
        dq_path = path.replace("/api/v1/dq", "/api/v1")
        template = {"{rule_id}": RID, "{ruleset_id}": SID, "{item_id}": IID, "{binding_id}": BID}
        match = next(((m, p) for (m, p) in need if m == method and _fill(p, template) == dq_path), None)
        assert match, f"DQ has no route {method} {dq_path}"
        required = need[match]
        assert required in scope.split(), f"{method} {path} grants {scope!r} but DQ requires {required!r}"


def _fill(path: str, template: dict) -> str:
    for k, v in template.items():
        path = path.replace(k, v)
    return path
