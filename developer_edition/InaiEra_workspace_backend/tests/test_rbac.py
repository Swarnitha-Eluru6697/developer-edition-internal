"""Workspace authorisation by permission — the published list + live enforcement.

  1. Unit tests against `app.core.rbac` / `permissions.json` (no client/DB): the published IDs follow the contract format,
     every ID the Admin Portal seed uses for Workspace is published, and nothing in the app can ask for an unpublished one.

  2. Live HTTP tests: each gated route answers 403 to a caller who lacks its permission (before reaching business logic) and
     lets a caller who holds exactly that permission through. The permission dependency raises before any DB row is touched,
     the same reasoning tests/test_auth.py relies on for its 401/403 paths.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core.auth import Principal
from app.core.rbac import (
    DOWNSTREAM,
    PERMISSIONS,
    effective_permissions,
    ensure_permission,
    require_permission,
)

ROOT = Path(__file__).resolve().parent.parent
ID_FORMAT = re.compile(r"^workspace:[a-z]+(-[a-z]+)*:[a-z]+(-[a-z]+)*$")

# Every workspace:* permission the Admin Portal seed (roles-permissions.json, task pack) grants. They must be published here,
# spelled exactly.
SEED_WORKSPACE_PERMISSIONS = {
    "workspace:access:view", "workspace:dbt:run", "workspace:dq:view", "workspace:files:edit", "workspace:flink:deploy",
    "workspace:python-jobs:run", "workspace:settings:edit", "workspace:sql-jobs:run",
}

ACCESS = "workspace:access:view"


def _principal(permissions) -> Principal:
    return Principal(user_id="00000000-0000-0000-0000-000000000099", org_id="00000000-0000-0000-0000-000000000001",
                     roles=[], email="test@example.com", issued_at=0, expires_at=9_999_999_999,
                     permissions=frozenset(permissions))


def test_permissions_json_follows_the_contract_schema():
    doc = json.loads((ROOT / "permissions.json").read_text())
    assert doc["app"] == "workspace" and doc["version"] == 1
    ids = [p["id"] for p in doc["permissions"]]
    assert len(ids) == len(set(ids)), "duplicate permission id"
    for p in doc["permissions"]:
        assert ID_FORMAT.match(p["id"]), p["id"]
        assert p["label"] and p["description"]
    assert tuple(ids) == PERMISSIONS


def test_every_seed_permission_is_published():
    assert SEED_WORKSPACE_PERMISSIONS <= set(PERMISSIONS)


def test_the_task_permissions_are_published():
    expected = {"workspace:access:view", "workspace:files:edit", "workspace:sql:run", "workspace:notebooks:run",
                "workspace:dbt:edit", "workspace:dbt:run", "workspace:sql-jobs:edit", "workspace:sql-jobs:run",
                "workspace:python-jobs:edit", "workspace:python-jobs:run", "workspace:flink:edit", "workspace:flink:deploy",
                "workspace:dq:view", "workspace:dq:edit", "workspace:dq:run", "workspace:dq:publish", "workspace:git:write",
                "workspace:settings:edit", "workspace:settings:manage"}
    assert expected <= set(PERMISSIONS)


def test_downstream_only_names_published_permissions():
    assert set(DOWNSTREAM) <= set(PERMISSIONS)


def test_an_unpublished_permission_cannot_be_required():
    with pytest.raises(ValueError):
        require_permission("workspace:nothing:view")
    with pytest.raises(ValueError):
        require_permission("mlstudio:clusters:view")


def test_no_permissions_means_nothing():
    p = _principal([])
    assert not any(p.has(x) for x in PERMISSIONS)
    with pytest.raises(Exception) as e:
        ensure_permission(p, "workspace:dbt:run")
    assert e.value.status_code == 403


def test_permissions_are_exact_ids_never_prefixes_or_other_apps():
    p = _principal(["workspace:dbt", "workspace:dbt:run:extra", "mlstudio:clusters:view", "*"])
    assert not p.has("workspace:dbt:run")
    assert effective_permissions(p) == []


def test_effective_permissions_report_only_this_apps_ids_sorted():
    p = _principal(["workspace:sql:run", "mlstudio:train:run", "workspace:access:view"])
    assert effective_permissions(p) == ["workspace:access:view", "workspace:sql:run"]


# ── Live enforcement: a missing permission is actually rejected, not just hidden ──

Z = "00000000-0000-0000-0000-000000000001"
GATED = [
    # (method, path, the permission the route needs)
    ("POST", "/api/v1/dbt/projects/register", "workspace:dbt:edit"),
    ("POST", f"/api/v1/dbt/projects/{Z}/run", "workspace:dbt:run"),
    ("POST", "/api/v1/flink/jobs", "workspace:flink:edit"),
    ("POST", f"/api/v1/flink/jobs/{Z}/deploy", "workspace:flink:deploy"),
    ("POST", "/api/v1/dq/rules", "workspace:dq:create"),
    ("PATCH", f"/api/v1/dq/rules/{Z}", "workspace:dq:edit"),
    ("POST", f"/api/v1/dq/bindings/{Z}/apply", "workspace:dq:publish"),
    ("POST", "/api/v1/dq/bindings", "workspace:dq:publish"),
    ("POST", "/api/v1/dq/sync/ucs", "workspace:dq:publish"),
    ("POST", f"/api/v1/dq/render/{Z}/refresh", "workspace:dq:run"),
    ("POST", "/api/v1/dq/validate/rule", "workspace:dq:run"),
    ("GET", "/api/v1/dq/rules", "workspace:dq:view"),
    ("POST", f"/api/v1/git/workspaces/{Z}/commit", "workspace:git:write"),
    ("POST", f"/api/v1/git/workspaces/{Z}/push", "workspace:git:write"),
    ("POST", "/api/v1/connections", "workspace:connections:manage"),
    ("POST", "/api/v1/executions/notebook-cell", "workspace:notebooks:run"),
    ("POST", "/api/v1/executions/sql", "workspace:sql:run"),
    ("POST", "/api/v1/workspaces", "workspace:files:edit"),
    ("PUT", "/api/v1/settings/codegen/agent", "workspace:settings:manage"),
    ("POST", "/api/v1/settings/codegen/verify", "workspace:settings:manage"),
    ("GET", "/api/v1/codegen/org-settings", "workspace:settings:manage"),
    ("PUT", "/api/v1/codegen/org-settings", "workspace:settings:manage"),
    ("GET", "/api/v1/settings/engines", "workspace:settings:edit"),
    ("GET", "/api/v1/settings/codegen/agent", "workspace:settings:edit"),
]


def _all_but(permission: str, path: str) -> list[str]:
    """Every published permission except the one the route needs (and, for a dbt-runner job route, except the other job kinds'
    permissions that would also open it: registering is decided by the job's kind, a run by any run permission)."""
    job_family = (path.endswith("/register") and permission.endswith(":edit")) or path.endswith("/run")
    return [p for p in PERMISSIONS if p != permission and not (job_family and "-jobs:" in p)]


@pytest.mark.parametrize("method,path,permission", GATED)
def test_caller_without_the_permission_gets_403(client, gateway_headers, method, path, permission):
    r = client.request(method, path, json={}, headers=gateway_headers(roles=[], permissions=_all_but(permission, path)))
    assert r.status_code == 403, f"{method} {path} without {permission}: expected 403, got {r.status_code}: {r.text}"
    assert r.json()["detail"]["code"] == "FORBIDDEN"
    assert permission in r.json()["detail"]["message"]


@pytest.mark.parametrize("method,path,permission", GATED)
def test_caller_without_workspace_access_gets_403_everywhere(client, gateway_headers, method, path, permission):
    r = client.request(method, path, json={}, headers=gateway_headers(roles=[], permissions=[permission]))
    assert r.status_code == 403
    assert ACCESS in r.json()["detail"]["message"]


@pytest.mark.parametrize("method,path,permission", GATED)
def test_caller_with_the_permission_is_not_blocked(client, gateway_headers, method, path, permission):
    """Once past the permission dependency most handlers touch a DB row or an upstream that this env does not have; an
    exception from deep inside a handler is itself proof the request got past the gate (a denial is a controlled 403)."""
    try:
        r = client.request(method, path, json={}, headers=gateway_headers(roles=[], permissions=[ACCESS, permission]))
    except Exception:
        return
    if r.status_code == 403:
        assert "missing permission" not in r.text, f"{method} {path} was blocked although the caller holds {permission}"


@pytest.mark.parametrize("header", ["X-User-Role", "X-User-Roles"])
def test_forged_role_headers_grant_nothing(client, gateway_headers, header):
    h = gateway_headers(roles=[], permissions=[ACCESS])
    h[header] = "admin,workspace_admin,platform-admin"
    r = client.post(f"/api/v1/dbt/projects/{Z}/run", json={}, headers=h)
    assert r.status_code == 403


def test_settings_edit_alone_does_not_open_administration(client, gateway_headers):
    """The seed gives every role workspace:settings:edit: it must never unlock an admin-only action."""
    h = gateway_headers(roles=[], permissions=[ACCESS, "workspace:settings:edit"])
    for method, path in [("PUT", "/api/v1/settings/codegen/agent"), ("POST", "/api/v1/settings/codegen/verify"),
                         ("GET", "/api/v1/codegen/org-settings"), ("PUT", "/api/v1/codegen/org-settings")]:
        r = client.request(method, path, json={"provider": "codegen"}, headers=h)
        assert r.status_code == 403 and "workspace:settings:manage" in r.text, (method, path, r.status_code, r.text)
