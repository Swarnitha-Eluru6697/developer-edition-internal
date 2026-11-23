"""v1.1 permission API -- contract 01-CONTRACT.md sections 4-6."""
import base64
import json
import os
import warnings

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from gateway_auth import (
    extract_identity, get_principal, has_permission, protect_app, require_permission, split_multi,
)

SECRET = "real-secret"


def b64(values):
    return base64.b64encode(json.dumps(values).encode()).decode()


def via_envoy(permissions=None, **extra):
    h = {"x-inaiera-gateway-secret": SECRET, "x-user-name": "alice", "x-user-id": "u-123", "x-tenant-id": "acme"}
    if permissions is not None:
        h["x-user-permissions"] = b64(permissions)
    h.update(extra)
    return h


@pytest.fixture(autouse=True)
def env():
    os.environ["TRUST_ENVOY_HEADERS"] = "true"
    os.environ["ENVOY_INTERNAL_SECRET"] = SECRET
    yield
    os.environ.pop("TRUST_ENVOY_HEADERS", None)
    os.environ.pop("ENVOY_INTERNAL_SECRET", None)


@pytest.fixture
def client():
    app = FastAPI()
    protect_app(app, public_paths=["/health", "/static/*"])

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/static/app.js")
    def static():
        return {"js": True}

    @app.get("/api/clusters", dependencies=[Depends(require_permission("mlstudio:clusters:view"))])
    def clusters():
        return {"clusters": []}

    @app.get("/api/me")
    def me(principal=Depends(get_principal)):
        return principal.me()

    @app.get("/api/anything")
    def anything():
        return {"ok": True}

    return TestClient(app)


# ── list header parsing (contract 4) ─────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    b64(["mlstudio:clusters:view", "ucs:rbac:manage"]),                   # Envoy: base64(JSON array)
    '["mlstudio:clusters:view","ucs:rbac:manage"]',                       # raw JSON array
    "mlstudio:clusters:view, ucs:rbac:manage",                            # comma list
])
def test_permission_header_formats(raw):
    assert set(split_multi(raw)) == {"mlstudio:clusters:view", "ucs:rbac:manage"}


@pytest.mark.parametrize("raw", [None, "", "   ", b64("not-a-list"), b64({"a": 1}), b64(5)])
def test_invalid_or_empty_permission_header_is_no_permissions(raw):
    ident = extract_identity("alice", None, None, "default", permissions=raw)
    assert ident.permissions == frozenset()


def test_identity_carries_user_id_permissions_and_audit_roles():
    ident = extract_identity("alice", None, "acme", "default", user_id="u-1",
                             permissions=b64(["a:b:view"]), roles=b64(["ml-engineer"]))
    assert ident.user_id == "u-1" and ident.tenant_id == "acme"
    assert ident.permissions == frozenset({"a:b:view"})
    assert ident.roles == ("ml-engineer",)
    assert has_permission(ident, "a:b:view") and not has_permission(ident, "a:b:edit")


def test_has_permission_least_privilege():
    assert not has_permission(None, "x:y:view")
    assert not has_permission(object(), "x:y:view")


# ── contract 6: A-G ──────────────────────────────────────────────────────────
def test_A_no_secret_is_401(client):
    assert client.get("/api/clusters").status_code == 401
    assert client.get("/api/anything").status_code == 401          # deny by default, even without a permission check


def test_B_wrong_secret_is_401(client):
    assert client.get("/api/clusters", headers=via_envoy(["mlstudio:clusters:view"], **{"x-inaiera-gateway-secret": "wrong"})).status_code == 401


def test_trust_flag_off_is_401(client):
    os.environ["TRUST_ENVOY_HEADERS"] = "false"
    assert client.get("/api/clusters", headers=via_envoy(["mlstudio:clusters:view"])).status_code == 401


def test_C_right_secret_no_permission_header_is_403(client):
    assert client.get("/api/clusters", headers=via_envoy()).status_code == 403


def test_C_right_secret_no_identity_is_401(client):
    assert client.get("/api/clusters", headers={"x-inaiera-gateway-secret": SECRET}).status_code == 401


def test_D_needed_permission_is_200(client):
    r = client.get("/api/clusters", headers=via_envoy(["mlstudio:clusters:view"]))
    assert r.status_code == 200 and r.json() == {"clusters": []}


def test_E_only_another_apps_permission_is_403(client):
    assert client.get("/api/clusters", headers=via_envoy(["ucs:clusters:view", "mlstudio:clusters:edit"])).status_code == 403


def test_F_forged_role_headers_are_ignored(client):
    forged = via_envoy([], **{"x-user-role": "ml-platform-admin", "x-user-roles": b64(["admin", "ml-platform-admin"])})
    assert client.get("/api/clusters", headers=forged).status_code == 403


def test_G_public_paths_need_nothing(client):
    os.environ.pop("TRUST_ENVOY_HEADERS", None)
    assert client.get("/health").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_me_returns_contract_body(client):
    r = client.get("/api/me", headers=via_envoy(["b:x:view", "a:x:view"], **{"x-user-roles": b64(["r1"])}))
    assert r.json() == {"user": "alice", "userId": "u-123", "tenant": "acme", "permissions": ["a:x:view", "b:x:view"]}


def test_require_permission_works_without_protect_app():
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(require_permission("a:x:view"))])
    def x():
        return {}

    c = TestClient(app)
    assert c.get("/x").status_code == 401
    assert c.get("/x", headers=via_envoy(["a:x:view"])).status_code == 200
    assert c.get("/x", headers=via_envoy(["a:y:view"])).status_code == 403


def test_role_helpers_warn_deprecated():
    from gateway_auth import RoleConfig
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        RoleConfig(all_roles=("a",), tiers={"a": 0}, default_role="a")
    assert any(issubclass(x.category, DeprecationWarning) for x in w)
