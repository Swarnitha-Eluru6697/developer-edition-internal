"""
Gate 1 for DataConnect — the north-south trust boundary, via the shared `inaiera-gateway-auth` library (v1.1,
contract docs/auth/01-CONTRACT.md).

Envoy has already validated the user's ticket, stripped every identity header the client sent, set
x-user-name / x-user-id / x-tenant-id / x-user-groups / x-user-permissions from the ticket's claims, and stamped
x-inaiera-gateway-secret. `install_gateway(app)` puts the library's `protect_app` in front of EVERY route:

  * no TRUST_ENVOY_HEADERS=true, or a missing/wrong gateway secret   -> 401
  * no identity (x-user-name)                                        -> 401
  * otherwise the verified Principal is left on request.state.principal

Authorisation is then per route, by permission (app/core/permissions.py's `require(...)`, a 403 without it).
Role names are never consulted; x-user-roles is kept on the principal for display/audit only.

Public (no principal needed) is the short list in PUBLIC_PATHS below and nothing else: the health probes, the signed
Slack / Teams alert-action links (/alerts/actions/*, the token is the credential), plus /metrics only when an operator
explicitly sets DATACONNECT_METRICS_PUBLIC=true for an in-cluster scraper.

Local development
-----------------
DataConnect has no Envoy/Keycloak of its own locally. Instead of trusting a client header (the old X-User-Role
bypass, removed), a developer can opt in to a FIXED development principal:

    DATACONNECT_ENV=development              (set explicitly — see assert_explicit_environment)
    TRUST_ENVOY_HEADERS unset / not "true"
    DATACONNECT_DEV_PERMISSIONS=all          (or a comma list of dataconnect:* IDs)

Every request is then served as settings.dev_user / settings.dev_tenant holding exactly those permissions. Nothing
the client sends can change who it is or what it may do — no header is read. Startup refuses
DATACONNECT_DEV_PERMISSIONS in any other environment, so it can never be enabled outside development.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from gateway_auth import GatewayIdentity, Principal, get_principal, protect_app

from app.core.config import settings
from app.core.permissions import all_permissions

__all__ = ["PUBLIC_PATHS", "assert_explicit_environment", "dev_mode_enabled", "dev_principal", "get_principal", "install_gateway"]


def public_paths() -> list[str]:
    """The ONLY routes reachable without a gateway principal (contract 5.2)."""
    prefix = settings.api_prefix.rstrip("/")
    paths = [f"{prefix}/health", f"{prefix}/health/*"]
    if settings.metrics_public:
        paths.append("/metrics")
    # The Acknowledge / Resolve buttons in a Slack / Teams alert: the signed, expiring token in the link is the credential
    # (app/api/routes/alert_actions.py). Without DATACONNECT_ALERTS_ACTION_SECRET that route rejects every token.
    paths.append(f"{prefix}/alerts/actions/*")
    return paths


PUBLIC_PATHS = public_paths()


def _envoy_trusted() -> bool:
    return os.environ.get("TRUST_ENVOY_HEADERS") == "true"


def dev_mode_enabled() -> bool:
    """The fixed development principal is used only in development, never behind Envoy, and only when asked for."""
    return settings.env == "development" and not _envoy_trusted() and bool(settings.dev_permissions)


def dev_permissions() -> frozenset[str]:
    requested = {p.strip() for p in settings.dev_permissions.split(",") if p.strip()}
    if "all" in requested:
        return all_permissions()
    return frozenset(requested & all_permissions())


def dev_principal() -> Principal:
    return Principal(GatewayIdentity(
        username=settings.dev_user, user_id=settings.dev_user, tenant_id=settings.dev_tenant,
        permissions=dev_permissions(),
    ))


def assert_explicit_environment() -> None:
    """Fail startup loudly instead of running in a mode nobody chose.

    BUG-04: settings.env DEFAULTS to "development". The development principal must never be reached because a
    deployment forgot DATACONNECT_ENV, and DATACONNECT_DEV_PERMISSIONS must never take effect outside development.
    Called from the app lifespan."""
    if settings.dev_permissions and settings.env != "development":
        raise RuntimeError(
            f"Refusing to start: DATACONNECT_DEV_PERMISSIONS is set but DATACONNECT_ENV is {settings.env!r}. "
            "The development principal exists only for local development; unset it."
        )
    if dev_mode_enabled() and "env" not in settings.model_fields_set:
        raise RuntimeError(
            "Refusing to start with the development principal: DATACONNECT_ENV is not set, so it defaulted to "
            "'development'. Set DATACONNECT_ENV=development explicitly for local work, or 'staging'/'production' "
            "together with TRUST_ENVOY_HEADERS=true behind Envoy."
        )


def install_gateway(app: FastAPI) -> None:
    """Require a principal on every route except PUBLIC_PATHS. Call before adding CORS so CORS stays outermost."""
    public = public_paths()
    if dev_mode_enabled():
        @app.middleware("http")
        async def _dev_principal(request: Request, call_next):
            # Never reads a request header: the identity and permissions are fixed by the process's own settings.
            request.state.principal = dev_principal()
            return await call_next(request)
        return
    protect_app(app, public_paths=public, default_tenant_id=settings.default_tenant)
