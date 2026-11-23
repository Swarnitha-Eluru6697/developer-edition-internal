"""Scoped service JWTs, inbound and outbound (DQ-AUTH-012, RUN-DBT-005, RUN-DBT-007).

INBOUND — require_service_scope()
---------------------------------
POST /api/v1/projects/{id}/schema-tests (called only by inaiera-data-quality)
used to accept a static shared token, and later a JWT signed with THAT SAME
static token — so anyone holding the raw legacy token (which Workspace also
holds for /validate) could mint themselves a "scoped" token. Now:

  * Scoped tokens are verified with SERVICE_JWT_SECRET only, a key distinct
    from INTERNAL_SERVICE_TOKEN (Settings refuses to start if they match).
  * The raw legacy token is explicitly rejected on scoped routes.
  * Required claims: iss, sub, aud, scope, org_id, project_id, jti, iat, exp.
    iss and sub must be in their allowlists; exp - iat must not exceed
    service_jwt_max_lifetime_seconds; iat may not be in the future.
  * org_id must equal this data plane's org (a missing org is rejected).
  * The token is bound to a project: its project_id claim must equal the
    {project_id} path parameter of the route it is presented to.
  * Replay protection: each jti is consumed once (service_token_jti table,
    primary-key insert). A second presentation of the same token fails.

Cross-repo note: inaiera-data-quality must sign with SERVICE_JWT_SECRET and
include sub, project_id and jti (and org_id) in its tokens.

SCHEDULER IDENTITY — require_scheduler_scope() (DBT-003)
--------------------------------------------------------
The Dagster orchestrator used to authenticate to the /internal routes with the same static
INTERNAL_SERVICE_TOKEN that Workspace's codegen loop uses for /validate, so a leak of either opened
project enumeration AND run triggering. It now has an identity of its own: a short-lived JWT signed with
SCHEDULER_JWT_SECRET (a key distinct from every other service secret), iss/sub "dagster", aud "dbtrunner",
ONE scope per route (dbt.projects.read, dbt.runs.read, dbt.runs.trigger, dbt.runs.cancel), org-bound. A
state-changing token (trigger, cancel) is single-use; reads are not recorded. The static token is no longer accepted on those routes.

OUTBOUND — mint_scoped_token()
------------------------------
This service is itself the CALLER when publishing lineage to UCS and
registering datasets with the Datasets product (RUN-DBT-007): each call gets
a fresh short-lived, audience-bound, single-scope JWT instead of
self-asserted identity headers or no credential at all.
"""
from __future__ import annotations

import hmac
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import Header, HTTPException, Request, status
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import get_settings

log = structlog.get_logger()

# This service's own name as a JWT audience — a token minted for any other
# service must not verify here.
SERVICE_AUDIENCE = "dbtrunner"
# Audiences of tokens this service mints for others.
UCS_AUDIENCE = "ucs.inaiera"
DATASETS_AUDIENCE = "datasets.inaiera"

_REQUIRED_CLAIMS = ("iss", "sub", "aud", "scope", "org_id", "jti", "iat", "exp")


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail={"code": code, "message": message})


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                         detail={"code": code, "message": message})


async def _consume_jti(jti: str, issuer: str, exp: int) -> bool:
    """Record a jti; False if it was already used (a replay)."""
    from sqlalchemy import delete
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db import session as db_session
    from app.models import ServiceTokenJti

    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    async with db_session.AsyncSessionLocal() as db:
        # Opportunistic purge: an expired token already fails exp, so its
        # jti no longer needs remembering.
        await db.execute(delete(ServiceTokenJti).where(
            ServiceTokenJti.expires_at < datetime.now(timezone.utc)))
        result = await db.execute(
            pg_insert(ServiceTokenJti)
            .values(jti=jti, issuer=issuer, expires_at=expires_at)
            .on_conflict_do_nothing(index_elements=["jti"])
        )
        await db.commit()
        return result.rowcount == 1


def _scoped_service_dependency(
    required_scope: str, *, secret_attr: str, issuers_attr: str, subjects_attr: str | None,
    env_name: str, project_path_param: str | None, single_use_for,
):
    """The one implementation behind every scoped-service identity (DQ's and the scheduler's): HS256 with
    that identity's own secret, audience this service, required claims, bounded lifetime, allow-listed issuer
    (and subject), exactly `required_scope`, this deployment's org, optional project binding, and a jti that
    `single_use_for(scope)` says must be consumed. The raw static token is always refused here."""

    async def _check(
        request: Request,
        x_internal_service_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        settings = get_settings()
        token = x_internal_service_token
        secret = getattr(settings, secret_attr)
        if not secret:
            raise _unauthorized("SERVICE_AUTH_NOT_CONFIGURED",
                                f"scoped service authentication is not configured ({env_name})")
        if not token:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "Missing service token")
        if settings.internal_service_token and hmac.compare_digest(
                token.encode(), settings.internal_service_token.encode()):
            log.warning("raw_internal_token_rejected_on_scoped_route", scope=required_scope)
            raise _unauthorized("RAW_TOKEN_REJECTED",
                                "the static internal token is not accepted on scoped routes")

        try:
            claims = jwt.decode(
                token, secret, algorithms=["HS256"], audience=SERVICE_AUDIENCE,
                options={"verify_signature": True, "verify_aud": True, "verify_exp": True,
                         "verify_iat": True, "verify_nbf": True,
                         "require_exp": True, "require_iat": True, "require_aud": True,
                         "require_iss": True, "require_sub": True, "require_jti": True,
                         "leeway": settings.service_jwt_clock_skew_seconds},
            )
        except JWTError as e:
            raise _unauthorized("INVALID_SERVICE_TOKEN", str(e))

        missing = [c for c in _REQUIRED_CLAIMS if claims.get(c) in (None, "")]
        if project_path_param:
            if claims.get("project_id") in (None, ""):
                missing.append("project_id")
        if missing:
            raise _unauthorized("INVALID_SERVICE_TOKEN", f"token missing required claims: {missing}")

        now = int(time.time())
        iat, exp = int(claims["iat"]), int(claims["exp"])
        if iat > now + settings.service_jwt_clock_skew_seconds:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "token issued in the future")
        if exp - iat > settings.service_jwt_max_lifetime_seconds:
            raise _unauthorized("INVALID_SERVICE_TOKEN",
                                f"token lifetime exceeds {settings.service_jwt_max_lifetime_seconds}s")

        issuers = getattr(settings, issuers_attr)
        if claims["iss"] not in issuers:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "token issuer is not allowed")
        subjects = getattr(settings, subjects_attr) if subjects_attr else issuers
        if claims["sub"] not in subjects:
            raise _unauthorized("INVALID_SERVICE_TOKEN", "token subject is not allowed")

        scopes = str(claims.get("scope") or "").split()
        if required_scope not in scopes:
            raise _forbidden("INSUFFICIENT_SCOPE", f"token missing required scope '{required_scope}'")

        if claims["org_id"] != settings.org_id:
            raise _forbidden("ORG_MISMATCH", "token org does not match this deployment")

        if project_path_param:
            path_value = request.path_params.get(project_path_param)
            try:
                bound = uuid.UUID(str(claims["project_id"])) == uuid.UUID(str(path_value))
            except (ValueError, TypeError):
                bound = False
            if not bound:
                raise _forbidden("PROJECT_MISMATCH", "token is not bound to this project")

        if single_use_for(required_scope) and not await _consume_jti(str(claims["jti"]), str(claims["iss"]), exp):
            log.warning("service_token_replay_rejected", iss=claims["iss"], jti=claims["jti"])
            raise _unauthorized("TOKEN_REPLAYED", "service token has already been used")

        return claims

    _check.required_scope = required_scope  # type: ignore[attr-defined]  # read by the route drift tests
    return _check


def require_service_scope(required_scope: str, *, project_path_param: str | None = "project_id"):
    """FastAPI dependency: a valid scoped service JWT (DQ's identity, SERVICE_JWT_SECRET) carrying
    `required_scope`, bound to the route's `project_path_param` (unless None), presented exactly once."""
    return _scoped_service_dependency(
        required_scope, secret_attr="service_jwt_secret", issuers_attr="service_jwt_allowed_issuers",
        subjects_attr="service_jwt_allowed_subjects", env_name="SERVICE_JWT_SECRET",
        project_path_param=project_path_param, single_use_for=lambda _scope: True,
    )


def require_scheduler_scope(required_scope: str):
    """FastAPI dependency for the /internal routes: Dagster's own scoped identity (SCHEDULER_JWT_SECRET)
    carrying exactly `required_scope`. Not project-bound (it enumerates). Reads may be repeated; a
    state-changing scope (anything not ending in ".read") is single-use."""
    return _scoped_service_dependency(
        required_scope, secret_attr="scheduler_jwt_secret", issuers_attr="scheduler_jwt_allowed_issuers",
        subjects_attr=None, env_name="SCHEDULER_JWT_SECRET",
        project_path_param=None, single_use_for=lambda scope: not scope.endswith(".read"),
    )


def mint_scoped_token(
    *, secret: str, audience: str, scope: str, subject: str,
    org_id: str | None = None, expires_in: int = 300, extra: dict | None = None,
) -> str:
    """Mint a short-lived JWT this service presents TO another service:
    HMAC-signed with that callee's shared secret, audienced to the callee,
    exactly one scope, unique jti."""
    now = int(time.time())
    claims: dict = {
        "iss": subject,
        "sub": subject,
        "aud": audience,
        "scope": scope,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid.uuid4().hex,
    }
    if org_id:
        claims["org_id"] = org_id
    if extra:
        claims.update(extra)
    return jwt.encode(claims, secret, algorithm="HS256")
