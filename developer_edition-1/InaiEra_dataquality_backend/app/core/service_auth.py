"""Service-to-service JWTs (DQ-AUTH-012).

OUTBOUND — mint_service_token()
  Every call DQ makes to UCS, the dbt runner or the FlinkSQL runner carries
  a freshly minted HS256 JWT:
    iss/sub  settings.service_jwt_issuer / service_jwt_subject ("dataquality")
    aud      the ONE target service (per-integration setting)
    scope    the ONE operation (e.g. "dbt.schema-tests.write")
    org_id   the org the call acts for (required — never minted without one)
    iat/exp  lifetime = service_jwt_ttl_seconds (capped at 300s)
    jti      unique per token (the dbt runner consumes it once)
    + operation-specific binding claims (e.g. project_id for the dbt runner)
  signed with THAT integration's own secret (ucs_/dbtrunner_/flinksql_
  service_jwt_secret). No standing token or raw shared secret is ever sent.

INBOUND — require_inbound_service_scope()
  DQ's own service-only route (/api/v1/render/validate/rule, called by the
  Workspace codegen loop) requires the same kind of token: aud
  "dataquality", the route's scope, org_id == this data plane, allowlisted
  issuer, capped lifetime, single-use jti. The legacy static token is only
  accepted when LEGACY_INTERNAL_TOKEN_ENABLED=true AND ENVIRONMENT=dev.
"""
from __future__ import annotations

import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

import structlog
from fastapi import Header, HTTPException, status
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import MAX_SERVICE_TOKEN_TTL_SECONDS, Settings, get_settings

log = structlog.get_logger()

# Scopes DQ requests from other services.
SCOPE_DBT_SCHEMA_TESTS_WRITE = "dbt.schema-tests.write"
SCOPE_FLINK_VALIDATE = "flink.jobs.validate"
SCOPE_UCS_CHECKS_WRITE = "ucs.quality.checks.write"
SCOPE_UCS_AUDIT_WRITE = "ucs.audit.events.write"

# DQ's own audience + inbound scopes.
DQ_AUDIENCE = "dataquality"
SCOPE_DQ_RULES_VALIDATE = "dq.rules.validate"


class ServiceAuthNotConfigured(RuntimeError):
    """The integration is disabled or has no signing secret."""


@dataclass(frozen=True)
class _Integration:
    enabled_attr: str
    secret_attr: str
    audience_attr: str


INTEGRATIONS: dict[str, _Integration] = {
    "ucs": _Integration("ucs_enabled", "ucs_service_jwt_secret", "ucs_audience"),
    "dbtrunner": _Integration("dbtrunner_enabled", "dbtrunner_service_jwt_secret",
                              "dbtrunner_audience"),
    "flinksql": _Integration("flinksql_enabled", "flinksql_service_jwt_secret",
                             "flinksql_audience"),
}


def mint_service_token(
    integration: str, *, scope: str, org_id: str,
    extra_claims: dict[str, Any] | None = None, settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    spec = INTEGRATIONS[integration]
    if not getattr(settings, spec.enabled_attr):
        raise ServiceAuthNotConfigured(f"{integration} integration is disabled")
    secret = getattr(settings, spec.secret_attr)
    if not secret:
        raise ServiceAuthNotConfigured(f"{spec.secret_attr.upper()} is not configured")
    if not org_id:
        raise ServiceAuthNotConfigured("refusing to mint a service token without an org_id")
    if not scope or " " in scope:
        raise ValueError("exactly one scope per token")
    ttl = min(int(settings.service_jwt_ttl_seconds), MAX_SERVICE_TOKEN_TTL_SECONDS)
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": settings.service_jwt_issuer,
        "sub": settings.service_jwt_subject,
        "aud": getattr(settings, spec.audience_attr),
        "scope": scope,
        "org_id": org_id,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    for k, v in (extra_claims or {}).items():
        if k in claims:
            raise ValueError(f"extra claim {k!r} would override a reserved claim")
        claims[k] = v
    return jwt.encode(claims, secret, algorithm="HS256")


# ── Inbound ────────────────────────────────────────────────────────────

def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail={"code": code, "message": message})


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                         detail={"code": code, "message": message})


async def _consume_jti(jti: str, issuer: str, exp: int) -> bool:
    """Record a jti once; False on replay."""
    from sqlalchemy import delete
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import session as db_session
    from app.models import ServiceTokenJti

    async with db_session.AsyncSessionLocal() as db:
        await db.execute(delete(ServiceTokenJti).where(
            ServiceTokenJti.expires_at < datetime.now(timezone.utc)))
        result = await db.execute(
            pg_insert(ServiceTokenJti)
            .values(jti=jti, issuer=issuer, expires_at=datetime.fromtimestamp(exp, tz=timezone.utc))
            .on_conflict_do_nothing(index_elements=["jti"]))
        await db.commit()
        return result.rowcount == 1


def require_inbound_service_scope(required_scope: str):
    async def _check(
        x_internal_service_token: Annotated[str | None, Header()] = None,
    ) -> dict | None:
        settings = get_settings()
        token = x_internal_service_token
        if not token:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "Missing service token")

        if settings.legacy_internal_token_enabled and settings.environment == "dev" \
                and settings.internal_service_token \
                and hmac.compare_digest(token.encode(), settings.internal_service_token.encode()):
            log.warning("legacy_internal_token_accepted", scope=required_scope)
            return None

        if not settings.inbound_service_jwt_secret:
            raise _unauthorized("SERVICE_AUTH_NOT_CONFIGURED",
                                "INBOUND_SERVICE_JWT_SECRET is not configured")
        try:
            claims = jwt.decode(
                token, settings.inbound_service_jwt_secret, algorithms=["HS256"],
                audience=DQ_AUDIENCE,
                options={"require_exp": True, "require_iat": True, "require_aud": True,
                         "require_iss": True, "require_jti": True, "leeway": 30},
            )
        except JWTError as e:
            raise _unauthorized("INVALID_SERVICE_TOKEN", str(e))

        if int(claims["exp"]) - int(claims["iat"]) > MAX_SERVICE_TOKEN_TTL_SECONDS:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "token lifetime too long")
        if claims["iss"] not in settings.inbound_service_jwt_allowed_issuers:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "token issuer is not allowed")
        if required_scope not in str(claims.get("scope") or "").split():
            raise _forbidden("INSUFFICIENT_SCOPE", f"token missing required scope '{required_scope}'")
        if not claims.get("org_id"):
            raise _forbidden("ORG_REQUIRED", "service token carries no org_id claim")
        if claims["org_id"] != settings.org_id:
            raise _forbidden("ORG_MISMATCH", "token org does not match this deployment")
        if not await _consume_jti(str(claims["jti"]), str(claims["iss"]), int(claims["exp"])):
            raise _unauthorized("TOKEN_REPLAYED", "service token has already been used")
        return claims

    return _check
