"""Resource/data policy for production actions (X-006): UCS governs what Workspace RBAC only coarsely allows.

Workspace RBAC (app.core.rbac) is the COARSE PRODUCT gate: "may a data engineer deploy Flink jobs at all". UCS is the
RESOURCE/DATA POLICY gate: "may this person deploy THIS job, run THIS project, publish THIS rule set, push THIS repository,
against this data". Before this module only data access (a connection's `query`) went to UCS; every execution and deployment
action was a local role decision. Now every dbt / Flink / Data Quality / Git route names the product action it performs
(`ROUTE_ACTIONS`), and the capability dependency asks UCS about it. A test fails if a route is added to those
routers without an entry or without a capability dependency, so the rule cannot be forgotten route by route.

Product action -> UCS. UCS decides on its own action vocabulary (read / write / query / create_dataset / publish_dataset) against
an object (`catalog.schema.object`). Each product action maps onto one of those (`ACTIONS`); the object is the target data object
when Workspace knows it (a DQ binding's dataset) and otherwise a stable pseudo object `<data_policy_catalog>.<kind>.<id>` that
governance can grant (e.g. `inaiera_workspace.flink_job.*`). The product action name, the environment, the dataset's
classification, the resource owner and the target object travel as `context` in the request body (a UCS that does not read it yet
ignores it) and are written to Workspace's audit log with the decision.

The gate runs INSIDE the permission dependency (app.core.rbac.require_permission), after the permission check: a caller without
the route's permission is refused without UCS being asked, and one who holds it is then governed by UCS.

Modes (Settings.data_policy_mode): off | audit (decision recorded, never blocks; the default) | enforce (denial = 403
DATA_POLICY_DENIED, UCS unreachable or not configured = 503 DATA_POLICY_UNAVAILABLE: fail closed, never fail open).
`data_policy_enforced_actions` narrows enforce to some actions so a rollout can go action by action.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import structlog
from fastapi import Depends, Request

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.deps import DB, get_current_user
from app.core.errors import WorkspaceError

log = structlog.get_logger()


@dataclass(frozen=True)
class PolicyAction:
    name: str          # the product action (dbt.run, flink.deploy, ...)
    ucs_action: str    # UCS's own vocabulary: read | write | query | create_dataset | publish_dataset
    kind: str          # the kind of resource it acts on


def _a(name: str, ucs_action: str, kind: str) -> tuple[str, PolicyAction]:
    return name, PolicyAction(name, ucs_action, kind)


ACTIONS: dict[str, PolicyAction] = dict([
    _a("dbt.project.read", "read", "dbt_project"),
    _a("dbt.project.edit", "write", "dbt_project"),
    _a("dbt.run", "write", "dbt_project"),
    _a("dbt.deploy", "write", "dbt_project"),             # no separate production-deploy route exists yet; kept so it can be attached
    _a("flink.job.read", "read", "flink_job"),
    _a("flink.job.edit", "write", "flink_job"),
    _a("flink.deploy", "write", "flink_job"),
    _a("flink.cancel", "write", "flink_job"),
    _a("flink.savepoint", "write", "flink_job"),           # stop-with-savepoint
    _a("dq.rule.read", "read", "dq_rule"),
    _a("dq.rule.edit", "write", "dq_rule"),
    _a("dq.publish", "write", "dq_binding"),               # approve a custom rule, bind, apply to a runner, register checks in the catalog
    _a("dq.execute", "query", "dq_binding"),               # evaluate / re-render without changing what runs
    _a("dataset.read", "read", "dataset"),
    _a("dataset.write", "write", "dataset"),
    _a("dataset.preview", "query", "dataset"),
    _a("git.repo.read", "read", "git_repo"),
    _a("git.repo.write", "write", "git_repo"),
])

# Dataset actions are enforced where the data is touched (app.services.ucs_client.authorize_connection_use: a connection's
# catalog object is asked for UCS "query" before any SQL, preview or catalog browse runs) — they are named here so the vocabulary
# is one list, and `tests/test_data_policy.py` pins which routes use which path.
INLINE_DATASET_ACTIONS = frozenset({"dataset.read", "dataset.write", "dataset.preview"})

# (HTTP method, route path) -> product action. None = deliberately not gated (see EXEMPT_REASONS).
ROUTE_ACTIONS: dict[tuple[str, str], str | None] = {
    # dbt
    ("GET", "/api/v1/dbt/projects"): "dbt.project.read",
    ("POST", "/api/v1/dbt/projects/register"): "dbt.project.edit",
    ("GET", "/api/v1/dbt/projects/{project_id}"): "dbt.project.read",
    ("PATCH", "/api/v1/dbt/projects/{project_id}"): "dbt.project.edit",
    ("DELETE", "/api/v1/dbt/projects/{project_id}"): "dbt.project.edit",
    ("GET", "/api/v1/dbt/projects/{project_id}/lineage"): "dbt.project.read",
    ("POST", "/api/v1/dbt/projects/{project_id}/run"): "dbt.run",
    ("GET", "/api/v1/dbt/projects/{project_id}/runs"): "dbt.project.read",
    ("GET", "/api/v1/dbt/projects/{project_id}/runs/{run_id}"): "dbt.project.read",
    ("GET", "/api/v1/dbt/projects/{project_id}/runs/{run_id}/events"): "dbt.project.read",
    # Flink
    ("GET", "/api/v1/flink/jobs"): "flink.job.read",
    ("POST", "/api/v1/flink/jobs"): "flink.job.edit",
    ("POST", "/api/v1/flink/jobs/from-git"): "flink.job.edit",
    ("GET", "/api/v1/flink/jobs/{job_id}"): "flink.job.read",
    ("PATCH", "/api/v1/flink/jobs/{job_id}"): "flink.job.edit",
    ("DELETE", "/api/v1/flink/jobs/{job_id}"): "flink.job.edit",
    ("POST", "/api/v1/flink/jobs/{job_id}/cancel"): "flink.cancel",
    ("POST", "/api/v1/flink/jobs/{job_id}/deploy"): "flink.deploy",
    ("GET", "/api/v1/flink/jobs/{job_id}/status"): "flink.job.read",
    ("POST", "/api/v1/flink/jobs/{job_id}/stop"): "flink.savepoint",
    ("POST", "/api/v1/flink/jobs/{job_id}/sync"): "flink.job.edit",
    ("POST", "/api/v1/flink/jobs/{job_id}/validate"): "flink.job.edit",
    ("GET", "/api/v1/flink/ui"): "flink.job.read",
    ("POST", "/api/v1/flink/validate"): "flink.job.edit",
    # Data Quality
    ("GET", "/api/v1/dq/rules"): "dq.rule.read",
    ("POST", "/api/v1/dq/rules"): "dq.rule.edit",
    ("GET", "/api/v1/dq/rules/{rule_id}"): "dq.rule.read",
    ("PATCH", "/api/v1/dq/rules/{rule_id}"): "dq.rule.edit",
    ("POST", "/api/v1/dq/rules/{rule_id}/approve"): "dq.publish",
    ("DELETE", "/api/v1/dq/rules/{rule_id}"): "dq.rule.edit",
    ("GET", "/api/v1/dq/rulesets"): "dq.rule.read",
    ("POST", "/api/v1/dq/rulesets"): "dq.rule.edit",
    ("GET", "/api/v1/dq/rulesets/{ruleset_id}"): "dq.rule.read",
    ("PATCH", "/api/v1/dq/rulesets/{ruleset_id}"): "dq.rule.edit",
    ("DELETE", "/api/v1/dq/rulesets/{ruleset_id}"): "dq.rule.edit",
    ("GET", "/api/v1/dq/rulesets/{ruleset_id}/capability"): "dq.rule.read",
    ("GET", "/api/v1/dq/rulesets/{ruleset_id}/items"): "dq.rule.read",
    ("POST", "/api/v1/dq/rulesets/{ruleset_id}/items"): "dq.rule.edit",
    ("PATCH", "/api/v1/dq/rulesets/{ruleset_id}/items/{item_id}"): "dq.rule.edit",
    ("DELETE", "/api/v1/dq/rulesets/{ruleset_id}/items/{item_id}"): "dq.rule.edit",
    ("GET", "/api/v1/dq/bindings"): "dq.rule.read",
    ("POST", "/api/v1/dq/bindings"): "dq.publish",
    ("GET", "/api/v1/dq/bindings/{binding_id}"): "dq.rule.read",
    ("DELETE", "/api/v1/dq/bindings/{binding_id}"): "dq.publish",
    ("POST", "/api/v1/dq/bindings/{binding_id}/resolve"): "dq.publish",
    ("POST", "/api/v1/dq/bindings/{binding_id}/apply"): "dq.publish",
    ("GET", "/api/v1/dq/render/{binding_id}"): "dq.rule.read",
    ("POST", "/api/v1/dq/render/{binding_id}/refresh"): "dq.execute",
    ("POST", "/api/v1/dq/sync/ucs"): "dq.publish",
    ("POST", "/api/v1/dq/validate/rule"): "dq.execute",
    # Git
    ("GET", "/api/v1/git/connections"): "git.repo.read",
    ("DELETE", "/api/v1/git/connections/{provider}"): "git.repo.write",
    ("POST", "/api/v1/git/webhooks/{provider}"): None,
    ("POST", "/api/v1/git/workspaces/from-git"): "git.repo.read",
    ("GET", "/api/v1/git/workspaces/{workspace_id}/binding"): "git.repo.read",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/binding"): "git.repo.write",
    ("PATCH", "/api/v1/git/workspaces/{workspace_id}/branch"): "git.repo.write",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/branches"): "git.repo.write",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/commit"): "git.repo.write",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/pull"): "git.repo.read",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/pull-requests"): "git.repo.write",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/push"): "git.repo.write",
    ("GET", "/api/v1/git/workspaces/{workspace_id}/status"): "git.repo.read",
    ("POST", "/api/v1/git/workspaces/{workspace_id}/token"): "git.repo.write",
    ("GET", "/api/v1/git/{provider}/callback"): None,
    ("GET", "/api/v1/git/{provider}/connect"): None,
    ("GET", "/api/v1/git/{provider}/repos"): "git.repo.read",
}

# Why a route is not gated. Each one is authenticated by something other than a user's session, or only starts a login.
EXEMPT_REASONS = {
    ("POST", "/api/v1/git/webhooks/{provider}"): "authenticated by the provider's webhook signature, not by a user",
    ("GET", "/api/v1/git/{provider}/callback"): "the OAuth redirect back from the provider; it only completes a login",
    ("GET", "/api/v1/git/{provider}/connect"): "starts the OAuth login with the provider; touches no repository",
}

_PATH_ID_PARAMS = ("project_id", "job_id", "binding_id", "rule_id", "ruleset_id", "workspace_id", "provider")


class PolicyDenied(WorkspaceError):
    def __init__(self, message: str, *, action: str, object_name: str):
        super().__init__(403, "DATA_POLICY_DENIED", message)
        self.action, self.object_name = action, object_name


# After UCS fails, audit-mode requests stop asking for a while so an outage does not add a timeout to every call.
_UCS_BACKOFF_SECONDS = 30.0
_ucs_down_until = 0.0


def reset_backoff() -> None:
    global _ucs_down_until
    _ucs_down_until = 0.0


def pseudo_object(kind: str, resource_id: str | None) -> str:
    """The object UCS is asked about when the action has no data object of its own: `<catalog>.<kind>.<id>`."""
    return f"{get_settings().data_policy_catalog}.{kind}.{resource_id or '*'}"


def _enforced(action: str) -> bool:
    s = get_settings()
    return s.data_policy_mode == "enforce" and (not s.data_policy_enforced_actions or action in s.data_policy_enforced_actions)


async def _classification(principal: Principal, fqn: str) -> list[str] | None:
    """The classification tags UCS holds for a 3-part data object, as policy context. Best effort: it is information for the
    decision, not part of it, so a failure leaves it unknown (None) and never blocks."""
    if fqn.count(".") != 2:
        return None
    from app.services.ucs_client import _identity_headers, _ucs_configured

    s = get_settings()
    if not _ucs_configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=s.data_policy_timeout_seconds) as client:
            r = await client.get(f"{s.ucs_service_url}/api/v1/classification", params={"fqn": fqn},
                                 headers=_identity_headers(principal))
            r.raise_for_status()
            items = r.json().get("classifications") or []
            return sorted({str(i.get("tag") or i.get("classification") or i) for i in items})
    except (httpx.HTTPError, ValueError):
        return None


async def _target_object(request: Request) -> str | None:
    """The target data object a request names, when it carries one (a DQ binding's dataset)."""
    if request.method not in ("POST", "PATCH", "PUT"):
        return None
    try:
        body = await request.json()
    except Exception:        # noqa: BLE001 — no body / not JSON: nothing to read
        return None
    if isinstance(body, dict):
        for key in ("ucs_fqn", "target_fqn", "target_resource_id"):
            v = body.get(key)
            if isinstance(v, str) and v.count(".") == 2:
                return v
    return None


async def ask_ucs(principal: Principal, *, ucs_action: str, object_name: str, context: dict[str, Any]) -> tuple[bool, str]:
    """(allowed, reason) from UCS. Raises PolicyUnavailable when UCS cannot answer."""
    from app.services.ucs_client import _identity_headers, _ucs_configured

    s = get_settings()
    if not _ucs_configured():
        raise PolicyUnavailable("UCS is not configured for this deployment")
    try:
        async with httpx.AsyncClient(timeout=s.data_policy_timeout_seconds) as client:
            r = await client.post(f"{s.ucs_service_url}/api/v1/authz/check",
                                  json={"action": ucs_action, "object": object_name, "context": context},
                                  headers=_identity_headers(principal))
            r.raise_for_status()
            decision = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise PolicyUnavailable("Could not reach UCS to authorize this action") from e
    return decision.get("decision") == "allow", str(decision.get("reason") or "")


class PolicyUnavailable(WorkspaceError):
    def __init__(self, message: str):
        super().__init__(503, "DATA_POLICY_UNAVAILABLE", message)


async def evaluate(principal: Principal, db: Any, *, action: str, kind: str, resource_id: str | None,
                   object_name: str | None, route: str, target_object: str | None = None,
                   resource_owner: str | None = None) -> None:
    """Apply the policy for one action. Returns when the action may proceed; raises PolicyDenied / PolicyUnavailable when it
    may not (enforce) — in audit mode it never raises."""
    global _ucs_down_until
    s = get_settings()
    if s.data_policy_mode == "off":
        return
    spec = ACTIONS[action]
    enforced = _enforced(action)
    obj = object_name or target_object or pseudo_object(kind, resource_id)
    if not enforced and time.monotonic() < _ucs_down_until:
        return                                              # audit mode while UCS is down: do not slow every request
    context: dict[str, Any] = {
        "product_action": action, "environment": s.environment, "org_id": principal.org_id, "resource_kind": kind,
        "resource_id": resource_id, "resource_owner": resource_owner, "target_data_object": target_object,
        "classification": await _classification(principal, target_object) if target_object else None,
        "route": route,
    }
    try:
        allowed, reason = await ask_ucs(principal, ucs_action=spec.ucs_action, object_name=obj, context=context)
    except PolicyUnavailable as e:
        log.error("data_policy_unavailable", action=action, object=obj, enforced=enforced, error=str(e))
        if enforced:
            raise
        _ucs_down_until = time.monotonic() + _UCS_BACKOFF_SECONDS
        return
    decision = "allow" if allowed else ("deny" if enforced else "would_deny")
    log.info("data_policy_decision", action=action, ucs_action=spec.ucs_action, object=obj, decision=decision, reason=reason,
             mode="enforce" if enforced else "audit")
    if decision != "allow":
        await _audit(principal, db, decision, action, kind, obj, reason, context)
    if not allowed and enforced:
        raise PolicyDenied(reason or "UCS denied this action", action=action, object_name=obj)


async def _audit(principal: Principal, db: Any, decision: str, action: str, kind: str, obj: str, reason: str,
                 context: dict[str, Any]) -> None:
    """A denied (or would-be-denied) action is an audit event. Written in its OWN transaction: the request's session is rolled
    back when a denial is raised as a 403, and a denial that left no trace would defeat the point. Never lets an audit problem
    change the outcome. (`db` is the request's session; it is deliberately not used.)"""
    try:
        from app.db.session import AsyncSessionLocal
        from app.services.audit_service import AuditService
        from app.services.workspace_service import WorkspaceService

        async with AsyncSessionLocal() as session:
            row = await WorkspaceService(session).ensure_user(principal)
            await AuditService(session).record(user_id=row.id, action=f"data_policy.{decision}", resource_type=kind,
                                               metadata={"product_action": action, "object": obj, "reason": reason, **context})
            await session.commit()
    except Exception as e:          # noqa: BLE001
        log.error("data_policy_audit_failed", action=action, error=str(e))


async def apply_route_policy(request: Request, user: Principal, db: Any) -> None:
    """Apply the data policy of the route being served (its entry in ROUTE_ACTIONS) for `user`. Called by the RBAC capability
    dependency AFTER the role check passed — so the order is always coarse product gate first, then resource/data policy — and
    at most once per request. A route with no capability dependency never reaches it, which `tests/test_data_policy.py` forbids
    for every route in the table."""
    if get_settings().data_policy_mode == "off" or getattr(request.state, "data_policy_done", False):
        return
    request.state.data_policy_done = True
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    key = (request.method, path)
    if key not in ROUTE_ACTIONS:
        return                      # not a governed route (the guard test pins which routes those are)
    action = ROUTE_ACTIONS[key]
    if action is None:
        return
    spec = ACTIONS[action]
    params = request.path_params
    resource_id = next((str(params[p]) for p in _PATH_ID_PARAMS if p in params), None)
    target = await _target_object(request)
    # the Workspace's own resources have a known owner: the caller (a workspace is personal, W-021)
    owner = str(user.user_id) if spec.kind == "git_repo" else None
    await evaluate(user, db, action=action, kind=spec.kind, resource_id=resource_id, object_name=None, route=f"{key[0]} {path}",
                   target_object=target, resource_owner=owner)


async def _policy_user(request: Request, user: Annotated[Principal, Depends(get_current_user)], db: DB) -> Principal:
    """The authenticated caller, after the route's data policy ran. For routes that have no role capability of their own (any
    role may use them; most Git routes), this is what puts them behind UCS too — same place in the order as the RBAC dependency."""
    await apply_route_policy(request, user, db)
    return user


_policy_user.applies_route_policy = True  # type: ignore[attr-defined]  # read by tests/test_data_policy.py

PolicyUser = Annotated[Principal, Depends(_policy_user)]
