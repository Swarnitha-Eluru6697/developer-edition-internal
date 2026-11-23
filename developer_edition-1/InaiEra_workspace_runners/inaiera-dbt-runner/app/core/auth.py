"""JWT validation using JWKS fetched from the control plane.

The control plane (Root Platform App) signs JWTs with RS256.
This module fetches the JWKS public keys at startup, caches them,
and validates tokens locally on every request.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
import structlog
from fastapi import HTTPException, status
from jose import jwk, jwt
from jose.exceptions import JWTError

from app.core.config import get_settings

log = structlog.get_logger()


@dataclass
class Principal:
    """Claims extracted from a validated JWT. `permissions` (the token's `permissions` claim, Workspace permission IDs) is
    what the caller may do; role names are never read (contract 01-CONTRACT.md)."""
    user_id: str
    org_id: str
    permissions: frozenset[str]
    email: str | None
    issued_at: int
    expires_at: int
    # Scopes the token grants (its space-separated `scope` claim). Every user-facing route demands one.
    scopes: list[str] = field(default_factory=list)
    # Who acted on the user's behalf: "workspace" for a delegated token, None for a CI service principal.
    actor: str | None = None
    # DBT-012: the workspace the Workspace service verified the caller owns for THIS call (delegated tokens only).
    workspace_id: str | None = None

    def has(self, permission: str) -> bool:
        return bool(self.permissions) and permission in self.permissions


class JWKSCache:
    """In-memory JWKS cache. One per process."""

    def __init__(self):
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0

    async def get_key(self, kid: str) -> dict:
        settings = get_settings()
        if (
            kid not in self._keys
            or (time.time() - self._fetched_at) > settings.jwks_cache_ttl_seconds
        ):
            await self._refresh()
        if kid not in self._keys:
            # Maybe the control plane rotated keys recently — force one refresh
            await self._refresh()
        if kid not in self._keys:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNKNOWN_KID", "message": f"Unknown signing key id: {kid}"},
            )
        return self._keys[kid]

    async def _refresh(self) -> None:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(settings.control_plane_jwks_url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.error("jwks_fetch_failed", error=str(e))
                # Don't blow away the existing cache if a refresh fails
                return
        data = resp.json()
        self._keys = {k["kid"]: k for k in data.get("keys", [])}
        self._fetched_at = time.time()
        log.info("jwks_refreshed", key_count=len(self._keys))

    async def refresh(self) -> None:
        """Public refresh entry point — called during app startup."""
        await self._refresh()

    def is_primed(self) -> bool:
        """True once at least one key has been cached."""
        return bool(self._keys)


_jwks_cache = JWKSCache()
# Public alias used by app.main.lifespan
jwks_cache = _jwks_cache


async def validate_jwt(token: str) -> Principal:
    """Validate a JWT and return the principal."""
    settings = get_settings()
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(e)},
        )

    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_KID", "message": "JWT header missing kid"},
        )

    key_data = await _jwks_cache.get_key(kid)
    public_key = jwk.construct(key_data)

    try:
        claims = jwt.decode(
            token,
            key=public_key.to_pem().decode("utf-8") if hasattr(public_key, "to_pem") else key_data,
            algorithms=[key_data.get("alg", "RS256")],
            audience=settings.control_plane_audience,
            issuer=settings.control_plane_issuer,
            options={"verify_signature": True, "verify_aud": True, "verify_iss": True},
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": str(e)},
        )

    # DBT-002: an externally issued token must be short-lived (a service principal's, not a long browser session).
    iat, exp = claims.get("iat"), claims.get("exp")
    cap = settings.external_jwt_max_lifetime_seconds
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_TOKEN", "message": "Token must carry iat and exp"})
    if cap and exp - iat > cap:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "TOKEN_LIFETIME_TOO_LONG",
                                    "message": f"Token lifetime {int(exp - iat)}s exceeds the {cap}s maximum"})

    org_id_claim = claims.get("org_id")
    if org_id_claim != settings.org_id:
        # Token is for a different org than this data plane serves.
        # This should never happen if routing is correct, but defense-in-depth.
        log.warning(
            "org_mismatch",
            token_org=org_id_claim,
            dataplane_org=settings.org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ORG_MISMATCH", "message": "Token org does not match this data plane"},
        )

    return Principal(
        user_id=claims["sub"],
        org_id=claims["org_id"],
        permissions=_permissions(claims),
        email=claims.get("email"),
        issued_at=claims["iat"],
        expires_at=claims["exp"],
        scopes=_scopes(claims),
    )


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


def _delegation_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def validate_delegated_jwt(token: str) -> Principal:
    """Validate the short-lived token Workspace presents on a user's behalf (X-001).

    HS256 with DELEGATION_JWT_SECRET only (never the JWKS), audience "dbtrunner", an allowed issuer,
    act.sub equal to the issuer, a bounded lifetime, this data plane's org. The Principal is the END
    USER (sub / email), carries the single scope the call needs and the caller's permissions for it (`permissions`). A state-changing token
    (any scope not ending in ".read") is consumed once: a replay is refused.
    """
    from app.core.service_auth import (  # lazy: service_auth imports the DB layer
        SERVICE_AUDIENCE,
        _consume_jti,
    )

    settings = get_settings()
    # Each issuer signs with its own key: DQ's (X-001) is chosen by the token's claimed issuer, and is only a LOOKUP
    # of the key, the signature is still verified with it (a token that lies about its issuer fails the signature).
    try:
        claimed_issuer = jwt.get_unverified_claims(token).get("iss")
    except JWTError as e:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", str(e))
    from_dq = claimed_issuer == settings.dq_delegation_issuer
    secret = settings.dq_delegation_jwt_secret if from_dq else settings.delegation_jwt_secret
    if not secret:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "DELEGATION_NOT_CONFIGURED",
                                "delegated tokens from the Data Quality service are not configured (DQ_DELEGATION_JWT_SECRET)"
                                if from_dq else
                                "delegated Workspace tokens are not configured (DELEGATION_JWT_SECRET)")
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], audience=SERVICE_AUDIENCE,
            options={"verify_signature": True, "verify_aud": True, "verify_exp": True, "verify_iat": True,
                     "verify_nbf": True, "require_exp": True, "require_iat": True, "require_aud": True,
                     "require_iss": True, "require_sub": True, "require_jti": True,
                     "leeway": settings.delegation_jwt_clock_skew_seconds},
        )
    except JWTError as e:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", str(e))

    missing = [c for c in ("iss", "sub", "aud", "scope", "org_id", "jti", "iat", "exp") if claims.get(c) in (None, "")]
    if missing:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", f"token missing required claims: {missing}")
    if claims["iss"] != claimed_issuer:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", "token issuer is not allowed")
    if not from_dq and claims["iss"] not in settings.delegation_jwt_allowed_issuers:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", "token issuer is not allowed")
    actor = (claims.get("act") or {}).get("sub") if isinstance(claims.get("act"), dict) else None
    if actor != claims["iss"]:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", "token is not a delegation by its issuer")
    now = int(time.time())
    iat, exp = int(claims["iat"]), int(claims["exp"])
    if iat > now + settings.delegation_jwt_clock_skew_seconds:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN", "token issued in the future")
    if exp - iat > settings.delegation_jwt_max_lifetime_seconds:
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN",
                                f"token lifetime exceeds {settings.delegation_jwt_max_lifetime_seconds}s")
    if claims["org_id"] != settings.org_id:
        log.warning("org_mismatch", token_org=claims["org_id"], dataplane_org=settings.org_id)
        raise _delegation_error(status.HTTP_403_FORBIDDEN, "ORG_MISMATCH", "Token org does not match this data plane")

    scopes = _scopes(claims)
    if from_dq and not set(scopes) <= set(settings.dq_delegation_allowed_scopes):
        raise _delegation_error(status.HTTP_403_FORBIDDEN, "INSUFFICIENT_SCOPE",
                                f"the Data Quality service may only act with {settings.dq_delegation_allowed_scopes}")
    if any(not sc.endswith(".read") for sc in scopes) and not await _consume_jti(
            str(claims["jti"]), str(claims["iss"]), exp):
        log.warning("delegated_token_replay_rejected", iss=claims["iss"], jti=claims["jti"])
        raise _delegation_error(status.HTTP_401_UNAUTHORIZED, "TOKEN_REPLAYED", "delegated token has already been used")

    return Principal(
        user_id=str(claims["sub"]), org_id=claims["org_id"],
        permissions=_permissions(claims),
        email=claims.get("email"), issued_at=iat, expires_at=exp, scopes=scopes, actor=str(actor),
        workspace_id=str(claims["workspace_id"]) if claims.get("workspace_id") else None,
    )
