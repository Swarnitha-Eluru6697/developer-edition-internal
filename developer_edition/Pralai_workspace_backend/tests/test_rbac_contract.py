"""X-002 / W-009: one permission contract, and every place that enforces it checked against it.

`permissions.json` is the only place Workspace permission IDs are declared (contract 01-CONTRACT.md section 3). It is
exported with where each permission is enforced downstream to `contracts/rbac.contract.v1.json`; the frontend generates its
types from a copy of permissions.json; each runner checks the same IDs, carried in the delegated token. These tests fail when
the file, the frontend copy or a runner's table says something different. Sibling repos are checked when they are checked out
next to this one (or at RUNNERS_REPO / DQ_REPO / FRONTEND_REPO); a missing sibling skips its check with a reason.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.api import dq_proxy
from app.core import rbac, service_tokens

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent
RUNNERS = Path(os.environ.get("RUNNERS_REPO", ROOT / "Pralai_workspace_runners"))
DQ = Path(os.environ.get("DQ_REPO", ROOT / "Pralai_dataquality_backend"))
FRONTEND = Path(os.environ.get("FRONTEND_REPO", ROOT / "Pralai_workspace_frontend"))
CONTRACT_FILE = HERE / "contracts" / "rbac.contract.v1.json"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # the runners' rbac tables import nothing from their app at module level
    return module


# ── the contract itself ───────────────────────────────────────────────────
def test_the_committed_contract_is_the_one_the_code_produces():
    done = subprocess.run([sys.executable, str(HERE / "scripts" / "export_rbac_contract.py"), "--check", str(CONTRACT_FILE)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_contract_lists_every_published_permission_and_no_roles():
    c = rbac.rbac_contract()
    assert c["contract_version"] == rbac.CONTRACT_VERSION and c["app"] == "workspace"
    assert set(c["permissions"]) == set(rbac.PERMISSIONS)
    text = json.dumps(c)
    assert '"roles"' not in text and "role_semantics" not in text and "role_aliases" not in text


def test_the_role_matrix_route_is_gone(client, gateway_headers):
    assert client.get("/api/v1/auth/rbac-contract", headers=gateway_headers(roles=[])).status_code == 404


# ── Workspace itself: every DQ route needs the permission it should, and no other ──────────────
DQ_ROUTE_PERMISSION = {
    ("GET", "/api/v1/dq/rules"): "workspace:dq:view",
    ("POST", "/api/v1/dq/rules"): "workspace:dq:create",
    ("GET", "/api/v1/dq/rules/{rule_id}"): "workspace:dq:view",
    ("PATCH", "/api/v1/dq/rules/{rule_id}"): "workspace:dq:edit",
    ("POST", "/api/v1/dq/rules/{rule_id}/approve"): "workspace:dq:publish",
    ("DELETE", "/api/v1/dq/rules/{rule_id}"): "workspace:dq:edit",
    ("GET", "/api/v1/dq/rulesets"): "workspace:dq:view",
    ("POST", "/api/v1/dq/rulesets"): "workspace:dq:create",
    ("GET", "/api/v1/dq/rulesets/{ruleset_id}"): "workspace:dq:view",
    ("PATCH", "/api/v1/dq/rulesets/{ruleset_id}"): "workspace:dq:edit",
    ("DELETE", "/api/v1/dq/rulesets/{ruleset_id}"): "workspace:dq:edit",
    ("GET", "/api/v1/dq/rulesets/{ruleset_id}/capability"): "workspace:dq:view",
    ("GET", "/api/v1/dq/rulesets/{ruleset_id}/items"): "workspace:dq:view",
    ("POST", "/api/v1/dq/rulesets/{ruleset_id}/items"): "workspace:dq:create",
    ("PATCH", "/api/v1/dq/rulesets/{ruleset_id}/items/{item_id}"): "workspace:dq:edit",
    ("DELETE", "/api/v1/dq/rulesets/{ruleset_id}/items/{item_id}"): "workspace:dq:edit",
    ("GET", "/api/v1/dq/bindings"): "workspace:dq:view",
    ("POST", "/api/v1/dq/bindings"): "workspace:dq:publish",
    ("GET", "/api/v1/dq/bindings/{binding_id}"): "workspace:dq:view",
    ("DELETE", "/api/v1/dq/bindings/{binding_id}"): "workspace:dq:publish",
    ("POST", "/api/v1/dq/bindings/{binding_id}/resolve"): "workspace:dq:publish",
    ("POST", "/api/v1/dq/bindings/{binding_id}/apply"): "workspace:dq:publish",
    ("GET", "/api/v1/dq/render/{binding_id}"): "workspace:dq:view",
    ("POST", "/api/v1/dq/render/{binding_id}/refresh"): "workspace:dq:run",
    ("POST", "/api/v1/dq/sync/ucs"): "workspace:dq:publish",
    ("POST", "/api/v1/dq/validate/rule"): "workspace:dq:run",
}


def _dq_route_permissions() -> dict[tuple[str, str], set[tuple[str, ...]]]:
    found: dict[tuple[str, str], set[tuple[str, ...]]] = {}
    for route in dq_proxy.router.routes:
        if not isinstance(route, APIRoute):
            continue
        perms = {d.call.required_permissions for d in route.dependant.dependencies
                 if hasattr(d.call, "required_permissions")}
        for method in route.methods:
            path = route.path if route.path.startswith("/api/v1/dq") else "/api/v1/dq" + route.path
            found[(method, path)] = perms
    return found


def test_every_dq_route_requires_exactly_its_permission():
    found = _dq_route_permissions()
    assert found, "no DQ routes were found: the guard would be vacuous"
    assert all(len(v) == 1 and len(next(iter(v))) == 1 for v in found.values()), "each DQ route needs exactly one permission"
    assert {k: next(iter(v))[0] for k, v in found.items()} == DQ_ROUTE_PERMISSION


def test_no_dq_read_route_needs_authoring_authority_and_sync_is_not_rule_authoring():
    assert DQ_ROUTE_PERMISSION[("POST", "/api/v1/dq/sync/ucs")] == "workspace:dq:publish"
    reads = [k for k in DQ_ROUTE_PERMISSION if k[0] == "GET"]
    assert reads and all(DQ_ROUTE_PERMISSION[k] == "workspace:dq:view" for k in reads)


@pytest.mark.skipif(not (DQ / "app" / "core" / "permissions.py").exists(), reason="DQ repo not checked out next to this one")
def test_dq_permissions_named_by_the_contract_exist_in_dqs_vocabulary():
    text = (DQ / "app" / "core" / "permissions.py").read_text()
    vocab = ast.literal_eval(next(n for n in ast.parse(text).body if isinstance(n, ast.Assign)
                                  and getattr(n.targets[0], "id", "") == "ALL_PERMISSIONS").value.args[0])
    named = {p for doc in rbac.rbac_contract()["permissions"].values() for p in doc["downstream"].get("dataquality", [])}
    assert named and named <= set(vocab)


# ── the runners check the contract's IDs, from the token ─────────────────────
RUNNER_TABLES = [("pralai-dbt-runner", "dbtrunner"), ("pralai-flinksql-runner", "flinksql-runner")]


def _runner(repo: str):
    path = RUNNERS / repo / "app" / "core" / "permissions.py"
    if not path.exists():
        pytest.skip("runner repos not checked out next to this one")
    return _load(path, f"{repo.replace('-', '_')}_permissions")


@pytest.mark.parametrize("repo,service", RUNNER_TABLES)
def test_runner_checks_exactly_the_permissions_the_contract_names_for_it(repo, service):
    runner = _runner(repo)
    named = {p for p, doc in rbac.rbac_contract()["permissions"].items() if service in doc["downstream"]}
    assert set(runner.PERMISSIONS) == named
    assert set(runner.PERMISSIONS) <= set(rbac.PERMISSIONS), "a runner checks a permission Workspace does not publish"


@pytest.mark.parametrize("repo,service", RUNNER_TABLES)
def test_workspace_puts_in_each_token_what_the_runner_checks_for_that_scope(repo, service):
    runner = _runner(repo)
    for scope, needed in runner.SCOPE_PERMISSIONS.items():
        assert set(service_tokens.SCOPE_PERMISSIONS[scope]) == set(needed), f"{repo}: {scope}"


@pytest.mark.parametrize("repo,_service", RUNNER_TABLES)
def test_runner_tables_hold_no_role_names(repo, _service):
    runner = _runner(repo)
    assert not hasattr(runner, "ALL_ROLES") and not hasattr(runner, "CAPABILITY_MATRIX")
    assert not (RUNNERS / repo / "app" / "core" / "rbac.py").exists(), "the old role matrix module is still there"


# ── the frontend's copy ─────────────────────────────────────────────────────
@pytest.mark.skipif(not (FRONTEND / "openapi").exists(), reason="frontend repo not checked out next to this one")
def test_the_frontends_copy_of_the_permission_list_is_current():
    copy = FRONTEND / "openapi" / "permissions.json"
    assert copy.exists(), "copy permissions.json to <frontend>/openapi/permissions.json and run npm run codegen:rbac"
    assert json.loads(copy.read_text()) == json.loads((HERE / "permissions.json").read_text())
    assert not (FRONTEND / "openapi" / "rbac.contract.v1.json").exists(), "the frontend still ships the old role matrix"
