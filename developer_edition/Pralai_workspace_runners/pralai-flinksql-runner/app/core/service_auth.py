"""Short-lived, narrowly-scoped service-to-service JWTs (DQ-AUTH-012,
hardened by RUN-FLINK-009).

  - mint_service_token(): used by quality_consumer._post_to_ucs (and the
    Kafka preview policy check) to mint a signed, short-lived, scoped JWT
    instead of sending the raw shared secret. settings.ucs_internal_token
    is an HMAC signing key, never a bearer value.
  - require_service_scope(): verifies a scoped JWT on /api/v1/validate.

RUN-FLINK-009 org boundary:
  * a service JWT MUST carry an org_id claim equal to this data plane's
    org — a token minted with no org claim used to pass the check because
    the comparison was skipped when the claim was absent;
  * the legacy "raw static token as bearer" fallback is OFF unless
    LEGACY_INTERNAL_TOKEN_ENABLED is explicitly true (unset = dev only, for
    local stacks whose Workspace still sends the static token). When on,
    it is constant-time compared, limited to LEGACY_TOKEN_SCOPES, and
    implicitly bound to this data plane's org (the static secret is
    provisioned per data plane — there is no cross-org token to accept).
  * the old, unused app.core.deps.require_internal_token (plain-equality,
    unscoped, org-unbound) has been deleted.
"""
from __future__ import annotations

import hmac
import time
import uuid
from typing import Annotated

import structlog
from fastapi import Header, HTTPException, status
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import get_settings

log = structlog.get_logger()

# This service's own name as a JWT audience.
SERVICE_AUDIENCE = "flinksql-runner"

SERVICE_TOKEN_TTL_SECONDS = 60

# The only scopes the legacy static token may stand in for (when enabled).
LEGACY_TOKEN_SCOPES = frozenset({"flink.jobs.validate"})


def mint_service_token(
    *, signing_key: str, issuer: str, audience: str, scope: str,
    org_id: str | None = None, ttl_seconds: int = SERVICE_TOKEN_TTL_SECONDS,
) -> str:
    """Mint one short-lived HS256 JWT authorizing exactly `scope` on `audience`."""
    now = int(time.time())
    claims = {
        "iss": issuer, "aud": audience, "scope": scope, "org_id": org_id,
        "iat": now, "exp": now + ttl_seconds, "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, signing_key, algorithm="HS256")


def _invalid() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail={"code": "INVALID_SERVICE_TOKEN",
                                 "message": "Missing or invalid internal service token"})


def require_service_scope(required_scope: str):
    """FastAPI dependency requiring `required_scope` on a signed, org-bound
    service JWT. Returns the verified claims, or None when the (explicitly
    enabled) legacy static token was accepted."""

    async def _check(
        x_internal_service_token: Annotated[str | None, Header()] = None,
    ) -> dict | None:
        settings = get_settings()
        if not settings.internal_service_token or not x_internal_service_token:
            raise _invalid()
        try:
            claims = jwt.decode(
                x_internal_service_token, settings.internal_service_token,
                algorithms=["HS256"], audience=SERVICE_AUDIENCE,
                options={"verify_signature": True, "verify_aud": True, "verify_exp": True},
            )
        except JWTError:
            if (
                settings.legacy_internal_token_allowed
                and required_scope in LEGACY_TOKEN_SCOPES
                and hmac.compare_digest(x_internal_service_token.encode(),
                                        settings.internal_service_token.encode())
            ):
                log.info("legacy_internal_token_accepted", scope=required_scope,
                         org_id=settings.org_id)
                return None
            raise _invalid()

        scopes = (claims.get("scope") or "").split()
        if required_scope not in scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail={"code": "INSUFFICIENT_SCOPE",
                                        "message": f"token missing required scope '{required_scope}'"})
        token_org = claims.get("org_id")
        if not token_org:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail={"code": "ORG_REQUIRED",
                                        "message": "service token carries no org_id claim"})
        if token_org != settings.org_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail={"code": "ORG_MISMATCH",
                                        "message": "token org does not match this deployment"})
        return claims

    return _check
