"""
Real sync from this platform's agents to UCS's real catalog — registers
each agent as a real Gravitino Model (via UCS's admin API, verified
working against the actual UCS service, not assumed), and each published
AgentVersion as a real Model version. "Who can use this agent" becomes a
real UCS RBAC role scoped to that Model, bound to a real group — the
same admin flow UCS already uses for tables/topics/filesets, pointed at
agents instead. See ucs_client.py's agent_fqn() for the naming
convention this must stay consistent with.

Authenticates as a service identity, the way AI/ML Studio does
(src/ucs/config.ts, ucsServiceIdentity): UCS authorises only gateway-
authenticated calls (docs/auth/01-CONTRACT.md), so every admin call carries
the gateway secret (UCS_GATEWAY_SECRET), a principal (UCS_SERVICE_USERNAME,
default agents-service) and the permissions it needs (ucs:catalogs:manage,
ucs:rbac:manage). No bearer token and no Keycloak login. Without a gateway
secret every function here raises a clear error rather than silently
skipping the sync or pretending it succeeded.
"""
import logging
import os
import re
import time
from typing import List, Optional

import httpx

from .ucs_client import (UCS_BASE_URL, UCS_AGENT_CATALOG, UCS_AGENT_SCHEMA, agent_fqn,
                         agent_slug, UCSUnavailable, gateway_headers, gateway_secret)

logger = logging.getLogger(__name__)

# The service identity every admin call is made as.
UCS_SERVICE_USERNAME = os.getenv("UCS_SERVICE_USERNAME", "agents-service")
UCS_SERVICE_USER_ID = os.getenv("UCS_SERVICE_USER_ID", "") or UCS_SERVICE_USERNAME
# Registration (catalogs, schemas, filesets, models, versions) and grants (roles,
# group bindings, the group listing) — the two UCS permissions those routes require.
UCS_SERVICE_PERMISSIONS = ["ucs:catalogs:manage", "ucs:rbac:manage"]

# Local-dev fallback only, used when no gateway secret is configured: a bearer
# token (UCS_ADMIN_TOKEN), or one minted from UCS_ADMIN_USERNAME/PASSWORD. A UCS
# that enforces the gateway contract refuses both.
UCS_ADMIN_TOKEN = os.getenv("UCS_ADMIN_TOKEN", "")
UCS_KEYCLOAK_URL = os.getenv("UCS_KEYCLOAK_URL", "http://host.docker.internal:8081")
UCS_KEYCLOAK_REALM = os.getenv("UCS_KEYCLOAK_REALM", "data-realm")
UCS_KEYCLOAK_CLIENT = os.getenv("UCS_KEYCLOAK_CLIENT", "inaiera-ucs")
UCS_ADMIN_USERNAME = os.getenv("UCS_ADMIN_USERNAME", "")
UCS_ADMIN_PASSWORD = os.getenv("UCS_ADMIN_PASSWORD", "")
# Keycloak must mint the token for the host UCS expects as `iss` (dev fallback).
UCS_KEYCLOAK_ISSUER_HOST = os.getenv("UCS_KEYCLOAK_ISSUER_HOST", "")

_minted_token: "Optional[str]" = None


def service_identity_configured() -> bool:
    """Whether admin calls can be made as the service identity (a gateway secret is set)."""
    return bool(gateway_secret())


def _bearer_fallback() -> bool:
    """The dev-only bearer path: no gateway secret, but a token or credentials to mint one."""
    return not service_identity_configured() and bool(
        UCS_ADMIN_TOKEN or (UCS_ADMIN_USERNAME and UCS_ADMIN_PASSWORD))


class UCSSyncError(Exception):
    pass


def mint_admin_token() -> str:
    """Obtain a fresh UCS admin-API token from the IdP."""
    if not (UCS_ADMIN_USERNAME and UCS_ADMIN_PASSWORD):
        raise UCSSyncError(
            "No UCS admin identity: set UCS_GATEWAY_SECRET (the gateway secret UCS checks) so "
            "admin calls are made as the service identity UCS_SERVICE_USERNAME. "
            "UCS_ADMIN_TOKEN / UCS_ADMIN_USERNAME+PASSWORD are a local-dev fallback only."
        )
    url = (f"{UCS_KEYCLOAK_URL.rstrip('/')}/realms/{UCS_KEYCLOAK_REALM}"
           f"/protocol/openid-connect/token")
    try:
        headers = {"Host": UCS_KEYCLOAK_ISSUER_HOST} if UCS_KEYCLOAK_ISSUER_HOST else None
        resp = httpx.post(url, data={
            "grant_type": "password", "client_id": UCS_KEYCLOAK_CLIENT,
            "username": UCS_ADMIN_USERNAME, "password": UCS_ADMIN_PASSWORD,
        }, headers=headers, timeout=15.0)
    except httpx.HTTPError as e:
        raise UCSUnavailable(f"could not reach the IdP at {UCS_KEYCLOAK_URL}: {e}")
    if resp.status_code >= 400:
        raise UCSSyncError(
            f"the IdP refused to issue an admin token ({resp.status_code}): {resp.text[:200]}")
    token = (resp.json() or {}).get("access_token")
    if not token:
        raise UCSSyncError("the IdP returned no access_token")
    return token


def _current_token(force_refresh: bool = False) -> str:
    global _minted_token
    if UCS_ADMIN_TOKEN and not force_refresh:
        return UCS_ADMIN_TOKEN
    if _minted_token is None or force_refresh:
        _minted_token = mint_admin_token()
    return _minted_token


def _admin_headers(force_refresh: bool = False) -> dict:
    """The service identity's gateway headers; the bearer token only as the dev fallback."""
    if service_identity_configured():
        return {**gateway_headers(UCS_SERVICE_USERNAME, permissions=UCS_SERVICE_PERMISSIONS,
                                  user_id=UCS_SERVICE_USER_ID),
                "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {_current_token(force_refresh)}",
            "Content-Type": "application/json"}


def _admin_post(path: str, body: dict, timeout: float = 20.0) -> dict:
    """POST to the UCS admin API. On the dev bearer fallback, a 401 (a minted token
    that has since expired) mints a fresh one and retries once."""
    def attempt(force_refresh: bool):
        try:
            return httpx.post(f"{UCS_BASE_URL}{path}", json=body,
                              headers=_admin_headers(force_refresh), timeout=timeout)
        except httpx.HTTPError as e:
            raise UCSUnavailable(f"UCS at {UCS_BASE_URL} unreachable: {e}")

    resp = attempt(False)
    if resp.status_code == 401 and _bearer_fallback() and UCS_ADMIN_USERNAME and UCS_ADMIN_PASSWORD:
        logger.info("UCS admin token rejected (401) — minting a fresh one and retrying %s", path)
        resp = attempt(True)
    if resp.status_code >= 400:
        raise UCSSyncError(f"POST {path} -> {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _admin_get(path: str, timeout: float = 10.0) -> dict:
    """GET from the UCS admin API, with _admin_post's dev-fallback 401 refresh."""
    def attempt(force_refresh: bool):
        try:
            return httpx.get(f"{UCS_BASE_URL}{path}",
                             headers=_admin_headers(force_refresh), timeout=timeout)
        except httpx.HTTPError as e:
            raise UCSUnavailable(f"UCS at {UCS_BASE_URL} unreachable: {e}")

    resp = attempt(False)
    if resp.status_code == 401 and _bearer_fallback() and UCS_ADMIN_USERNAME and UCS_ADMIN_PASSWORD:
        logger.info("UCS admin token rejected (401) — minting a fresh one and retrying %s", path)
        resp = attempt(True)
    if resp.status_code >= 400:
        raise UCSSyncError(f"GET {path} -> {resp.status_code}: {resp.text[:400]}")
    return resp.json()


# Group membership, read from UCS's own admin API.
#
# Why this exists: UCS's /api/v1/authz/check does NOT resolve a
# principal's group membership itself — it decides against the `groups`
# list the caller supplies. Confirmed against the running UCS: with
# groups=null it answers "no matching grant" for EVERY principal,
# including the real admin (padmin), even for an object whose role is
# genuinely bound to a group that principal belongs to. Supply the same
# principal's groups explicitly and the identical request is allowed
# ("granted via acme-docs-readers::kb-semantic_docs-select").
#
# So a caller that passes groups=None is not asking a laxer question, it
# is asking an unanswerable one, and the only possible reply is a denial.
# The agent half of check_entitlement already supplied groups (from the
# local Role table); the invoking-user half passed None, which is why no
# user could ever be entitled to anything no matter what was granted.
#
# This is a relay, not a bypass: the membership comes from UCS itself, so
# the decision is still entirely UCS's. The one transformation is
# stripping a leading "user:" — publish identities are spelled
# "user:alice" while UCS stores bare member names ("alice").
_GROUPS_CACHE_TTL = 60.0
_groups_listing: Optional[tuple] = None


def _group_listing(force: bool = False) -> List[dict]:
    global _groups_listing
    now = time.monotonic()
    if not force and _groups_listing and (now - _groups_listing[0]) < _GROUPS_CACHE_TTL:
        return _groups_listing[1]
    groups = _admin_get("/api/v1/admin/groups").get("groups") or []
    _groups_listing = (now, groups)
    return groups


def principal_groups(principal: str) -> List[str]:
    """Every group `principal` is actually a member of, per UCS."""
    name = principal.split(":", 1)[1] if principal.startswith("user:") else principal
    return [g["group_name"] for g in _group_listing()
            if name in (g.get("member_names") or [])]


def _missing_container(error: Exception) -> bool:
    """Whether a failure means the catalog or schema this object needs is
    not there — as opposed to the object itself being rejected.

    Gravitino words it as part of the operation it refused:

      reason [Catalog prod_metalake.test-generator does not exist]
      reason [Schema prod_metalake.<catalog>.agent does not exist]
    """
    text = str(error).lower()
    return ("does not exist" in text
            and ("catalog" in text or "schema" in text))


def _admin_delete(path: str, timeout: float = 20.0) -> dict:
    """DELETE on the UCS admin API, with _admin_post's dev-fallback 401 refresh."""
    def attempt(force_refresh: bool):
        try:
            return httpx.delete(f"{UCS_BASE_URL}{path}",
                                headers=_admin_headers(force_refresh), timeout=timeout)
        except httpx.HTTPError as e:
            raise UCSUnavailable(f"UCS at {UCS_BASE_URL} unreachable: {e}")

    resp = attempt(False)
    if resp.status_code == 401 and _bearer_fallback() and UCS_ADMIN_USERNAME and UCS_ADMIN_PASSWORD:
        resp = attempt(True)
    if resp.status_code >= 400:
        raise UCSSyncError(f"DELETE {path} -> {resp.status_code}: {resp.text[:400]}")
    return resp.json() if resp.content else {}


def _already_exists(error: Exception) -> bool:
    """Whether a UCS admin refusal means the thing is ALREADY in the
    state we asked for.

    Gravitino answers 409 for a model, schema or role that exists. That
    is the desired end state, not a failure — but reporting it as one
    made a correctly registered agent read "not synced" in the UCS
    Access panel, and made Grant look broken for a group that already
    had the role. ensure_agent_catalog already treated its own "already
    exist" this way; the model and role calls did not.
    """
    text = str(error).lower()
    return "already exist" in text or "-> 409" in text


def ensure_agent_catalog(catalog: str = UCS_AGENT_CATALOG) -> None:
    """Register one agent's catalog and its schema. Idempotent — a
    400/409 because it already exists is treated as success, matching how
    every other create_* caller in this codebase leaves "already there"
    races to whoever calls second.

    Deliberately NOT memoised. It used to remember which catalogs it had
    created and return early for those, which is wrong the moment a
    catalog is removed in UCS: the process kept believing it existed, so
    every later publish skipped creating it and died at the next step —

      POST /api/v1/admin/models -> 404: ... reason
      [Catalog prod_metalake.test-generator does not exist]

    — and no amount of publishing or Retry sync could recover, because
    the only thing that would recreate the catalog was being skipped.
    That is the same trap a single process-wide `_catalog_registered`
    flag had before it; per-catalog memoisation just made it harder to
    notice. Two idempotent calls per publish is a cheap price for being
    able to heal.
    """
    try:
        _admin_post("/api/v1/admin/catalogs", {
            "name": catalog, "engine": "model",
            "comment": "InAiEra Agent Platform — an agent registered as a real Gravitino Model",
        })
    except UCSSyncError as e:
        if "already exist" not in str(e).lower() and "409" not in str(e):
            raise

    # The SCHEMA, not just the catalog. Registering a model needs both,
    # and nothing created this one — so every agent's sync failed at the
    # first REGISTER on a deployment where it did not happen to exist:
    #
    #   POST /api/v1/admin/models -> 404: model
    #   '<catalog>.agent.<id>': ... reason [Schema
    #   prod_metalake.<catalog>.agent does not exist]
    #
    # Publish then reported "not synced" with that 404 in the UCS Access
    # panel, and Retry sync could not fix it either, because it calls
    # this same function. The knowledge-base path already did this
    # correctly (ucs_provisioning.ensure_kb_catalog_object creates
    # app_datasets.docs before registering a fileset); the agent path
    # simply never had the equivalent step.
    #
    # Through the UCS admin API (POST /admin/schemas) — only UCS may
    # talk to Gravitino. Function-scoped import because ucs_provisioning
    # imports from THIS module, so a top-level one would be a real cycle.
    try:
        from .ucs_provisioning import _create_schema, _looks_like_already_exists
        try:
            _create_schema(catalog, UCS_AGENT_SCHEMA,
                           "Agents registered as Gravitino Models by the InaiEra Agent Platform.")
        except Exception as e:
            if not _looks_like_already_exists(e):
                # Not raised: the model call below reports the real
                # consequence with Gravitino's own wording, which says
                # more than anything this could invent. Logged so a
                # genuinely unreachable Gravitino is not silent.
                logger.warning("could not ensure schema %s.%s: %s",
                               catalog, UCS_AGENT_SCHEMA, e)
    except ImportError:  # pragma: no cover - provisioning module absent
        logger.warning("ucs_provisioning unavailable — cannot ensure the agent schema")


def sync_agent(agent_id: str, agent_name: str, description: str = "") -> dict:
    """Register one agent as a real Model in its OWN catalog. Call on
    agent creation — the Model needs to exist before any version or RBAC
    role can reference it, and before any real authz check on it can
    ever allow.

    The catalog is named after the agent (see agent_slug) and the model
    is still the agent id, so a grant stays anchored to the one thing a
    rename does not change.
    """
    catalog = agent_slug(agent_name)
    ensure_agent_catalog(catalog)

    def register():
        return _admin_post("/api/v1/admin/models", {
            "catalog": catalog, "schema_name": UCS_AGENT_SCHEMA, "name": agent_id,
            "comment": f"{agent_name} — {description}"[:500] if description else agent_name,
        })

    try:
        return register()
    except UCSSyncError as e:
        # The catalog or schema disappeared between ensuring it and using
        # it — someone removed it in UCS, or the ensure above was refused.
        # Rebuild and try once more, rather than reporting a 404 the next
        # publish would hit identically.
        if _missing_container(e):
            logger.info("catalog/schema missing for %s — rebuilding and retrying: %s",
                        agent_id, str(e)[:160])
            ensure_agent_catalog(catalog)
            return register()
        if not _already_exists(e):
            raise
        # Registered by an earlier attempt — including one that failed
        # LATER in the same call, which is exactly how an agent ends up
        # with a real Model in Gravitino and "not synced" on its card.
        return {"catalog": catalog, "schema": UCS_AGENT_SCHEMA, "name": agent_id,
                "already_registered": True}


CURRENT_ALIAS = "current"


def _point_current_alias(catalog: str, agent_id: str, version: str) -> str:
    """Move the `current` alias onto the version just registered.

    Gravitino allows an alias on exactly one version of a model, so
    `current` has to be taken off whichever version holds it before it
    can be added to the new one. UCS does the move (POST
    /admin/models/alias), so this platform never calls Gravitino.

    Best effort by design: the VERSION is the thing that must be
    registered, and it already is by the time this runs. A failure to
    move a convenience pointer is logged, not raised, because failing
    the publish over it would be worse than a stale alias.
    """
    try:
        res = _admin_post("/api/v1/admin/models/alias", {
            "catalog": catalog, "schema_name": UCS_AGENT_SCHEMA, "model": agent_id,
            "alias": CURRENT_ALIAS, "version": version})
        return {"moved": "moved", "already": "already current"}.get(res.get("status"), res.get("status", "moved"))
    except Exception as e:
        if "404" in str(e):
            return "version not found"
        logger.warning("could not move the '%s' alias for %s to %s: %s",
                       CURRENT_ALIAS, agent_id, version, e)
        return f"error: {type(e).__name__}"


def sync_agent_version(agent_id: str, agent_name: str, version: str, run_url: str,
                        comment: str = "") -> dict:
    """Register a real Model version for a real published AgentVersion.
    `run_url` is the real invocation endpoint (/api/agents/{id}/run) —
    the one field Gravitino actually requires, so it's the one field
    this refuses to fabricate a placeholder for.

    Only the version's OWN alias goes on the register call. It used to
    send `[version, "current"]`, and Gravitino allows an alias on one
    version at a time, so the moment anything held `current` every later
    publish was refused:

      409: version of model '...' already exists in Gravitino

    That message names the version, but the conflict is the alias —
    proved by the same call succeeding with a unique alias and failing
    with `current`. Believing the message, I first made this swallow the
    409 as "already in the desired state", which turned a loud failure
    into a silent one: publishes reported success and the catalog kept
    exactly one version, which is how it was found (a second publish
    never appeared under Explore versions).

    So: register with the unique alias, which cannot collide, then move
    `current` separately (see _point_current_alias). A genuine duplicate
    — re-registering a version number that is already there — is still
    treated as success, because that IS the desired end state.
    """
    catalog = agent_slug(agent_name)
    result = {}
    try:
        result = _admin_post("/api/v1/admin/models/versions", {
            "catalog": catalog, "schema_name": UCS_AGENT_SCHEMA, "model": agent_id,
            "uri": run_url, "aliases": [version],
            "comment": comment or f"Published {version}",
        })
    except UCSSyncError as e:
        if not _already_exists(e):
            raise
        result = {"catalog": catalog, "schema": UCS_AGENT_SCHEMA, "model": agent_id,
                  "version": version, "already_registered": True}
    result["current_alias"] = _point_current_alias(catalog, agent_id, version)
    return result


def list_agent_roles() -> list:
    """Every RBAC role this platform created for an agent, with its
    current scope and the groups bound to it."""
    roles = (_admin_get("/api/v1/admin/rbac/roles").get("roles") or [])
    out = []
    for r in roles:
        name = r.get("role") or r.get("role_name") or ""
        if not name.startswith("agent-"):
            continue
        out.append({"role": name, "scope": r.get("scope"),
                    "object_type": r.get("object_type"), "raw": r})
    groups = _group_listing(force=True)
    for entry in out:
        entry["groups"] = [g["group_name"] for g in groups
                           if entry["role"] in (g.get("roles") or [])]
    return out


def migrate_agent_to_own_catalog(agent_id: str, agent_name: str, version: str = "",
                                  run_url: str = "") -> dict:
    """Move one agent from the shared catalog into its own, carrying its
    grants across.

    Agents used to be registered as models inside a single
    `agent_catalog.agents`, keyed by id, which is what the Explore tree
    showed: rows of opaque ids under one catalog. Each agent now gets a
    catalog named after itself.

    The grants are the part that cannot be left behind. A role is scoped
    to a specific FQN, so a role still pointing at
    `agent_catalog.agents.<id>` grants nothing once the agent is governed
    at `<slug>.agent.<id>` — access would appear intact in UCS and deny
    in practice. Each existing role is therefore re-created against the
    new scope and every group bound to the old one is bound to the new,
    before anything is removed.

    Idempotent: re-running finds the model and roles already there and
    reports them as such.
    """
    result = {"agent_id": agent_id, "new_fqn": agent_fqn(agent_id, agent_name)}
    result["registered"] = sync_agent(agent_id, agent_name)
    if version and run_url:
        try:
            result["version"] = sync_agent_version(agent_id, agent_name, version, run_url,
                                                   f"Published {version}")
        except (UCSSyncError, UCSUnavailable) as e:
            result["version"] = f"error: {e}"

    old_scope = f"{UCS_AGENT_CATALOG}.{'agents'}.{agent_id}"
    moved = []
    for entry in list_agent_roles():
        if not entry["role"].startswith(f"agent-{agent_id}-"):
            continue
        # Only roles still on the OLD scope need moving. A role already
        # pointing at the new FQN is done, and one with no scope at all
        # (UCS reports None for some older rows) is left alone rather
        # than guessed at.
        if entry["scope"] not in (old_scope,):
            moved.append({"role": entry["role"], "action": "left alone",
                           "scope": entry["scope"]})
            continue
        tier = entry["role"].rsplit("-", 1)[-1]
        try:
            # Adds the new scope to the existing role (see
            # grant_agent_access), which is what actually moves the
            # grant — the group bindings already point at this role name
            # and do not need touching.
            grant_agent_access(agent_id, agent_name, entry["groups"][0], "custom", tier) \
                if entry["groups"] else _admin_post(
                    f"/api/v1/admin/rbac/roles/{entry['role']}/privileges",
                    {"catalog": agent_slug(agent_name), "schema_name": UCS_AGENT_SCHEMA,
                     "object_name": agent_id, "object_type": "model", "access_level": tier})
            for group in entry["groups"][1:]:
                grant_agent_access(agent_id, agent_name, group, "custom", tier)
            # Then take the stale scope off, so the role no longer claims
            # an object nothing is governed at any more.
            try:
                _admin_post(f"/api/v1/admin/rbac/roles/{entry['role']}/privileges/revoke",
                            {"catalog": UCS_AGENT_CATALOG, "schema_name": "agents",
                             "object_name": agent_id, "object_type": "model",
                             "access_level": tier})
                old_scope_action = "old scope revoked"
            except (UCSSyncError, UCSUnavailable) as e:
                old_scope_action = f"old scope left in place ({type(e).__name__})"
            moved.append({"role": entry["role"], "groups": entry["groups"],
                           "action": "re-scoped", "old_scope": old_scope_action})
        except (UCSSyncError, UCSUnavailable) as e:
            moved.append({"role": entry["role"], "action": f"error: {str(e)[:140]}"})
    result["grants"] = moved
    return result


# The Gravitino MODEL privileges each UCS level grants — what UCS shows
# for an agent as USE_AGENT / CREATE_AGENT_VERSION / CREATE_AGENT. These,
# and nothing else, are what an agent grant sends: UCS's Tier column is
# derived from the privileges, so an agent:* scope in the list made it
# report a role name (agent_user) instead of select/writer/owner.
_MODEL_PRIVILEGES = {
    "select": ["USE_MODEL"],
    "writer": ["USE_MODEL", "CREATE_MODEL_VERSION"],
    "owner": ["USE_MODEL", "CREATE_MODEL_VERSION", "CREATE_MODEL"],
}


def agent_grant_role_name(agent_id: str, tier: str, group_name: str) -> str:
    """One UCS role per (agent, level, group).

    Per group because UCS records a binding's privileges from the create-
    role request it saw last, in memory: binding a second group to a role
    that already existed — or any group after UCS restarted — recorded
    whatever that stale request held, or nothing. A role of its own lets
    every grant create its role fresh.
    """
    return f"agent-{agent_id}-{tier}-{re.sub(r'[^A-Za-z0-9_-]', '_', group_name)}"


def _agent_grant_scope(agent_id: str, agent_name: str, tier: str) -> dict:
    if tier == "owner":
        # CREATE_MODEL (CREATE_AGENT) is not grantable on a model, only on
        # the schema holding it. Each agent has its own catalog, so its
        # `agent` schema holds only this agent — the caller refuses an
        # owner grant when another agent shares the catalog.
        return {"catalog": agent_slug(agent_name), "schema_name": UCS_AGENT_SCHEMA,
                "object_name": "*", "object_type": "schema", "access_level": "owner",
                "privileges": _MODEL_PRIVILEGES["owner"]}
    return {"catalog": agent_slug(agent_name), "schema_name": UCS_AGENT_SCHEMA,
            "object_name": agent_id, "object_type": "model", "access_level": tier,
            "privileges": _MODEL_PRIVILEGES.get(tier, _MODEL_PRIVILEGES["select"])}


def grant_agent_access(agent_id: str, agent_name: str, group_name: str,
                       group_source: str = "custom", tier: str = "select",
                       role: str = "") -> dict:
    """Grant a group one UCS level on this agent: create the role, bind
    the group. `role` is accepted for older callers and ignored — the
    level alone decides what is granted."""
    role_name = agent_grant_role_name(agent_id, tier, group_name)
    scope = _agent_grant_scope(agent_id, agent_name, tier)
    try:
        _admin_post("/api/v1/admin/rbac/roles", {"role_name": role_name, **scope})
    except UCSSyncError as e:
        if not _already_exists(e):
            raise
        # Re-granting the same level to the same group: drop and recreate,
        # so the binding below records this request's privileges.
        _admin_delete(f"/api/v1/admin/rbac/roles/{role_name}")
        _admin_post("/api/v1/admin/rbac/roles", {"role_name": role_name, **scope})
    return _admin_post("/api/v1/admin/rbac/groups/bind", {
        "group_name": group_name, "group_source": group_source, "role_name": role_name,
    })


def revoke_agent_access(agent_id: str, group_name: str, tier: str = "select",
                        group_source: str = "custom") -> dict:
    """Remove this group's grant at this level. The role is the group's
    own (see agent_grant_role_name), so deleting it removes exactly this
    grant and nothing anyone else holds."""
    role_name = agent_grant_role_name(agent_id, tier, group_name)
    try:
        _admin_delete(f"/api/v1/admin/rbac/roles/{role_name}")
    except UCSSyncError as e:
        if "404" not in str(e):
            raise
        # A grant made before per-group roles: unbind the shared role.
        return _admin_post("/api/v1/admin/rbac/groups/revoke", {
            "group_name": group_name, "group_source": group_source,
            "role_name": f"agent-{agent_id}-{tier}",
        })
    return {"status": "revoked", "group": group_name, "role": role_name}

