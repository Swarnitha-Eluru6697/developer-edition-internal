"""FastAPI dependencies."""
from __future__ import annotations

import time
import uuid
from typing import Annotated, AsyncIterator

import structlog
from fastapi import Depends, HTTPException, Request, status
from gateway_auth import GatewayAuthError, principal_from_headers
from gateway_auth import Principal as GatewayPrincipal
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.errors import forbidden
from app.core.observability import bind_identity_contextvars
from app.db.session import AsyncSessionLocal

log = structlog.get_logger()


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield an async DB session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def gateway_principal(request: Request) -> Principal:
    """Gate 1: who the caller is, as Envoy vouches for it — WITHOUT any app permission check (GET /me, logout auditing).

    Identity arrives as Envoy-injected, trusted headers. Envoy verified the caller's Keycloak ticket and stripped any
    client-supplied x-user-* headers before setting its own. The gateway-auth library (v1.1) proves THIS request came through
    Envoy (TRUST_ENVOY_HEADERS + constant-time x-inaiera-gateway-secret) and reads the identity and the caller's PERMISSIONS
    (x-user-permissions). `protect_app` (app/main.py) has normally done that already for the request; this reuses its result.
    Authorisation is by permission only (app/core/rbac.py); x-user-roles is kept on the principal for display/audit.
    """
    gp = getattr(request.state, "principal", None)
    if not isinstance(gp, GatewayPrincipal):
        try:
            gp = principal_from_headers(request.headers)
        except GatewayAuthError as e:
            raise HTTPException(status_code=e.status_code, detail={"code": "UNAUTHENTICATED", "message": e.detail})
        request.state.principal = gp
    cached = getattr(request.state, "workspace_principal", None)
    if isinstance(cached, Principal):
        return cached

    # Org isolation (WS-SEC-001): a data plane serves exactly one org, and
    # rows here (users, sessions, executions) carry no org_id of their own —
    # users are keyed by username alone. So the tenant Envoy asserts must BE
    # this data plane's org; otherwise a same-named user from another tenant
    # would resolve to this org's user row and everything it owns.
    settings = get_settings()
    if gp.tenant_id != settings.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "TENANT_MISMATCH", "message": "this data plane does not serve the caller's org"},
        )

    # W-020 / X-003: the user's identity is the control plane's immutable subject (the token's `sub`, injected by the gateway
    # as x-user-id — it is an x-user-* header, so the gateway strips any client-supplied one). The username is an attribute:
    # it can be renamed, and the runners and DQ only ever see the subject. Required outside dev (contract section 4).
    subject = (gp.user_id or "").strip()
    if subject:
        try:
            subject = str(uuid.UUID(subject))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_SUBJECT", "message": "x-user-id must be the control plane's user UUID"},
            )
    elif settings.user_subject_required:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_SUBJECT",
                    "message": "the gateway did not inject x-user-id (the token's `sub`); Workspace identifies users by it"},
        )
    else:
        subject = gp.username          # dev only: an older local gateway that does not send the subject yet
        log.warning("user_subject_missing_using_username", username=gp.username)

    # issued_at/expires_at are vestigial from the JWT era — nothing in this
    # codebase reads them; kept only so the Principal shape doesn't change.
    now = int(time.time())
    principal = Principal(
        user_id=subject, org_id=gp.tenant_id, roles=list(gp.roles),
        # `email` is, for gateway callers, the gateway username: the name the catalog's policy engine (UCS) knows the person by.
        email=gp.username, issued_at=now, expires_at=now + 3600, username=gp.username,
        permissions=frozenset(gp.permissions), groups=list(gp.groups),
    )
    # WS-AUDIT-016: identity is only known once the gateway secret/headers
    # validate — bind it here so every log emitted for the rest of this
    # request carries org_id/user_id automatically.
    bind_identity_contextvars(org_id=principal.org_id, user_id=principal.user_id)
    request.state.workspace_principal = principal
    return principal


ACCESS_PERMISSION = "workspace:access:view"   # published in permissions.json (checked by app/core/rbac.py)


async def get_current_user(request: Request) -> Principal:
    """The caller of every Workspace route: a gateway-verified principal that may open Workspace at all
    (`workspace:access:view`). The route's own permission is checked on top (app/core/rbac.py)."""
    principal = gateway_principal(request)
    if not principal.has(ACCESS_PERMISSION):
        raise forbidden(f"missing permission {ACCESS_PERMISSION}")
    return principal


async def get_gateway_user(request: Request) -> Principal:
    """Dependency form of gateway_principal (async, so the identity log context binds in the request's own context)."""
    return gateway_principal(request)


GatewayUser = Annotated[Principal, Depends(get_gateway_user)]
CurrentUser = Annotated[Principal, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
