"""Contract 01-CONTRACT.md section 6 (acceptance tests A-G) against the Workspace API, plus deny-by-default over every route.

    A  bypassing Envoy (no secret)                        -> 401
    B  wrong secret                                       -> 401
    C  right secret, no permission header                 -> 403 (401 with no identity)
    D  right secret + identity + the needed permission    -> 200
    E  right secret + identity + only other apps' perms   -> 403
    F  forged x-user-role / x-user-roles, no permissions  -> 403
    G  health probe without anything                      -> 200
And task 26: with workspace:dbt:run the dbt run API is allowed, without it 403.
"""
from __future__ import annotations

import re
import uuid

import httpx
import pytest
from conftest import TEST_GATEWAY_SECRET, TEST_ORG_ID, encode_list, subject_for
from fastapi.routing import APIRoute

from app.core.config import get_settings
from app.main import PUBLIC_PATHS, app

PROTECTED = "/api/v1/dbt/projects"          # needs workspace:access:view + a dbt-runner job permission
NEEDED = ["workspace:access:view", "workspace:dbt:edit"]
PID = str(uuid.uuid4())


@pytest.fixture
def runner(monkeypatch):
    """A fake dbt runner behind the proxy; records what reached it."""
    s = get_settings()
    monkeypatch.setattr(s, "dbtrunner_delegation_jwt_secret", "contract-test-delegation-secret")
    monkeypatch.setattr(s, "dbtrunner_url", "http://dbt.test")
    monkeypatch.setattr(s, "dagster_graphql_url", None)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/run"):
            return httpx.Response(202, json={"id": str(uuid.uuid4()), "status": "queued"})
        return httpx.Response(200, json=[])

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def _identity(**extra) -> dict[str, str]:
    h = {"X-Inaiera-Gateway-Secret": TEST_GATEWAY_SECRET, "X-User-Name": "alice", "X-User-Id": subject_for("alice"),
         "X-Tenant-Id": TEST_ORG_ID}
    h.update(extra)
    return h


def test_A_no_gateway_secret_is_401(client, runner):
    assert client.get(PROTECTED).status_code == 401
    h = _identity(**{"X-User-Permissions": encode_list(NEEDED)})
    del h["X-Inaiera-Gateway-Secret"]
    assert client.get(PROTECTED, headers=h).status_code == 401
    assert runner == []


def test_B_wrong_gateway_secret_is_401(client, runner):
    h = _identity(**{"X-Inaiera-Gateway-Secret": "wrong", "X-User-Permissions": encode_list(NEEDED)})
    assert client.get(PROTECTED, headers=h).status_code == 401
    assert runner == []


def test_C_right_secret_but_no_permissions_is_403_and_no_identity_is_401(client, runner):
    assert client.get(PROTECTED, headers=_identity()).status_code == 403
    assert client.get(PROTECTED, headers={"X-Inaiera-Gateway-Secret": TEST_GATEWAY_SECRET}).status_code == 401
    assert runner == []


def test_D_right_secret_identity_and_the_needed_permission_is_200(client, runner):
    r = client.get(PROTECTED, headers=_identity(**{"X-User-Permissions": encode_list(NEEDED)}))
    assert r.status_code == 200, r.text
    assert len(runner) == 1


@pytest.mark.parametrize("encoding", ["base64", "json", "comma"])
def test_D_every_list_header_encoding_envoy_or_a_dev_gateway_sends_is_read(client, runner, encoding):
    value = {"base64": encode_list(NEEDED), "json": '["' + '","'.join(NEEDED) + '"]', "comma": ",".join(NEEDED)}[encoding]
    assert client.get(PROTECTED, headers=_identity(**{"X-User-Permissions": value})).status_code == 200


def test_E_only_another_apps_permissions_is_403(client, runner):
    others = ["mlstudio:access:view", "mlstudio:clusters:view", "ucs:rbac:manage", "dataconnect:pipelines:deploy",
              "agent:settings:edit"]
    h = _identity(**{"X-User-Permissions": encode_list(others)})
    assert client.get(PROTECTED, headers=h).status_code == 403
    assert client.get("/api/v1/workspaces", headers=h).status_code == 403
    assert runner == []


@pytest.mark.parametrize("forged", [
    {"X-User-Role": "workspace_admin"},
    {"X-User-Roles": "admin,workspace_admin,platform-admin"},
    {"X-User-Roles": encode_list(["workspace_admin", "admin"]), "X-User-Role": "admin"},
    {"X-User-Groups": "admin,workspace_admin"},
])
def test_F_forged_role_headers_with_no_permissions_are_403(client, runner, forged):
    assert client.get(PROTECTED, headers=_identity(**forged)).status_code == 403
    assert client.post(f"/api/v1/dbt/projects/{PID}/run", json={}, headers=_identity(**forged)).status_code == 403
    assert runner == []


def test_G_health_probe_without_anything_is_200(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert client.get("/readyz").status_code != 401          # readiness depends on the pod's dependencies, never on identity


# ── task 26: the dbt run API ─────────────────────────────────────────────
def test_with_workspace_dbt_run_the_dbt_run_api_is_allowed(client, runner):
    h = _identity(**{"X-User-Permissions": encode_list(["workspace:access:view", "workspace:dbt:run"])})
    r = client.post(f"/api/v1/dbt/projects/{PID}/run", json={}, headers=h)
    assert r.status_code == 202, r.text
    (req,) = runner
    assert req.url.path == f"/api/v1/projects/{PID}/run"
    from jose import jwt
    claims = jwt.get_unverified_claims(req.headers["Authorization"].removeprefix("Bearer "))
    assert claims["scope"] == "dbt.runs.trigger" and claims["permissions"] == ["workspace:dbt:run"]
    assert "roles" not in claims


@pytest.mark.parametrize("held", [
    ["workspace:access:view"],
    ["workspace:access:view", "workspace:dbt:edit", "workspace:sql:run", "workspace:flink:deploy"],
    ["workspace:dbt:run"],                                       # may not open Workspace at all
])
def test_without_workspace_dbt_run_the_dbt_run_api_is_403(client, runner, held):
    h = _identity(**{"X-User-Permissions": encode_list(held)})
    r = client.post(f"/api/v1/dbt/projects/{PID}/run", json={}, headers=h)
    assert r.status_code == 403, r.text
    assert runner == []


# ── deny by default: every route not on the public list needs the gateway ──
def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda m: "github" if m.group(0) == "{provider}" else str(uuid.uuid4()), path)


def _public(path: str) -> bool:
    return any(path.startswith(p[:-1]) if p.endswith("*") else path == p for p in PUBLIC_PATHS)


def test_the_public_list_is_short_and_explicit():
    assert set(PUBLIC_PATHS) >= {"/healthz", "/readyz"}
    assert len(PUBLIC_PATHS) <= 12 and "/" not in PUBLIC_PATHS and "/docs" not in PUBLIC_PATHS


def _api_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):            # an included router (newer FastAPI keeps it wrapped)
            yield from (r for r in route.original_router.routes if isinstance(r, APIRoute))


def test_every_non_public_route_answers_401_without_the_gateway(client):
    checked = 0
    for route in _api_routes():
        path = _concrete(route.path)
        if _public(path):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            r = client.request(method, path, json={})
            assert r.status_code == 401, f"{method} {route.path}: {r.status_code}"
            checked += 1
    for extra in ("/", "/docs", "/openapi.json"):
        assert client.get(extra).status_code == 401, extra
    assert checked > 80, "route discovery found too few routes: the guard would be vacuous"


def test_me_returns_the_contract_body(client):
    h = _identity(**{"X-User-Permissions": encode_list(["workspace:dbt:run", "workspace:access:view", "ucs:rbac:manage"]),
                     "X-User-Roles": "someone"})
    r = client.get("/api/v1/me", headers=h)
    assert r.status_code == 200
    assert r.json() == {"user": "alice", "userId": subject_for("alice"), "tenant": TEST_ORG_ID,
                        "permissions": ["ucs:rbac:manage", "workspace:access:view", "workspace:dbt:run"]}


def test_me_answers_a_caller_without_workspace_access(client):
    r = client.get("/api/v1/me", headers=_identity())
    assert r.status_code == 200 and r.json()["permissions"] == []
    assert client.get("/api/v1/me").status_code == 401
