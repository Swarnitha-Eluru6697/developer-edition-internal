"""Auth bootstrap endpoints.

The Workspace service does NOT issue JWTs — the control plane does. This
module exposes:

  GET  /api/v1/me              — the contract's identity + permissions body ({user, userId, tenant, permissions})
  GET  /api/v1/auth/me         — current principal, local user mirror and permissions
  POST /api/v1/auth/exchange   — exchange the control plane JWT for a workspace
                                 session cookie scoped to this subdomain (the
                                 browser SPA calls this once on load so future
                                 requests can be cookie-authenticated rather
                                 than carrying the Authorization header on every
                                 fetch)
  POST /api/v1/auth/logout     — clear the cookie
  POST /api/v1/auth/ws-ticket  — mint a single-use, short-lived WebSocket
                                 connect ticket (WS-AUTH-010)

The control plane remains the source of truth for identity. We never refresh
JWTs here; the SPA refreshes against the control plane and re-exchanges.
/exchange verifies the bearer it stores and ties it to the gateway identity.
"""
from __future__ import annotations

import json
import secrets as _secrets
import time
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.auth import SESSION_COOKIE_NAME, validate_jwt
from app.core.authz import get_owned_session
from app.core.config import get_settings
from app.core.deps import DB, CurrentUser, GatewayUser, gateway_principal
from app.core.rbac import effective_permissions
from app.core.workspace_scope import WORKSPACE_MODEL
from app.schemas.common import OkResponse
from app.services.audit_service import AuditService
from app.services.execution_router import get_redis
from app.services.workspace_service import WorkspaceService

log = structlog.get_logger()

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
# GET /api/v1/me (contract section 5.4) lives beside /auth: same identity, the shape every Data Plane app returns.
me_router = APIRouter(prefix="/api/v1", tags=["auth"])

WS_TICKET_TTL_SECONDS = 30


class MeUser(BaseModel):
    """The local user mirror (app/models User row)."""
    id: str
    external_id: str
    email: str | None
    display_name: str | None


class MeClaims(BaseModel):
    """Identity from the gateway headers."""
    user_id: str
    org_id: str
    email: str | None
    # An attribute only (display / catalog policy name); `user_id` is the immutable control-plane subject (W-020, X-003).
    username: str | None = None


class MeOut(BaseModel):
    user: MeUser
    claims: MeClaims
    # What the caller may do in Workspace: the `workspace:*` permissions from their ticket (permissions.json IDs). The UI
    # gates on exactly this list; the API checks the same IDs.
    permissions: list[str]
    # W-021: "personal" — a workspace has exactly one owner; there is no sharing, membership or ACL in this release. Clients
    # must not present collaboration features, and `workspace_id` downstream is a label, not an access list.
    workspace_model: str


class ContractMeOut(BaseModel):
    """Contract section 5.4: `GET /me` -> `{ user, userId, tenant, permissions }`."""
    user: str
    userId: str
    tenant: str
    permissions: list[str]


@me_router.get("/me", response_model=ContractMeOut)
async def contract_me(user: GatewayUser) -> dict:
    """Who the caller is and every permission they hold. Any gateway-verified caller may ask (no app permission needed),
    so the UI can tell "no access" apart from "not signed in"."""
    return user.me()


@router.get("/me", response_model=MeOut)
async def me(user: GatewayUser, db: DB) -> dict:
    """Return the current principal, the local user mirror and the caller's Workspace permissions.

    Called by the SPA on load. Needs only a gateway-verified caller (not `workspace:access:view`), so a user without access
    gets an answer that says so (an empty or access-less permission list) instead of a 403.
    """
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    return {
        "user": {
            "id": str(user_row.id),
            "external_id": user_row.external_id,
            "email": user_row.email,
            "display_name": user_row.display_name,
        },
        "claims": {
            "user_id": user.user_id,
            "org_id": user.org_id,
            "email": user.email,
            "username": user.username,
        },
        "permissions": effective_permissions(user),
        "workspace_model": WORKSPACE_MODEL,
    }


@router.post("/exchange", status_code=status.HTTP_204_NO_CONTENT)
async def exchange(request: Request, response: Response, user: CurrentUser, db: DB) -> Response:
    """Exchange the control plane JWT (in Authorization header) for a session
    cookie scoped to this Workspace subdomain.

    The cookie carries the same JWT, just delivered as a Secure HttpOnly cookie
    so the browser sends it automatically on same-origin XHR/fetch and on
    WebSocket upgrades (where Authorization headers can't be set by JS).

    CurrentUser only proves who the *gateway* says the caller is; it does not
    vouch for this Authorization header. Because the token is persisted and
    the session's credential (no service receives it any more: X-001 delegated tokens), it is verified here
    against the control-plane JWKS (signature, issuer, audience, expiry, org)
    and must belong to the same subject and org as the trusted gateway identity.
    A token that fails either check is never stored (W-003).

    The cookie expires when the JWT expires; the SPA must call /exchange
    again with a freshly refreshed JWT.
    """
    settings = get_settings()
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        return JSONResponse(
            {"code": "MISSING_TOKEN", "message": "Authorization: Bearer <jwt> required"},
            status_code=401,
        )

    # Raises 401 INVALID_TOKEN / UNKNOWN_KID / MISSING_KID, or 403 ORG_MISMATCH.
    verified = await validate_jwt(token)
    # The gateway's identity is the token's immutable subject (x-user-id = `sub`, W-020/X-003); its tenant is the token's org.
    # The username is only cross-checked when both sides have one (Envoy derives it from this same token). A gateway that sent
    # no subject (dev only; user_id is then the username) is compared the way it always was: username, else sub.
    subject_mode = bool(user.username) and user.user_id != user.username
    if subject_mode:
        token_user = verified.user_id if (not verified.username or verified.username == user.username) else f"{verified.user_id}/{verified.username}"
    else:
        token_user = verified.username or verified.user_id
    if token_user != user.user_id or verified.org_id != user.org_id:
        log.warning("exchange_token_identity_mismatch",
                    gateway_user=user.user_id, token_user=token_user,
                    gateway_org=user.org_id, token_org=verified.org_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_IDENTITY_MISMATCH",
                    "message": "The bearer token does not belong to the authenticated user"},
        )

    audit = AuditService(db)
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    await audit.record(
        user_id=user_row.id,
        action="auth.session_exchange",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # Cookie lifetime follows the verified token's own exp.
    max_age = max(60, verified.expires_at - int(time.time()))

    is_prod = settings.environment in ("staging", "prod")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=is_prod,
        samesite="strict",
        path="/",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/logout", response_model=OkResponse)
async def logout(response: Response, request: Request) -> OkResponse:
    """Clear the session cookie — always (W-004).

    Logging out must work with an expired, malformed or missing credential: that is exactly when a stale cookie needs clearing, and
    an endpoint that answers 401 before deleting it leaves the cookie in the browser. So this route requires no authentication; the
    identity and the audit event are best-effort extras (an unverifiable caller simply is not audited), and the cookie is deleted
    with the attributes it was set with whatever happens. Idempotent: logging out twice, or without a session, is a 200.
    """
    try:
        await _audit_logout(request)
    except Exception as e:                      # noqa: BLE001 — identity/audit must never keep a cookie alive
        log.info("logout_audit_skipped", reason=type(e).__name__)
    settings = get_settings()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=settings.environment in ("staging", "prod"),
                           httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return OkResponse(ok=True)


async def _audit_logout(request: Request) -> None:
    """Record auth.logout for the caller IF the gateway vouches for one. Own session: it cannot affect the response."""
    from app.db.session import AsyncSessionLocal

    h = request.headers
    try:
        principal = gateway_principal(request)
    except HTTPException:
        return                                  # no verifiable identity: nothing to attribute
    async with AsyncSessionLocal() as session:
        user_row = await WorkspaceService(session).ensure_user(principal)
        await AuditService(session).record(
            user_id=user_row.id, action="auth.logout",
            ip_address=request.client.host if request.client else None, user_agent=h.get("user-agent"))
        await session.commit()


class WsTicketRequest(BaseModel):
    session_id: UUID


class WsTicketOut(BaseModel):
    ticket: str
    expires_in: int


@router.post("/ws-ticket", response_model=WsTicketOut)
async def mint_ws_ticket(payload: WsTicketRequest, user: CurrentUser, db: DB) -> WsTicketOut:
    """Mint a single-use, short-lived ticket for the WebSocket handshake.

    Browsers can't attach the session cookie's Authorization-header
    equivalent on a WS upgrade in every deployment topology, so the caller
    still has to identify itself via the query string — but a raw bearer JWT
    there (the previous design) is a long-lived credential that infra/proxy
    logs can capture. This ticket is worthless beyond one connection
    attempt: bound to exactly this org+user+session, deleted from Redis the
    instant the gateway reads it (single-use), and expired after
    WS_TICKET_TTL_SECONDS regardless (WS-AUTH-010).

    Ownership of `session_id` is verified here, at mint time — the gateway
    trusts a presented ticket's binding rather than re-querying the DB, since
    the ticket itself is the delegated proof of that already-made decision.
    """
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    session = await get_owned_session(db, user_row.id, payload.session_id, for_use=True)

    ticket = _secrets.token_urlsafe(32)
    r = get_redis()
    await r.set(
        f"ws-ticket:{ticket}",
        json.dumps({
            "org_id": user.org_id,
            # The *internal* User.id — what get_owned_session/get_owned_execution
            # actually compare against — not the external JWT subject.
            "user_id": str(user_row.id),
            "external_user_id": user.user_id,
            "session_id": str(session.id),
        }),
        ex=WS_TICKET_TTL_SECONDS,
    )
    return WsTicketOut(ticket=ticket, expires_in=WS_TICKET_TTL_SECONDS)
