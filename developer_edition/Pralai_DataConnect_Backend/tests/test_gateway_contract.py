"""
Contract section 6 (docs/auth/01-CONTRACT.md) acceptance tests A–G, against a real protected route
(GET /api/v1/dataconnect/connections), plus GET /me (contract 5.4) and the tenant/actor coming from the
gateway principal rather than client headers.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
from conftest import gateway_headers
from fastapi.testclient import TestClient
from personas import OTHER_APP, PERSONAS

from app.main import app

client = TestClient(app)
CONNECTIONS = "/api/v1/dataconnect/connections"
HEALTH = "/api/v1/dataconnect/health"
SECRET = os.environ["ENVOY_INTERNAL_SECRET"]


async def _async(value: Any) -> Any:
    return value


@pytest.fixture(autouse=True)
def tenants_seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def _list(org_id: str) -> list[dict]:
        seen.append(org_id)
        return []

    monkeypatch.setattr("app.api.routes.connections.connection_service.list_connections", _list)
    return seen


def _identity(**extra: str) -> dict[str, str]:
    return {"X-User-Name": "alice", "X-User-Id": "u-alice", "X-Tenant-Id": "org-a",
            "X-Inaiera-Gateway-Secret": SECRET, **extra}


def _b64(values: list[str]) -> str:
    return base64.b64encode(json.dumps(values).encode()).decode()


# ── A–G ──────────────────────────────────────────────────────────────────────

def test_a_no_gateway_secret_is_401() -> None:
    assert client.get(CONNECTIONS).status_code == 401
    # identity + permissions but no secret: still 401 (a request that bypassed Envoy)
    headers = gateway_headers()
    headers.pop("X-Inaiera-Gateway-Secret")
    assert client.get(CONNECTIONS, headers=headers).status_code == 401


def test_b_wrong_gateway_secret_is_401() -> None:
    assert client.get(CONNECTIONS, headers={**gateway_headers(), "X-Inaiera-Gateway-Secret": "wrong"}).status_code == 401


def test_c_secret_and_identity_but_no_permission_header_is_403() -> None:
    assert client.get(CONNECTIONS, headers=_identity()).status_code == 403


def test_c_secret_but_no_identity_is_401() -> None:
    assert client.get(CONNECTIONS, headers={"X-Inaiera-Gateway-Secret": SECRET}).status_code == 401


def test_d_the_needed_permission_is_200() -> None:
    resp = client.get(CONNECTIONS, headers=_identity(**{"X-User-Permissions": _b64(["dataconnect:connections:view"])}))
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.parametrize("encoding", ["base64", "json", "comma"])
def test_d_every_contract_list_encoding_is_accepted(encoding: str) -> None:
    perms = ["dataconnect:access:view", "dataconnect:connections:view"]
    value = {"base64": _b64(perms), "json": json.dumps(perms), "comma": ",".join(perms)}[encoding]
    assert client.get(CONNECTIONS, headers=_identity(**{"X-User-Permissions": value})).status_code == 200


def test_e_only_another_apps_permissions_is_403() -> None:
    headers = _identity(**{"X-User-Permissions": _b64(sorted(OTHER_APP))})
    assert client.get(CONNECTIONS, headers=headers).status_code == 403


def test_e_a_similar_looking_permission_of_another_app_is_403() -> None:
    headers = _identity(**{"X-User-Permissions": _b64(["ucs:connections:view", "connections:view", "dataconnect:connections:*"])})
    assert client.get(CONNECTIONS, headers=headers).status_code == 403


def test_f_forged_role_headers_with_admin_values_and_no_permissions_are_403() -> None:
    forged = _identity(**{
        "X-User-Role": "DataConnect Admin",
        "X-User-Roles": _b64(["admin", "dataconnect-admin", "DataConnect Admin", "platform-admin"]),
    })
    assert client.get(CONNECTIONS, headers=forged).status_code == 403
    assert client.post(CONNECTIONS, headers=forged, json={"name": "x", "source_type": "postgres"}).status_code == 403


def test_g_health_without_anything_is_200() -> None:
    for path in (HEALTH, f"{HEALTH}/live"):
        assert client.get(path).status_code == 200, path


# ── GET /me ──────────────────────────────────────────────────────────────────

def test_me_returns_the_contract_body() -> None:
    perms = sorted(PERSONAS["viewer"])
    resp = client.get("/api/v1/dataconnect/me", headers=_identity(**{"X-User-Permissions": _b64(list(reversed(perms)))}))
    assert resp.status_code == 200
    assert resp.json() == {"user": "alice", "userId": "u-alice", "tenant": "org-a", "permissions": perms}


def test_me_with_no_permissions_is_200_and_empty() -> None:
    resp = client.get("/api/v1/dataconnect/me", headers=_identity(**{"X-User-Roles": "admin"}))
    assert resp.status_code == 200
    assert resp.json()["permissions"] == []


def test_me_without_the_gateway_is_401() -> None:
    assert client.get("/api/v1/dataconnect/me").status_code == 401


def test_the_old_capabilities_endpoint_is_gone() -> None:
    assert client.get("/api/v1/dataconnect/access/capabilities", headers=gateway_headers()).status_code == 404


# ── tenant and actor come from the principal ─────────────────────────────────

def test_the_tenant_is_the_gateway_tenant_and_a_client_x_org_id_is_ignored(tenants_seen: list[str]) -> None:
    headers = {**gateway_headers(tenant_id="org-a"), "X-Org-Id": "org-b"}
    assert client.get(CONNECTIONS, headers=headers).status_code == 200
    assert tenants_seen == ["org-a"]


def test_an_invalid_gateway_tenant_is_rejected() -> None:
    assert client.get(CONNECTIONS, headers=gateway_headers(tenant_id="a b")).status_code == 400


def test_metrics_are_behind_the_gateway_by_default() -> None:
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=gateway_headers()).status_code == 200


def test_docs_are_behind_the_gateway() -> None:
    assert client.get("/openapi.json").status_code == 401
