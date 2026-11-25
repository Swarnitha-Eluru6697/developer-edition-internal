"""
Verified caller identity, for the run/upload/publish paths that record or
enforce who is asking (features.require_authenticated_user, upload owners,
template authors, saved-result owners).

The only source of identity is the shared gateway contract
(InaiEra_dataplane_GatewayAuth — see app/gateway_identity.py): x-user-name
proven by x-inaiera-gateway-secret, both checks failing closed. app/main.py's
protect_app already refuses every non-public route without that principal, so
in a running service every routed request has one.

The former legacy path (OIDC_VERIFIED_USER_HEADER / X-Auth-Request-Email,
OIDC_GATEWAY_SHARED_SECRET, OIDC_DEV_BYPASS_*) is removed: identity is never
accepted from any other header (contract section 5.5).
"""
import logging
import os
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def get_verified_identity(request: Request) -> Optional[str]:
    """The gateway-verified username, or None. Never raises — callers that
    need to enforce presence use require_verified_identity below."""
    from .gateway_identity import identity_from_gateway_headers
    return identity_from_gateway_headers(request)


def _identity_refusal_detail(request: Request) -> str:
    """Why this request has no verified identity, naming the failing check
    (never a secret's value — only whether a check passed)."""
    from .gateway_identity import gateway_trust_enabled
    if not gateway_trust_enabled():
        reason = ("gateway trust is disabled on this service (TRUST_ENVOY_HEADERS "
                  "is not the literal string 'true')")
    elif not os.getenv("ENVOY_INTERNAL_SECRET"):
        reason = ("no gateway secret is configured, so nothing can prove this "
                  "request passed through the gateway")
    elif not request.headers.get("x-inaiera-gateway-secret"):
        reason = "x-inaiera-gateway-secret was absent"
    elif not request.headers.get("x-user-name"):
        reason = "x-inaiera-gateway-secret matched but x-user-name was absent"
    else:
        reason = "x-inaiera-gateway-secret did not match"
    return (
        "This agent requires a verified identity, and this request has none: "
        + reason
        + ". Identity headers must be set by the gateway itself, not by the client."
    )


def enforce_authenticated_user(agent, request: Request, channel: str) -> Optional[str]:
    """Apply features.require_authenticated_user on ANY invocation channel
    (run, stream, batch-run, MCP, webhook).

    Returns the verified identity to use, or None when the agent has not
    opted in (in which case the caller keeps whatever it had). Raises 401
    when the agent has opted in and the request cannot prove who it is.
    `channel` only shapes the message.
    """
    if not ((getattr(agent, "features", None) or {}).get("require_authenticated_user")):
        return None
    identity = get_verified_identity(request)
    if identity:
        return identity
    detail = _identity_refusal_detail(request)
    logger.warning("Refused a %s invocation for lack of a verified identity: %s",
                   channel, detail)
    raise HTTPException(401, f"[{channel}] {detail}")


def require_verified_identity(request: Request) -> str:
    """Raise 401 rather than silently falling back to a self-reported
    identity, for any agent that has opted into
    features.require_authenticated_user."""
    identity = get_verified_identity(request)
    if not identity:
        detail = _identity_refusal_detail(request)
        logger.warning("Refused a run for lack of a verified identity: %s", detail)
        raise HTTPException(401, detail)
    return identity
