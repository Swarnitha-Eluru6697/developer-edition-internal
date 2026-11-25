"""Inbound user JWT validation (control-plane JWKS).

DQ-AUTH-012 (inbound): every token is verified for signature, audience,
issuer and expiry, and is ORG-BOUND — its org_id must equal this data
plane's ORG_ID or the request is refused (403 ORG_MISMATCH). The token's
roles / `permissions` / `scope` claims are resolved into DQ permissions
(app.core.permissions) once, here.
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
    user_id: str
    org_id: str
    roles: list[str]
    email: str | None
    issued_at: int
    expires_at: int
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions


class JWKSCache:
    def __init__(self):
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0

    async def get_key(self, kid: str) -> dict:
        settings = get_settings()
        if kid not in self._keys or (time.time() - self._fetched_at) > settings.jwks_cache_ttl_seconds:
            await self._refresh()
        if kid not in self._keys:
            await self._refresh()
        if kid not in self._keys:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail={"code": "UNKNOWN_KID", "message": f"Unknown signing key: {kid}"})
        return self._keys[kid]

    async def _refresh(self) -> None:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(settings.control_plane_jwks_url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.error("jwks_fetch_failed", error=str(e))
                return
        data = resp.json()
        self._keys = {k["kid"]: k for k in data.get("keys", [])}
        self._fetched_at = time.time()
        log.info("jwks_refreshed", key_count=len(self._keys))

    async def refresh(self) -> None:
        await self._refresh()

    def is_primed(self) -> bool:
        return bool(self._keys)


_jwks_cache = JWKSCache()
jwks_cache = _jwks_cache


async def validate_jwt(token: str) -> Principal:
    settings = get_settings()
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_TOKEN", "message": str(e)})
    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "MISSING_KID", "message": "JWT header missing kid"})
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_TOKEN", "message": str(e)})
    for required in ("sub", "org_id", "exp", "iat"):
        if claims.get(required) in (None, ""):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail={"code": "INVALID_TOKEN",
                                        "message": f"token missing required claim {required!r}"})
    org_id_claim = claims.get("org_id")
    if org_id_claim != settings.org_id:
        log.warning("org_mismatch", token_org=org_id_claim, dataplane_org=settings.org_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "ORG_MISMATCH", "message": "Token org does not match"})
    from app.core.permissions import resolve_permissions
    roles = list(claims.get("roles") or [])
    return Principal(user_id=claims["sub"], org_id=claims["org_id"],
                     roles=roles, email=claims.get("email"),
                     issued_at=claims["iat"], expires_at=claims["exp"],
                     permissions=resolve_permissions(roles, claims, settings))
