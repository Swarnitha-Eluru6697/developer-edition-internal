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

# Set by POST /api/v1/auth/exchange; carries the same control-plane JWT.
SESSION_COOKIE_NAME = "workspace_session"


@dataclass
class Principal:
    """The caller: claims from a validated JWT, or the gateway-verified identity (app/core/deps.py).

    `permissions` decides what the caller may do (contract 01-CONTRACT.md). `roles` (x-user-roles / the token's `roles`
    claim) is display/audit identity only: it is passed on to UCS as the caller's identity attributes, never compared here."""
    user_id: str
    org_id: str
    roles: list[str]
    email: str | None
    issued_at: int
    expires_at: int
    # Only set by validate_jwt: the token's `preferred_username`, which is what
    # the gateway maps to x-user-name (/auth/exchange compares against it).
    username: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    groups: list[str] = field(default_factory=list)

    def has(self, permission: str) -> bool:
        """True if the caller holds `permission` exactly. No permissions = nothing."""
        return bool(self.permissions) and permission in self.permissions

    def stored_identity(self) -> dict[str, str]:
        """Groups and permissions as stored for a deferred UCS re-check (JSON arrays)."""
        import json
        return {"groups": json.dumps(list(self.groups)), "permissions": json.dumps(sorted(self.permissions))}

    def me(self) -> dict:
        """The contract's GET /me body."""
        return {"user": self.username or self.email or self.user_id, "userId": self.user_id, "tenant": self.org_id,
                "permissions": sorted(self.permissions)}


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
        roles=claims.get("roles", []),
        email=claims.get("email"),
        issued_at=claims["iat"],
        expires_at=claims["exp"],
        username=claims.get("preferred_username"),
    )


def load_list(raw: str | None) -> list[str]:
    """A stored JSON array of strings (groups / permissions); anything else = empty."""
    import json
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []
