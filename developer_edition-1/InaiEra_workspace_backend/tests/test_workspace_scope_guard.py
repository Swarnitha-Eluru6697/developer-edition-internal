"""X-005: workspace_id has ONE security meaning downstream, and a new route cannot quietly forget it.

The decision (docs/RESOURCE_SCOPING.md): the dbt and Flink registries are org-shared; `workspace_id` is a grouping label that
is forwarded only after Workspace checked the caller owns the workspace, and it reaches the runners as a signed token claim.
These guards keep that true as code is added:

  * every Workspace proxy route that can carry a workspace id awaits `authorize_workspace` (and forwards the VERIFIED id);
  * the runners' request models with a `workspace_id` are only reachable through the runners' own `authorize_workspace`
    (their guards live in each runner's tests);
  * the claim name Workspace signs is the claim name the runners read, and the two runners' rule is the same code.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

import pytest

from app.api import dbt_proxy, flink_proxy

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = Path(os.environ.get("RUNNERS_REPO", ROOT / "InaiEra_workspace_runners"))
WRITE_VERBS = {"post", "patch", "put", "delete"}


def _route_functions(module):
    """(function node, http verb) for every router-decorated async handler in a proxy module."""
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name) \
                    and dec.func.value.id == "router":
                yield node, dec.func.attr


def _calls(node, name):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and (
        (isinstance(c.func, ast.Name) and c.func.id == name) or (isinstance(c.func, ast.Attribute) and c.func.attr == name))]


def _mentions_workspace(node) -> bool:
    return any((isinstance(n, ast.Attribute) and n.attr == "workspace_id") or (isinstance(n, ast.Name) and n.id == "workspace_id")
               or (isinstance(n, ast.Constant) and n.value == "workspace_id") for n in ast.walk(node))


def _body_models_with_workspace(node, module):
    """Parameters annotated with a pydantic model of this module that has a `workspace_id` field."""
    hits = []
    for arg in node.args.args:
        name = getattr(arg.annotation, "id", None)
        model = getattr(module, name, None) if name else None
        if model is not None and hasattr(model, "model_fields") and "workspace_id" in model.model_fields:
            hits.append(name)
    return hits


# ---- Workspace's proxies -----------------------------------------------------
@pytest.mark.parametrize("module", [dbt_proxy, flink_proxy], ids=["dbt_proxy", "flink_proxy"])
def test_every_route_that_can_carry_a_workspace_id_authorizes_it_first(module):
    checked = 0
    for node, verb in _route_functions(module):
        carries = _mentions_workspace(node) or _body_models_with_workspace(node, module)
        takes_raw_body = verb in {"post", "patch", "put"} and any(a.arg == "request" for a in node.args.args) \
            and not _calls(node, "authorize_workspace") and node.name in {"register_project", "create_job"}
        if carries or takes_raw_body:
            checked += 1
            assert _calls(node, "authorize_workspace"), (
                f"{module.__name__}.{node.name} can forward a workspace_id but never awaits authorize_workspace: "
                "any same-org caller could attach a resource to another user's workspace (W-016 / X-005)")
    assert checked >= 1


def test_the_guard_sees_the_routes_it_is_meant_to_guard():
    names = {n.name for m in (dbt_proxy, flink_proxy) for n, _ in _route_functions(m)}
    assert {"register_project", "create_job", "create_job_from_git", "validate_sql"} <= names


def test_only_the_verified_workspace_id_is_ever_put_in_an_outgoing_body():
    """`body["workspace_id"] = ...` must use the result of authorize_workspace, never the request's own value."""
    for module in (dbt_proxy, flink_proxy):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                                                    and t.slice.value == "workspace_id" for t in node.targets):
                src = ast.unparse(node.value)
                assert "payload.workspace_id" not in src and "request" not in src, (module.__name__, src)


def test_a_route_added_without_the_check_would_be_caught(tmp_path):
    """Prove the guard itself works: a proxy that forwards a workspace id without authorizing is flagged."""
    bad = tmp_path / "bad_proxy.py"
    bad.write_text("from fastapi import APIRouter\nrouter = APIRouter()\n\n@router.post('/x')\nasync def make(payload, request):\n"
                   "    body = {'workspace_id': str(payload.workspace_id)}\n    return body\n")
    node, _ = next(_route_functions(types_module(bad)))
    assert _mentions_workspace(node) and not _calls(node, "authorize_workspace")


def types_module(path: Path):
    import types
    m = types.ModuleType("bad_proxy")
    m.__file__ = str(path)
    return m


# ---- the three repos agree ---------------------------------------------------
def _runner_file(runner: str, rel: str) -> Path:
    p = RUNNERS / runner / rel
    if not p.exists():
        pytest.skip(f"{runner} is not checked out next to this repo (set RUNNERS_REPO)")
    return p


def test_the_claim_workspace_signs_is_the_claim_both_runners_read():
    from jose import jwt

    from app.core.service_tokens import mint_delegated_token

    token = mint_delegated_token(secret="s", audience="a", scope="x", principal=type("P", (), {
        "user_id": "u", "org_id": "o", "roles": [], "email": None})(), workspace_id="11111111-1111-1111-1111-111111111111")
    claim = [k for k, v in jwt.get_unverified_claims(token).items() if v == "11111111-1111-1111-1111-111111111111"]
    assert claim == ["workspace_id"]
    for runner in ("inaiera-dbt-runner", "inaiera-flinksql-runner"):
        src = _runner_file(runner, "app/core/auth.py").read_text()
        assert 'claims["workspace_id"]' in src and "workspace_id: str | None" in src, runner


def test_both_runners_apply_the_same_workspace_rule():
    """The rule is copied (separate repos); a change to one must be a change to both."""
    a = _runner_file("inaiera-dbt-runner", "app/core/workspace_scope.py").read_bytes()
    b = _runner_file("inaiera-flinksql-runner", "app/core/workspace_scope.py").read_bytes()
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
