"""JWT validation — shared with workspace-service (same JWKS).

RUN-FLINK-009:
  * the token's org_id must equal this data plane's org (a validly signed
    token for another org is 403, a token with no org claim is 403);
  * a token without `sub` is a 401 (it used to surface as a 500 KeyError);
  * the JWKS document is cached for settings.jwks_cache_ttl_seconds instead
    of being re-fetched on every single request, and refreshed early (rate
    limited) when a signature fails to verify against the cached keys, so
    a control-plane key rotation is picked up without waiting out the TTL.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
import structlog
from fastapi import HTTPException, status
from jose import JWTError, jwt

from app.core.config import get_settings

log = structlog.get_logger()


@dataclass
class Principal:
    user_id: str
    org_id: str
    # The token's `permissions` claim (Workspace permission IDs): what the caller may do. Role names are never read.
    permissions: frozenset[str] = field(default_factory=frozenset)
    email: str | None = None
    # Scopes the token grants (its space-separated `scope` claim); every user-facing route demands one.
    scopes: list[str] = field(default_factory=list)
    # "workspace" for a delegated token; None for an external-CI service principal.
    actor: str | None = None
    # FL-003: the workspace the Workspace service verified the caller owns for THIS call (delegated tokens only).
    workspace_id: str | None = None


# url -> (fetched_at_monotonic, jwks)
_JWKS_CACHE: dict[str, tuple[float, dict]] = {}


def reset_jwks_cache() -> None:
    _JWKS_CACHE.clear()


async def _fetch_jwks(url: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.json()


async def get_jwks(*, force_refresh: bool = False) -> tuple[dict, bool]:
    """Return (jwks, fetched_now). Serves from cache while younger than
    jwks_cache_ttl_seconds; `force_refresh` re-fetches unless the cached
    copy is younger than jwks_min_refresh_interval_seconds (so a flood of
    bad tokens can't turn into a flood of JWKS fetches)."""
    settings = get_settings()
    url = settings.control_plane_jwks_url
    now = time.monotonic()
    cached = _JWKS_CACHE.get(url)
    if cached:
        age = now - cached[0]
        if not force_refresh and age < settings.jwks_cache_ttl_seconds:
            return cached[1], False
        if force_refresh and age < settings.jwks_min_refresh_interval_seconds:
            return cached[1], False
    try:
        jwks = await _fetch_jwks(url)
    except Exception as e:  # noqa: BLE001
        if cached:
            log.warning("jwks_refresh_failed_serving_cached", error=str(e))
            return cached[1], False
        log.error("jwks_fetch_failed", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "JWKS_UNAVAILABLE",
                                    "message": "Unable to fetch signing keys"})
    _JWKS_CACHE[url] = (time.monotonic(), jwks)
    return jwks, True


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail={"code": code, "message": message})


async def validate_jwt(token: str) -> Principal:
    settings = get_settings()
    if not token:
        raise _unauthorized("MISSING_TOKEN", "Bearer token required")

    def _decode(jwks: dict) -> dict:
        return jwt.decode(token, jwks, algorithms=["RS256"],
                          audience=settings.control_plane_audience,
                          issuer=settings.control_plane_issuer)

    jwks, fetched_now = await get_jwks()
    try:
        payload = _decode(jwks)
    except JWTError as first_error:
        if fetched_now:
            raise _unauthorized("INVALID_TOKEN", str(first_error))
        # Possibly a rotated signing key — refresh once (rate limited) and retry.
        jwks, fetched_now = await get_jwks(force_refresh=True)
        if not fetched_now:
            raise _unauthorized("INVALID_TOKEN", str(first_error))
        try:
            payload = _decode(jwks)
        except JWTError as e:
            raise _unauthorized("INVALID_TOKEN", str(e))

    # DBT-002: an externally issued token must be short-lived (a service principal's, not a long browser session).
    iat, exp = payload.get("iat"), payload.get("exp")
    cap = settings.external_jwt_max_lifetime_seconds
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise _unauthorized("INVALID_TOKEN", "Token must carry iat and exp")
    if cap and exp - iat > cap:
        raise _unauthorized("TOKEN_LIFETIME_TOO_LONG", f"Token lifetime {int(exp - iat)}s exceeds the {cap}s maximum")

    # The data-plane boundary: a valid token signed by the same control
    # plane can still belong to a different org.
    org_id_claim = payload.get("org_id")
    if not org_id_claim or org_id_claim != settings.org_id:
        log.warning("org_mismatch", token_org=org_id_claim, dataplane_org=settings.org_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "ORG_MISMATCH",
                                    "message": "Token org does not match this data plane"})

    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise _unauthorized("INVALID_TOKEN", "Token has no subject (sub) claim")

    return Principal(user_id=sub, org_id=org_id_claim,
                     permissions=_permissions(payload), email=payload.get("email"),
                     scopes=_scopes(payload))


def _permissions(claims: dict) -> frozenset[str]:
    """The token's `permissions` claim: a JSON array (or a space-separated string) of permission IDs. Anything else = none."""
    raw = claims.get("permissions")
    if isinstance(raw, str):
        raw = raw.split()
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(p).strip() for p in raw if isinstance(p, str) and p.strip())


def _scopes(claims: dict) -> list[str]:
    raw = claims.get("scope") or claims.get("scp") or ""
    return raw.split() if isinstance(raw, str) else [str(x) for x in raw]


async def validate_delegated_jwt(token: str) -> Principal:
    """Validate the short-lived token Workspace presents on a user's behalf (X-001).

    HS256 with DELEGATION_JWT_SECRET only (never the JWKS), audience "flinksql-runner", an allowed issuer,
    act.sub equal to the issuer, a bounded lifetime and this data plane's org. The Principal is the END
    USER (sub / email), carrying the single scope the call needs and the caller's permissions for it (`permissions`).
    """
    from app.core.service_auth import (
        SERVICE_AUDIENCE,  # lazy: service_auth imports this module's siblings
    )

    settings = get_settings()
    # Each issuer signs with its own key: DQ's (X-001) is chosen by the token's claimed issuer; that only LOOKS UP the
    # key, the signature is still verified with it (a token that lies about its issuer fails the signature).
    try:
        claimed_issuer = jwt.get_unverified_claims(token).get("iss")
    except JWTError as e:
        raise _unauthorized("INVALID_TOKEN", str(e))
    from_dq = claimed_issuer == settings.dq_delegation_issuer
    secret = settings.dq_delegation_jwt_secret if from_dq else settings.delegation_jwt_secret
    if not secret:
        raise _unauthorized("DELEGATION_NOT_CONFIGURED",
                            "delegated tokens from the Data Quality service are not configured (DQ_DELEGATION_JWT_SECRET)"
                            if from_dq else "delegated Workspace tokens are not configured (DELEGATION_JWT_SECRET)")
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], audience=SERVICE_AUDIENCE,
            options={"verify_signature": True, "verify_aud": True, "verify_exp": True, "verify_iat": True,
                     "verify_nbf": True, "require_exp": True, "require_iat": True, "require_aud": True,
                     "require_iss": True, "require_sub": True, "require_jti": True,
                     "leeway": settings.delegation_jwt_clock_skew_seconds},
        )
    except JWTError as e:
        raise _unauthorized("INVALID_TOKEN", str(e))
    missing = [c for c in ("iss", "sub", "aud", "scope", "org_id", "jti", "iat", "exp") if claims.get(c) in (None, "")]
    if missing:
        raise _unauthorized("INVALID_TOKEN", f"token missing required claims: {missing}")
    if claims["iss"] != claimed_issuer:
        raise _unauthorized("INVALID_TOKEN", "token issuer is not allowed")
    if not from_dq and claims["iss"] not in settings.delegation_jwt_allowed_issuers:
        raise _unauthorized("INVALID_TOKEN", "token issuer is not allowed")
    act = claims.get("act")
    actor = act.get("sub") if isinstance(act, dict) else None
    if actor != claims["iss"]:
        raise _unauthorized("INVALID_TOKEN", "token is not a delegation by its issuer")
    now = int(time.time())
    iat, exp = int(claims["iat"]), int(claims["exp"])
    if iat > now + settings.delegation_jwt_clock_skew_seconds:
        raise _unauthorized("INVALID_TOKEN", "token issued in the future")
    if exp - iat > settings.delegation_jwt_max_lifetime_seconds:
        raise _unauthorized("INVALID_TOKEN", f"token lifetime exceeds {settings.delegation_jwt_max_lifetime_seconds}s")
    if claims["org_id"] != settings.org_id:
        log.warning("org_mismatch", token_org=claims["org_id"], dataplane_org=settings.org_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "ORG_MISMATCH", "message": "Token org does not match this data plane"})
    scopes = _scopes(claims)
    if from_dq and not set(scopes) <= set(settings.dq_delegation_allowed_scopes):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
            "code": "INSUFFICIENT_SCOPE",
            "message": f"the Data Quality service may only act with {settings.dq_delegation_allowed_scopes}"})
    return Principal(user_id=str(claims["sub"]), org_id=claims["org_id"],
                     permissions=_permissions(claims),
                     email=claims.get("email"), scopes=scopes, actor=str(actor),
                     workspace_id=str(claims["workspace_id"]) if claims.get("workspace_id") else None)
