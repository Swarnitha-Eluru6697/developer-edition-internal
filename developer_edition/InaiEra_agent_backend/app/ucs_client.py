"""
Real client for InAiEra's actual Unified Catalog Service (UCS) product —
not a reference implementation of its contract anymore. This calls a
genuinely separate, independently-built FastAPI service's real
`/api/v1/authz/check` endpoint over HTTP.

Dual-check per §7.6: the agent's own service principal AND the invoking
user must both be entitled — implemented here as two independent real calls
to UCS, not one shared lookup:

1. **Agent principal check** — `groups` passed explicitly, sourced from this
   repo's own `role` table (which groups this agent is bound to). UCS
   resolves the allow/deny purely from those groups' RBAC bindings.
2. **Invoking user check** — `groups` omitted; UCS resolves the user's real
   group membership from its own `custom_group_members` table
   independently. This is a genuinely separate lookup, not the same table
   checked twice — a user who personally has access does NOT grant an
   under-entitled agent access, and vice versa.

Requires a running UCS instance (see README for how this repo's tests stood
one up locally). Configure the URL via UCS_BASE_URL. If UCS is unreachable,
this raises rather than silently allowing or falling back to a local
check — a governance gate that fails open is worse than one that fails loud.
"""
import logging
import os
import re
from datetime import datetime
from typing import List, Optional

import base64
import json

import httpx
from sqlmodel import Session, select
from .models import Role

logger = logging.getLogger(__name__)

UCS_BASE_URL = os.getenv("UCS_BASE_URL", "http://127.0.0.1:5174")
UCS_TENANT = os.getenv("UCS_TENANT", "prod_metalake")
# Same budget as the existing _authz_check call below; an authorization check
# sits in the request path, so it fails fast rather than hanging the caller.
UCS_TIMEOUT = float(os.getenv("UCS_TIMEOUT", "5.0"))
# UCS_ENABLED=false runs the platform without a Unified Catalog (Developer Edition): every
# authorization check allows, and catalog reads fail fast as UCSUnavailable (callers already
# treat that as "couldn't ask") instead of waiting on a service that isn't there.
UCS_ENABLED = os.getenv("UCS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


class UCSUnavailable(Exception):
    pass


def _fqn_parts(catalog_object: str):
    return catalog_object


def gateway_secret() -> str:
    """The secret UCS checks in X-Inaiera-Gateway-Secret. UCS_GATEWAY_SECRET, as
    AI/ML Studio names it; ENVOY_INTERNAL_SECRET (this service's own inbound
    secret, the same value on a data plane) when that is not set. Read per call."""
    return os.getenv("UCS_GATEWAY_SECRET") or os.getenv("ENVOY_INTERNAL_SECRET") or ""


def _b64_list(values: List[str]) -> str:
    """A list header the way Envoy sets it (contract §4): base64-encoded JSON."""
    return base64.b64encode(json.dumps(list(values)).encode()).decode()


def gateway_headers(principal: str, groups: Optional[List[str]] = None,
                    roles: Optional[List[str]] = None,
                    permissions: Optional[List[str]] = None,
                    user_id: Optional[str] = None) -> dict:
    """The five-header gateway contract, for a call this service makes TO
    UCS (InaiEra_dataplane_GatewayAuth).

    UCS adopts the same shared library this service does
    (app/gateway_identity.py), so an outbound call should speak the same
    contract rather than a subset of it. This used to send three headers
    — name, groups and tenant — and omit both `x-user-roles` and
    `x-inaiera-gateway-secret`. Confirmed live against UCS on :5178: it
    answers 401 with no headers and 200 with those three, and returns
    200 for a WRONG secret as readily as the right one, so it is not
    verifying the secret today. Sending it is therefore preparation for
    when UCS enables its own check, not a security gain right now —
    worth stating plainly rather than implying the hop is authenticated.

    Two deliberate omissions rather than empty values:

    * the secret header is left out entirely when no gateway secret is
      configured (gateway_secret()). An empty one would fail UCS's
      check the moment it turns it on, converting calls that work today
      into 401s; absent is honest, empty is a trap.
    * `x-user-roles` is left out when the caller's roles are unknown.
      This service authorises by permission, never by role name, so it
      has no role to assert; absent lets UCS apply its own default, and
      the decision here is group-based anyway.

    `permissions` are what UCS authorises on (`ucs:<area>:<action>`): the
    service identity's admin permissions, or a producer permission for
    telemetry. `user_id` is sent as X-User-Id when known.
    """
    headers = {
        "X-User-Name": principal,
        "X-User-Groups": ",".join(groups or []),
        "X-Tenant-Id": UCS_TENANT,
    }
    if user_id:
        headers["X-User-Id"] = user_id
    if permissions:
        headers["X-User-Permissions"] = _b64_list(permissions)
    secret = gateway_secret()
    if secret:
        headers["X-Inaiera-Gateway-Secret"] = secret
    if roles:
        headers["X-User-Roles"] = ",".join(roles)
    return headers


def _authz_check(principal: str, groups: Optional[List[str]], action: str, obj: str,
                 roles: Optional[List[str]] = None) -> dict:
    if not UCS_ENABLED:
        return {"decision": "allow", "reason": "UCS disabled (UCS_ENABLED=false)"}
    try:
        resp = httpx.post(
            f"{UCS_BASE_URL}/api/v1/authz/check",
            json={"principal": principal, "groups": groups, "action": action, "object": obj},
            headers=gateway_headers(principal, groups, roles),
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise UCSUnavailable(f"UCS at {UCS_BASE_URL} unreachable or errored: {e}")


# Each agent is its own catalog, named after the agent; the schema below
# is fixed inside it. UCS_AGENT_CATALOG is kept only as the fallback for
# an agent whose name yields no usable slug (see agent_slug).
UCS_AGENT_CATALOG = os.getenv("UCS_AGENT_CATALOG", "agent_catalog")
UCS_AGENT_SCHEMA = os.getenv("UCS_AGENT_SCHEMA", "agent")


_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


def agent_slug(agent_name: str) -> str:
    """The catalog name an agent is registered under, derived from its own
    name.

    A catalog per agent, named after the agent, is what makes the
    Explore tree readable — it used to be one shared catalog with every
    agent keyed by id, so the tree read `agent_catalog > agents > a4`.

    Slugified, not the literal name, because Gravitino refuses both of
    the things agent names actually contain. Measured against the
    running catalog:

        'Acme bot FAQ'  -> 400, operation [CREATE] refused
        'Acme.bot.FAQ'  -> 400, operation [CREATE] refused
        'acme-bot-faq'  -> created

    Dots are doubly impossible: UCS's rbac_cache.decide() splits an
    object on '.' into catalog/schema/object, so a dot inside the
    catalog name would silently re-cut the FQN and scope a grant to
    something else entirely.

    Falls back to the shared catalog when a name slugifies to nothing
    (all punctuation, or a non-latin script), rather than inventing a
    name — the agent stays governable either way.
    """
    slug = _SLUG_SAFE.sub("-", (agent_name or "").strip().lower()).strip("-")
    return slug or UCS_AGENT_CATALOG


def agent_fqn(agent_id: str, agent_name: str = "") -> str:
    """The real, dotted fully-qualified name an agent is registered
    under in UCS — must match how it's actually registered (see
    ucs_sync.py) for RBAC scoping to mean anything. Confirmed against
    the real UCS: rbac_cache.decide() splits the object string on '.'
    into catalog/schema/object, so a flat 'agent:{id}' string (the
    previous convention here) was silently treated as an all-wildcard
    catalog-level match, not a real per-agent scope.

    The catalog is the agent's own slug and the OBJECT is still the
    agent id: the id is what does not change when someone renames an
    agent, so it stays the thing a grant is anchored to.

    agent_name is optional only so an older caller keeps working; without
    it the FQN falls back to the shared catalog, which is where agents
    registered before this change still live.
    """
    return f"{agent_slug(agent_name)}.{UCS_AGENT_SCHEMA}.{agent_id}"


# Where this platform's OWN knowledge bases live in the catalog.
#
# A KB created here (Integrations > KB Connections) is a collection of
# ingested documents, so a fileset catalog is its natural home — and
# app_datasets is exactly that (type=fileset), confirmed against the
# running Gravitino. This convention is not invented here either: the
# seed data's own retrieval nodes already pointed at
# `app_datasets.docs.support_kb`, i.e. catalog `app_datasets`, schema
# `docs`, object = the KB. That FQN denied only because the `docs`
# schema and that fileset were never actually created, not because the
# shape was wrong.
#
# Overridable so a deployment whose KBs are governed somewhere else
# doesn't have to take this default — and an individual KB can override
# it outright by declaring its own `catalog_object` (see
# flow_compiler.kb_catalog_object), which is what a KB backed by an
# already-governed table should do.
UCS_KB_CATALOG = os.getenv("UCS_KB_CATALOG", "app_datasets")
UCS_KB_SCHEMA = os.getenv("UCS_KB_SCHEMA", "docs")


def kb_fqn(kb_id: str) -> str:
    """The dotted, three-part name a platform-managed knowledge base is
    governed under. Three parts is not cosmetic: UCS's own
    rbac_cache.decide() splits an object on '.' into
    catalog/schema/object, so a bare KB name (or a display name like
    "ACME Support FAQ") is read as a catalog-level, all-wildcard match
    and denies against every real grant — see agent_fqn above, which
    had the identical bug."""
    return f"{UCS_KB_CATALOG}.{UCS_KB_SCHEMA}.{kb_id}"


def check_agent_access(invoking_user: str, agent_id: str, agent_name: str = "") -> dict:
    """Real UCS check for whether a specific end user may open a specific
    published agent as an app — the same `_authz_check` primitive
    `check_entitlement` above uses for data objects, applied to the real
    Gravitino Model an agent is registered as (see ucs_sync.py).
    Grant/revoke for this lives entirely in UCS's own console, same as
    every other entitlement in this platform — there is no local ACL
    table here to drift out of sync with it.

    Uses action="read", not "invoke" — confirmed against the real UCS:
    the USE_MODEL privilege's recognized actions are read/browse/query/
    list (core/privileges.py's PRIVILEGE_ACTIONS), so "invoke" matched
    nothing and every check silently denied regardless of real grants."""
    # The user's real groups, for exactly the reason spelled out in
    # check_entitlement below: UCS decides against the groups the CALLER
    # supplies, and answers "no matching grant" for every principal when
    # that list is None. This passed None, so no user could open any
    # published app however it was granted — the same fault, in the one
    # place it had not been fixed.
    try:
        from .ucs_sync import principal_groups
        user_groups = principal_groups(invoking_user)
    except Exception as e:
        logger.warning("Could not resolve UCS groups for %s (%s) — app access will be "
                       "denied for lack of membership, not for lack of a grant",
                       invoking_user, e)
        user_groups = None
    # Invoke is decided from the UCS level the user holds on the agent
    # (USE_AGENT / CREATE_AGENT_VERSION / CREATE_AGENT) — see
    # check_agent_capability. Grants carry no agent:* scopes any more, so
    # UCS's own /agents/check would answer "no agent role bound" for all.
    fqn = agent_fqn(agent_id, agent_name)
    try:
        d = check_agent_capability(invoking_user, agent_id, "invoke", agent_name,
                                   groups=user_groups)
        return {"agent_id": agent_id, "allowed": d["allowed"], "reason": d["reason"]}
    except UCSUnavailable as e:
        logger.warning("Agent invoke check unavailable for %s (%s) — falling back to the "
                       "data-tier read check", fqn, e)

    decision = _authz_check(invoking_user, user_groups, "read", fqn)
    return {"agent_id": agent_id, "allowed": decision["decision"] == "allow", "reason": decision.get("reason", "")}


def check_entitlement(session: Session, agent_id: str, agent_name: str,
                       invoking_user: str, catalog_object: str, tier: str = "select") -> dict:
    """Dual-check against the REAL UCS. Returns the same shape the harness
    already expects (`resolved`, `agent_principal_ok`, `invoking_user_ok`),
    but both flags now come from two independent real UCS calls."""
    action = {"select": "read", "writer": "write", "owner": "*"}.get(tier, "read")

    agent_groups = [r.group for r in session.exec(select(Role).where(Role.agent_id == agent_id)).all()]
    agent_decision = _authz_check(f"agent:svc-{agent_id}", agent_groups, action, catalog_object)
    agent_ok = agent_decision["decision"] == "allow"

    # The invoking user's real groups, resolved from UCS's own membership
    # (see ucs_sync.principal_groups for why this is necessary and why it
    # is not a bypass). This used to pass None, which UCS answers with
    # "no matching grant" for every principal without exception — so the
    # user half of this dual check could not pass for anybody, and every
    # publish was refused however the objects were granted.
    #
    # Function-scoped import: ucs_sync imports from this module at its
    # own top level, so importing it back here at module level is a real
    # circular import (same reason as resolve_entitlement_plan below).
    #
    # Fails closed: if membership cannot be read, the groups stay None and
    # the decision is a denial, which is the safe direction. It is logged
    # rather than swallowed so it is distinguishable from a user who
    # genuinely belongs to no group.
    try:
        from .ucs_sync import principal_groups
        user_groups = principal_groups(invoking_user)
    except Exception as e:
        logger.warning("Could not resolve UCS groups for %s (%s) — the entitlement "
                       "check will deny for lack of membership, not for lack of a grant",
                       invoking_user, e)
        user_groups = None
    user_decision = _authz_check(invoking_user, user_groups, action, catalog_object)
    user_ok = user_decision["decision"] == "allow"

    return {
        "object": catalog_object,
        "tier_requested": tier,
        "agent_principal_ok": agent_ok,
        "agent_reason": agent_decision.get("reason", ""),
        "invoking_user_ok": user_ok,
        "user_reason": user_decision.get("reason", ""),
        "resolved": agent_ok and user_ok,
    }


def resolve_entitlement_plan(session: Session, agent_id: str, agent_name: str,
                              invoking_user: str, objects: List[str]) -> dict:
    # Real, function-scoped import (§0.49, GAP_REPORT) — harness.py
    # already imports from this module at its own module level
    # (`from .ucs_client import check_entitlement`), so importing
    # harness.py back at THIS module's own top level would be a real
    # circular import. Deferring it to call time is the standard, safe
    # way to share this one real constant without restructuring either
    # module — by the time anything actually calls this function, both
    # modules have already finished loading.
    from .harness import SUB_AGENT_HOP_CAP
    checks = [check_entitlement(session, agent_id, agent_name, invoking_user, obj) for obj in objects]
    return {
        "resolved": all(c["resolved"] for c in checks) if checks else True,
        "agent_principal_ok": all(c["agent_principal_ok"] for c in checks) if checks else True,
        "invoking_user_ok": all(c["invoking_user_ok"] for c in checks) if checks else True,
        "objects_checked": objects,
        "checks": checks,
        "chain_depth_cap": SUB_AGENT_HOP_CAP,
        "ucs_base_url": UCS_BASE_URL,
    }

# UCS agent action -> the agent permission (agent_roles.PERMISSIONS) that
# unlocks it. `delete` has no permission of its own: only CREATE_AGENT's
# wildcard reaches it.
AGENT_ACTION_PERMISSION = {
    "view": "agent:read",
    "invoke": "agent:invoke",
    "update_config": "agent:write",
    "create_version": "agent:version:create",
    "manage_versions": "agent:version:manage",
    "view_usage": "agent:usage:read",
    "manage_entitlements": "agent:entitlement:manage",
    "delete": "agent:*",
}


def _tier_from_model_privileges(privileges) -> Optional[str]:
    """The UCS level a set of agent privileges amounts to.

    UCS reports an agent's privileges under its agent labels (USE_AGENT …)
    on most endpoints and the Gravitino MODEL names elsewhere, so both
    spellings are read."""
    held = {str(p).upper().replace("_AGENT", "_MODEL") for p in (privileges or [])}
    if "CREATE_MODEL" in held:
        return "owner"
    if "CREATE_MODEL_VERSION" in held:
        return "writer"
    if "USE_MODEL" in held:
        return "select"
    return None


def agent_tier_held(invoking_user: str, agent_id: str, agent_name: str = "",
                    groups: Optional[List[str]] = None) -> dict:
    """The UCS level this user holds on this agent, read from UCS's own
    access-preview (the same answer UCS's Explore page shows).

    Agent grants carry only USE_AGENT / CREATE_AGENT_VERSION / CREATE_AGENT
    — the three privileges UCS shows — so what a user may do with an agent
    is decided here from that level, via the role table in agent_roles.py,
    rather than from agent:* scopes on the binding.
    """
    if groups is None:
        try:
            from .ucs_sync import principal_groups
            groups = principal_groups(invoking_user)
        except Exception as e:
            logger.warning("Could not resolve UCS groups for %s (%s)", invoking_user, e)
            groups = None
    fqn = agent_fqn(agent_id, agent_name)
    if not UCS_ENABLED:
        raise UCSUnavailable("UCS disabled (UCS_ENABLED=false)")
    try:
        resp = httpx.post(
            f"{UCS_BASE_URL}/api/v1/user/access-preview",
            json={"objects": [fqn]},
            headers=gateway_headers(invoking_user, groups),
            timeout=UCS_TIMEOUT,
        )
        resp.raise_for_status()
        row = (resp.json().get("access") or {}).get(fqn) or {}
    except Exception as e:
        raise UCSUnavailable(f"agent access for {fqn}: {e}") from e
    privileges = row.get("privileges") or []
    return {"agent": fqn, "tier": _tier_from_model_privileges(privileges),
            "privileges": privileges}


def agent_capabilities(invoking_user: str, agent_id: str, agent_name: str = "") -> dict:
    """Every agent capability this user holds on this agent, in one call.

    Use it to render (hide what cannot be done); use check_agent_capability
    to enforce. Both decide from the same UCS level, so they cannot disagree.
    """
    from .agent_roles import role_for_tier, role_has
    held = agent_tier_held(invoking_user, agent_id, agent_name)
    role = role_for_tier(held["tier"]) if held["tier"] else None
    return {"agent": held["agent"], "as": invoking_user, "tier": held["tier"],
            "privileges": held["privileges"],
            "capabilities": {a: bool(role and role_has(role, perm))
                             for a, perm in AGENT_ACTION_PERMISSION.items()}}


def check_agent_capability(invoking_user: str, agent_id: str, action: str,
                           agent_name: str = "", groups: Optional[List[str]] = None) -> dict:
    """Enforce ONE agent action for one user.

    `action` is a UCS agent action: view · invoke · update_config · create_version ·
    manage_versions · view_usage · manage_entitlements · delete.

    A deny is a normal answer, not an exception — the caller decides between 403 and a
    degraded view. `reason` names the level held and the one the action needs.
    """
    from .agent_roles import ROLES, role_for_tier, role_has
    perm = AGENT_ACTION_PERMISSION.get(action)
    if perm is None:
        return {"agent_id": agent_id, "action": action, "allowed": False,
                "reason": f"unknown agent action '{action}'"}
    held = agent_tier_held(invoking_user, agent_id, agent_name, groups)
    if not held["tier"]:
        return {"agent_id": agent_id, "action": action, "allowed": False,
                "reason": "no USE_AGENT, CREATE_AGENT_VERSION or CREATE_AGENT grant on this agent"}
    role = role_for_tier(held["tier"])
    if role_has(role, perm):
        return {"agent_id": agent_id, "action": action, "allowed": True,
                "reason": f"holds {ROLES[role].label} ({held['tier']})"}
    needed = next((r.label for r in ROLES.values() if role_has(r.name, perm)), "CREATE_AGENT")
    return {"agent_id": agent_id, "action": action, "allowed": False,
            "reason": f"holds {ROLES[role].label} — '{action}' needs {needed}"}


# --- lineage and quality -------------------------------------------------
#
# UCS is the catalog of record for both. The agent platform holds its own
# run history, but "what did this agent read, and what does the catalogue
# think of its quality" is a question about the governed object, not
# about our rows — so it is asked of UCS rather than recomputed here.
#
# Every function below fails SOFT. These feed a read-only panel: a
# catalogue that is down should grey that panel out, not break the drawer
# or the page behind it.

LINEAGE_NAMESPACE = os.getenv("UCS_LINEAGE_NAMESPACE", "inaiera.agents")
UCS_PRODUCER = "https://inaiera.dev/agent-platform"


def _ucs_get(path: str, principal: str = "system",
             groups: Optional[List[str]] = None,
             roles: Optional[List[str]] = None,
             params: Optional[dict] = None, timeout: float = 5.0) -> dict:
    if not UCS_ENABLED:
        raise UCSUnavailable("UCS disabled (UCS_ENABLED=false)")
    resp = httpx.get(
        f"{UCS_BASE_URL}{path}",
        headers=gateway_headers(principal, groups, roles),
        params=params or {}, timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# UCS accepts telemetry from a gateway-verified identity holding the route's producer
# permission (UCS app/producer_auth.py); the role below is kept for older UCS builds.
LINEAGE_EVENTS_CREATE = "ucs:lineage-events:create"
QUALITY_RESULTS_CREATE = "ucs:quality-results:create"


def _producer_roles(roles: Optional[List[str]]) -> List[str]:
    """Roles for a telemetry write: the caller's, plus the producer role UCS requires on
    /lineage/events and /quality/results (UCS PRODUCER_ROLES)."""
    return sorted(set(roles or []) | {os.getenv("UCS_PRODUCER_ROLE", "service")})


def emit_lineage_event(job_fqn: str, run_id: str, state: str,
                       inputs: Optional[List[str]] = None,
                       outputs: Optional[List[str]] = None,
                       principal: str = "system",
                       groups: Optional[List[str]] = None,
                       roles: Optional[List[str]] = None,
                       event_time: Optional[str] = None) -> dict:
    """Report one agent run to UCS as an OpenLineage RunEvent.

    Without this the catalogue has no idea an agent reads anything: the
    lineage graph for every agent came back `available` with no edges,
    because nothing had ever told UCS what it touched.

    Datasets are named by their catalogue FQN so the event joins onto
    objects UCS already governs — a knowledge base named
    `app_datasets.docs.scen_qa_hq` becomes an edge to that fileset, not
    to a new orphan node.

    Fail-soft, and deliberately so: reporting lineage is bookkeeping
    about a run that has already happened. A catalogue that is down must
    not fail the run it is being told about.
    """
    event = {
        "eventType": state,          # START | COMPLETE | FAIL | ABORT
        "eventTime": event_time or datetime.utcnow().isoformat() + "Z",
        "producer": UCS_PRODUCER,
        "run": {"runId": run_id},
        "job": {"namespace": LINEAGE_NAMESPACE, "name": job_fqn},
        "inputs": [{"namespace": LINEAGE_NAMESPACE, "name": n} for n in (inputs or [])],
        "outputs": [{"namespace": LINEAGE_NAMESPACE, "name": n} for n in (outputs or [])],
    }
    try:
        resp = httpx.post(
            f"{UCS_BASE_URL}/api/v1/lineage/events",
            # UCS accepts telemetry only from producers (service token or a producer role) —
            # an ordinary user identity can no longer write lineage. This platform IS the
            # producer for its runs, so it says so.
            json=event, headers=gateway_headers(principal, groups, _producer_roles(roles),
                                                permissions=[LINEAGE_EVENTS_CREATE]),
            timeout=5.0)
        resp.raise_for_status()
        return {"ok": True, "detail": resp.json()}
    except httpx.HTTPError as e:
        logger.warning("Could not report lineage for %s to UCS: %s", job_fqn, e)
        return {"ok": False, "detail": str(e)}


def push_quality_result(fqn: str, name: str, dimension: str, status: str,
                        observed=None, threshold=None, rows_scanned=None,
                        detail: str = "", run_ref: str = "",
                        principal: str = "system",
                        groups: Optional[List[str]] = None,
                        roles: Optional[List[str]] = None) -> dict:
    """Publish one measured quality dimension to the catalogue.

    The measurement is made by this platform — it is the thing that runs
    the evals — and sent here so an agent carries the same quality
    vocabulary as every other governed object.

    Fail-soft: the measurement is already recorded locally, so a
    catalogue that is unreachable must not turn it into an error.
    """
    body = {
        "fqn": fqn, "name": name, "dimension": dimension, "status": status,
        "engine": "inaiera-agent-platform", "detail": detail[:500],
    }
    if observed is not None:
        body["observed"] = observed
    if threshold is not None:
        body["threshold"] = threshold
    if rows_scanned is not None:
        body["rows_scanned"] = rows_scanned
    if run_ref:
        body["run_ref"] = run_ref
    try:
        resp = httpx.post(f"{UCS_BASE_URL}/api/v1/quality/results", json=body,
                          headers=gateway_headers(principal, groups, _producer_roles(roles),
                                                  permissions=[QUALITY_RESULTS_CREATE]),
                          timeout=5.0)
        resp.raise_for_status()
        return {"ok": True, "detail": resp.json()}
    except httpx.HTTPError as e:
        logger.warning("Could not publish quality for %s to UCS: %s", fqn, e)
        return {"ok": False, "detail": str(e)}
