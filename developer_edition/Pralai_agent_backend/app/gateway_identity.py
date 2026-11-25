"""This service's side of the shared north-south trust boundary
(InaiEra_dataplane_GatewayAuth v1.1, contract docs/auth/01-CONTRACT.md).

The library decides *who a request is* and *which permissions it holds*:

    x-user-name                from preferred_username
    x-user-id                  from sub
    x-tenant-id                from tenant
    x-user-groups              from groups (base64 JSON)
    x-user-permissions         from permissions (base64 JSON)
    x-inaiera-gateway-secret   injected by Envoy with append:false

Both checks fail CLOSED: with TRUST_ENVOY_HEADERS unset, or the shared secret
absent or mismatched, there is no principal. app/main.py applies
`protect_app`, so every non-public route answers 401 without one; routes then
declare the permission they need (app/permissions.py). No role name is read or
interpreted here: x-user-roles is display/audit only.
"""
from __future__ import annotations

import os
from typing import Optional

from gateway_auth import GatewayAuthError, Principal, make_principal_dependency, principal_from_headers

# The tenant assumed when a request carries no x-tenant-id. Matches UCS_TENANT,
# which is what ucs_client already sends as X-Tenant-Id on every entitlement
# call — one tenant identifier across both gates rather than two that disagree.
DEFAULT_TENANT_ID = os.getenv("UCS_TENANT", "prod_metalake")

# `def route(principal = Depends(get_principal))` — the gateway principal
# (401 if none). Reuses the one protect_app already put on request.state.
get_principal = make_principal_dependency(default_tenant_id=DEFAULT_TENANT_ID)


def gateway_trust_enabled() -> bool:
    """Whether this process believes identity headers at all — the same exact,
    case-sensitive `== "true"` test the library applies."""
    return os.environ.get("TRUST_ENVOY_HEADERS") == "true"


def principal_from_request(request) -> Optional[Principal]:
    """The gateway principal for this request, or None. Never raises."""
    p = getattr(getattr(request, "state", None), "principal", None)
    if isinstance(p, Principal):
        return p
    try:
        p = principal_from_headers(request.headers, DEFAULT_TENANT_ID)
    except GatewayAuthError:
        return None
    try:
        request.state.principal = p
    except Exception:
        pass
    return p


def identity_from_gateway_headers(request) -> Optional[str]:
    """The verified username from the gateway contract, or None.

    This platform's identity plumbing (auth.get_verified_identity) threads a
    plain username string through run/upload/publish paths; this is the narrow
    adapter returning what those callers expect.
    """
    p = principal_from_request(request)
    return p.username if p else None


def groups_from_gateway_headers(request) -> Optional[list]:
    """The groups Envoy asserted for this request (parsed by the library), or
    None when there is no gateway principal or it carries no groups."""
    p = principal_from_request(request)
    return list(p.groups) if p and p.groups else None
