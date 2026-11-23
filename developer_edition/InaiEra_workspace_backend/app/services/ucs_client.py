"""Unified Catalog Service (UCS) authorization client.

UCS is the platform's governance control plane — a separate service
(InAiEra-unified-catalog-backend) — and its real, existing endpoint for this
is `POST /api/v1/authz/check` (see `app/routers/authz.py` in that repo):

    request  {"action": str, "object": str, "principal"?, "groups"?}
    response {"decision": "allow" | "deny", "reason": str}

Two things about that contract shape this client:

- UCS has no "database connection" object type or a "connection.use" /
  "dataset.query" action of its own — its real action vocabulary
  (`app/core/privileges.py` in the UCS repo) is single words: "read",
  "write", "query", "create_dataset", "publish_dataset". We ask for "query"
  (the closest existing match) against the connection's own `name` as the
  object. Whether that resolves to allow or deny depends entirely on
  whatever grant an org's governance team has configured for that name in
  UCS — this client makes no assumption about that.

- UCS authenticates every caller through the shared gateway-auth contract
  (InaiEra_dataplane_GatewayAuth): X-User-* identity headers plus UCS's
  gateway secret as X-Inaiera-Gateway-Secret (UCS_GATEWAY_SECRET here).
"""
from __future__ import annotations

import base64
import json

import httpx
import structlog

from app.core.auth import Principal
from app.core.config import get_settings

log = structlog.get_logger()

_TIMEOUT = 10.0


class UcsAuthorizationError(Exception):
    """Raised whenever the action is not authorized to proceed — UCS denied
    it, UCS is unreachable, or UCS isn't configured for this deployment.
    There is deliberately no fail-open branch: if this can't be answered
    with an explicit "allow", the action does not proceed."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class UcsUnavailableError(Exception):
    """Raised when UCS can't answer a catalog-discovery request — unreachable,
    not configured, or errored. Callers (CatalogAdapter) must not treat this
    as "fall back to fabricated data" in production — see WS-CAT-008."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _ucs_configured() -> bool:
    s = get_settings()
    return bool(s.ucs_service_url and s.ucs_gateway_secret)


def _encode_list(values) -> str:
    """Contract section 4: a list header is base64 of a JSON array (what Envoy sends; every gateway-auth library reads it)."""
    return base64.b64encode(json.dumps(list(values)).encode()).decode()


def _identity_headers(principal: Principal) -> dict[str, str]:
    """The caller as UCS's gateway-auth expects it: name, immutable id, tenant, the caller's real Keycloak groups (UCS's
    data-level grants are keyed on them) and permissions (UCS authorises its own routes on them). Never role names."""
    identity = principal.email or principal.user_id
    headers = {"X-User-Name": identity, "X-User-Id": str(principal.user_id), "X-Tenant-Id": str(principal.org_id)}
    if principal.groups:
        headers["X-User-Groups"] = _encode_list(principal.groups)
    if principal.permissions:
        headers["X-User-Permissions"] = _encode_list(sorted(principal.permissions))
    # Proves to UCS's gateway-auth check that this call is trusted, the same
    # job Envoy's injected secret does for browser traffic.
    secret = get_settings().ucs_gateway_secret
    if secret:
        headers["X-Inaiera-Gateway-Secret"] = secret
    return headers


def assert_governance_for_environment() -> None:
    """Startup guard (W-010): outside dev, the production-governed profile must have UCS configured — a data plane must not run
    partially governed. The development-ungoverned profile is allowed but logged loudly as unsupported for production."""
    s = get_settings()
    if s.environment == "dev":
        return
    if s.governance_profile == "development-ungoverned":
        log.critical("governance.ungoverned_profile_unsupported_for_production", env=s.environment)
        return
    missing = [name for name, value in (("UCS_SERVICE_URL", s.ucs_service_url), ("UCS_GATEWAY_SECRET", s.ucs_gateway_secret)) if not value]
    if missing:
        raise RuntimeError(
            f"governance_profile=production-governed requires UCS, but {', '.join(missing)} is not set. Configure UCS, or set "
            "GOVERNANCE_PROFILE=development-ungoverned (unsupported for production)")


async def authorize(principal: Principal, *, action: str, object_name: str) -> None:
    """Ask UCS whether `principal` may perform `action` on `object_name`.

    Raises UcsAuthorizationError instead of returning a boolean so a caller
    can't accidentally ignore a denial the way an unchecked bool return
    invites.
    """
    settings = get_settings()
    if not _ucs_configured():
        raise UcsAuthorizationError("UCS is not configured for this deployment")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.ucs_service_url}/api/v1/authz/check",
                json={"action": action, "object": object_name},
                headers=_identity_headers(principal),
            )
            resp.raise_for_status()
            decision = resp.json()
    except httpx.HTTPError as e:
        log.error("ucs_authz_unreachable", action=action, object=object_name, error=str(e))
        raise UcsAuthorizationError("Could not reach UCS to authorize this action") from e

    if decision.get("decision") != "allow":
        log.info("ucs_authz_denied", action=action, object=object_name, reason=decision.get("reason"))
        raise UcsAuthorizationError(decision.get("reason") or "UCS denied this action")


def connection_resource_fqn(conn) -> str:
    """The canonical resource UCS is asked about for `conn` (W-011): the identity persisted on the row, which is the UCS catalog it
    was linked to or `<catalog>.connection.<id>`. NEVER derived from the connection's name or its editable catalog field, so renaming a
    connection, or two connections sharing a name over time, cannot move its policy identity. A row without one (not yet backfilled)
    gets the id form, which is stable too."""
    if getattr(conn, "ucs_resource_fqn", None):
        return conn.ucs_resource_fqn
    return f"{get_settings().data_policy_catalog}.connection.{conn.id}"


async def authorize_connection_use(principal: Principal, user_row_id, conn) -> None:
    """Gate using a registered DatabaseConnection (running SQL, live probe).

    UCS decides: "query" (UCS's action vocabulary — see its core/privileges.py; what a SELECT_TABLE-style grant permits) on the
    connection's canonical resource (`connection_resource_fqn`), so catalog grants govern who can query that database through any
    connection to it.

    Owner bypass is an explicit, documented policy (Settings.connection_owner_bypass / `owner_bypass_allowed`), not an accident of the
    code path: when it is allowed the creator may use their own connection in their own org without asking UCS (so a deployment with
    no UCS can still query what its users created) and the bypass is logged; when it is not (the production-governed profile) the
    creator goes through the same UCS decision as everybody else (WS-DATA-007: existence of the row is not permission to use it).
    """
    s = get_settings()
    if s.owner_bypass_allowed and conn.created_by_user_id is not None and conn.created_by_user_id == user_row_id \
            and conn.org_id == principal.org_id:
        log.info("connection_use_owner_bypass", connection_id=str(conn.id), user_id=principal.user_id)
        return
    await authorize(principal, action="query", object_name=connection_resource_fqn(conn))


async def get_explorer_tree(principal: Principal) -> dict:
    """UCS's live, canonical catalog->schema->object tree
    (GET /api/v1/user/explorer-tree — see app/routers/user.py:21-23 and
    app/services/discovery_service.py in the UCS repo).

    Shape: {catalog_name: {"engine": str, "schemas": {schema_name: [{"name",
    "type"}, ...]}}}. Not filterable server-side — callers index into the
    returned dict by their own connection's `catalog_name`. A catalog_name
    with no matching key means UCS has no registered catalog by that name;
    that's indistinguishable from a transient Gravitino error on this
    endpoint's own terms, so callers must treat both as "nothing available"
    rather than guessing which one it was.
    """
    settings = get_settings()
    if not _ucs_configured():
        raise UcsUnavailableError("UCS is not configured for this deployment")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.ucs_service_url}/api/v1/user/explorer-tree",
                headers=_identity_headers(principal),
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        log.error("ucs_discovery_unreachable", error=str(e))
        raise UcsUnavailableError("Could not reach UCS for catalog discovery") from e

    return data.get("tree", {})


async def get_object_columns(principal: Principal, fqn: str) -> list[dict]:
    """Column list for one `catalog.schema.table` object
    (GET /api/v1/user/objects/{fqn}/profile — app/routers/user.py:50-55).

    Columns live at response["schema"]["columns"]; that section reports
    {"status": "unavailable: ..."} instead of a column list on a Gravitino
    error (app/services/asset_profile_service.py:30,44-45) — treated the
    same as UCS being unreachable, not as "this table has zero columns".
    """
    settings = get_settings()
    if not _ucs_configured():
        raise UcsUnavailableError("UCS is not configured for this deployment")

    from urllib.parse import quote
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.ucs_service_url}/api/v1/user/objects/{quote(fqn, safe='.')}/profile",
                headers=_identity_headers(principal),
            )
            resp.raise_for_status()
            profile = resp.json()
    except httpx.HTTPError as e:
        log.error("ucs_profile_unreachable", fqn=fqn, error=str(e))
        raise UcsUnavailableError("Could not reach UCS for object profile") from e

    schema_section = profile.get("schema")
    if not isinstance(schema_section, dict) or "columns" not in schema_section:
        raise UcsUnavailableError(
            f"UCS has no column profile for {fqn}"
            + (f" ({schema_section.get('status')})" if isinstance(schema_section, dict) else "")
        )
    return schema_section["columns"]
