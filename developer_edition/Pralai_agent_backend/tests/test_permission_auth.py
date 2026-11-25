"""Contract acceptance tests (docs/auth/01-CONTRACT.md section 6, A-G) against
the real app: deny by default via gateway-auth's protect_app, then authorise
by permission only. Role names — in any header — grant nothing.
"""
import ast
import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app, PUBLIC_PATHS
from conftest import ALL_PERMISSIONS, GATEWAY_TEST_SECRET, gateway_headers

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def _schema():
    # D reads real rows; the schema must exist (idempotent: alembic upgrade head).
    from app.db import init_db
    init_db()


@pytest.fixture()
def bare():
    """A client that sends nothing it is not told to (no default gateway headers)."""
    return TestClient(app, headers={})


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "true")
    monkeypatch.setenv("ENVOY_INTERNAL_SECRET", GATEWAY_TEST_SECRET)


def _b64(values):
    return base64.b64encode(json.dumps(values).encode()).decode()


# --- contract section 6 ---------------------------------------------------

def test_A_no_secret_is_401(bare):
    assert bare.get("/api/agents").status_code == 401


def test_A_untrusted_gateway_is_401_even_with_right_secret(bare, monkeypatch):
    monkeypatch.setenv("TRUST_ENVOY_HEADERS", "false")
    assert bare.get("/api/agents", headers=gateway_headers()).status_code == 401


def test_B_wrong_secret_is_401(bare):
    r = bare.get("/api/agents", headers=gateway_headers(secret="wrong"))
    assert r.status_code == 401


def test_C_secret_and_identity_without_permissions_is_403(bare):
    h = gateway_headers()
    del h["x-user-permissions"]
    assert bare.get("/api/agents", headers=h).status_code == 403


def test_C_secret_without_identity_is_401(bare):
    assert bare.get("/api/agents",
                    headers={"x-inaiera-gateway-secret": GATEWAY_TEST_SECRET}).status_code == 401


def test_D_needed_permission_is_200(bare):
    r = bare.get("/api/agents", headers=gateway_headers(["agent:studio:edit"]))
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_D_any_reader_permission_lists_agents(bare):
    for p in ("agent:overview:view", "agent:runs:view", "agent:monitoring:view"):
        assert bare.get("/api/agents", headers=gateway_headers([p])).status_code == 200, p


def test_E_only_another_apps_permissions_is_403(bare):
    other = ["mlstudio:access:view", "mlstudio:clusters:view", "dataconnect:pipelines:deploy",
             "ucs:rbac:manage", "workspace:dbt:run"]
    assert bare.get("/api/agents", headers=gateway_headers(other)).status_code == 403


@pytest.mark.parametrize("forged", [
    {"x-user-role": "platform-admin"},
    {"x-user-roles": "platform-admin,security-admin,builder,admin"},
    {"x-user-roles": _b64(["platform-admin", "Agent Admin", "admin"])},
    {"x-user-groups": _b64(["platform-admin", "admins"])},
])
def test_F_forged_role_headers_without_permissions_are_403(bare, forged):
    h = gateway_headers([])
    h.update(forged)
    assert bare.get("/api/agents", headers=h).status_code == 403
    assert bare.post("/api/agents", headers=h, json={"name": "x"}).status_code == 403
    assert bare.post("/internal/maintenance/hitl/reconcile-stale", headers=h).status_code == 403


def test_G_health_probe_without_anything_is_200(bare, monkeypatch):
    monkeypatch.delenv("TRUST_ENVOY_HEADERS", raising=False)
    monkeypatch.delenv("ENVOY_INTERNAL_SECRET", raising=False)
    assert bare.get("/livez").status_code == 200


def test_public_paths_are_only_probes_and_the_embed_script(bare):
    assert sorted(PUBLIC_PATHS) == ["/embed.js", "/livez", "/readyz"]
    assert bare.get("/embed.js").status_code == 200


# --- every other surface is behind the boundary -----------------------------

@pytest.mark.parametrize("method,path", [
    ("post", "/mcp/a1/sometoken"),
    ("post", "/api/webhooks/sometoken"),
    ("get", "/api/me"),
    ("get", "/api/health"),
    ("get", "/metrics"),
    ("get", "/api/runs"),
    ("get", "/api/templates"),
    ("post", "/internal/maintenance/uploads/sweep-expired"),
    ("get", "/internal/diagnostics/readyz"),
])
def test_protected_surfaces_without_secret_are_401(bare, method, path):
    assert getattr(bare, method)(path).status_code == 401


def test_mcp_without_agent_run_permission_is_403(bare):
    r = bare.post("/mcp/a1/sometoken", headers=gateway_headers(["agent:studio:edit"]), json={})
    assert r.status_code == 403


def test_me_returns_principal(bare):
    perms = ["agent:access:view", "agent:webapps:use"]
    r = bare.get("/api/me", headers=gateway_headers(perms, user="alice"))
    assert r.status_code == 200
    assert r.json() == {"user": "alice", "userId": "id-alice", "tenant": "prod_metalake",
                        "permissions": sorted(perms)}


def test_me_with_no_permissions_is_200_and_empty(bare):
    h = gateway_headers([])
    r = bare.get("/api/me", headers=h)
    assert r.status_code == 200 and r.json()["permissions"] == []


def test_webapps_only_user_cannot_build_or_publish(bare):
    h = gateway_headers(["agent:access:view", "agent:webapps:use"])
    assert bare.get("/api/agents", headers=h).status_code == 403
    assert bare.post("/api/agents", headers=h, json={"name": "x"}).status_code == 403
    assert bare.post("/api/agents/a1/publish", headers=h).status_code == 403
    assert bare.get("/api/runs", headers=h).status_code == 403
    assert bare.get("/api/settings/runtime-config", headers=h).status_code == 403


def test_maintenance_needs_its_permission(bare):
    h = gateway_headers(["agent:studio:edit", "agent:settings:edit"])
    assert bare.get("/internal/maintenance/status", headers=h).status_code == 403
    r = bare.get("/internal/maintenance/status", headers=gateway_headers(["agent:maintenance:run"]))
    assert r.status_code == 200, r.text


# --- static guarantees ------------------------------------------------------

def _route_permissions(route: APIRoute):
    found = []

    def walk(dep):
        for d in dep.dependencies:
            walk(d)
        call = dep.call
        if call is not None and getattr(call, "__qualname__", "").startswith(
                ("require_permission", "require_any_permission")):
            found.append(call)
    walk(route.dependant)
    return found


def test_every_routed_endpoint_declares_a_permission():
    missing = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or r.path in PUBLIC_PATHS:
            continue
        if r.path == "/api/me":          # needs a principal only, by contract
            continue
        if not _route_permissions(r):
            missing.append(f"{sorted(r.methods)} {r.path}")
    assert not missing, missing


def test_permission_ids_used_in_code_are_declared():
    declared = set(ALL_PERMISSIONS)
    src = (ROOT / "app" / "permissions.py").read_text()
    used = set(re.findall(r'"(agent:[a-z-]+:[a-z-]+)"', src))
    assert used and used <= declared, used - declared
    for pid in declared:
        assert re.fullmatch(r"agent:[a-z]+(-[a-z]+)*:[a-z]+(-[a-z]+)*", pid), pid


def test_admin_portal_seed_permissions_are_declared():
    seed = {"agent:access:view", "agent:overview:view", "agent:studio:edit", "agent:templates:view",
            "agent:templates:manage-platform", "agent:rag:manage", "agent:integrations:manage",
            "agent:runs:view", "agent:traces:view", "agent:monitoring:view", "agent:guardrails:manage",
            "agent:alerts:manage", "agent:webapps:use", "agent:settings:edit", "agent:agents:run",
            "agent:agents:publish"}
    assert seed <= set(ALL_PERMISSIONS)


ROLE_NAMES = re.compile(
    r"security-admin|platform-admin|no-access|Agent Admin|End User|Security Admin|Platform Admin"
    r"|['\"]builder['\"]|\bx-user-role\b")


def test_no_role_names_in_application_code():
    hits = []
    for f in (ROOT / "app").rglob("*.py"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if ROLE_NAMES.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not hits, hits
