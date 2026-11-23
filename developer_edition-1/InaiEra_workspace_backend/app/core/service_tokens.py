"""Scoped service tokens for Workspace -> runner/DQ calls.

The FlinkSQL Runner (/api/v1/validate) and the DQ service
(/api/v1/render/validate/rule) now require a short-lived, org-bound,
audience- and scope-limited HS256 JWT in X-Internal-Service-Token instead of a
raw shared static token (RUN-FLINK-009 / DQ-AUTH-012):

    iss/sub  "workspace"
    aud      the one target service ("flinksql-runner" | "dataquality")
    scope    exactly one operation ("flink.jobs.validate" | "dq.rules.validate")
    org_id   this data plane's org (the callee rejects any other)
    iat/exp  short lifetime (callees cap it at 300s)
    jti      unique per call (DQ consumes it once — never reuse a token)

Dev-only compatibility: a service still running an older image only knows the
raw static token. In ENVIRONMENT=dev, a 401 to the scoped token is retried once
with the legacy raw token so a mixed old/new local stack keeps working. Outside
dev there is no fallback — the scoped token is the only credential sent.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import structlog
from jose import jwt

from app.core.config import get_settings
from app.core.errors import WorkspaceError

log = structlog.get_logger()

SERVICE_TOKEN_ISSUER = "workspace"
SERVICE_TOKEN_TTL_SECONDS = 120

FLINK_AUDIENCE = "flinksql-runner"
FLINK_SCOPE_VALIDATE = "flink.jobs.validate"
DQ_AUDIENCE = "dataquality"
DQ_SCOPE_RULES_VALIDATE = "dq.rules.validate"

# ── Delegated user tokens (X-001) ─────────────────────────────────────────
# Workspace -> runner calls made on a user's behalf carry a short-lived token minted here, NOT the
# user's own bearer/cookie:  HS256, aud = the one runner, ONE scope, sub = the gateway-authenticated
# user (with their org / email), act.sub = "workspace", and `permissions` = the caller's permissions
# that THIS scope needs at the runner (never their roles: runners check permission IDs, contract
# 01-CONTRACT.md). Each runner verifies it with its own DELEGATION_JWT_SECRET, so a credential for
# one service means nothing at another.
DBTRUNNER_AUDIENCE = "dbtrunner"
DELEGATION_TTL_SECONDS = 120

DBT_SCOPE_PROJECTS_READ = "dbt.projects.read"
DBT_SCOPE_PROJECTS_WRITE = "dbt.projects.write"
DBT_SCOPE_RUNS_READ = "dbt.runs.read"
DBT_SCOPE_RUNS_TRIGGER = "dbt.runs.trigger"
FLINK_SCOPE_JOBS_READ = "flink.jobs.read"
FLINK_SCOPE_JOBS_EDIT = "flink.jobs.edit"
FLINK_SCOPE_JOBS_DEPLOY = "flink.jobs.deploy"

# The DQ service's own permission vocabulary (DQ app/core/permissions.py). A delegated token's scope is a CAP on what the
# user may do in DQ: it can only narrow it, never widen it.
DQ_SCOPE_READ = "dq.read"
DQ_SCOPE_RULES_WRITE = "dq.rules.write"
DQ_SCOPE_BIND = "dq.bind"
DQ_SCOPE_APPLY = "dq.apply"
DQ_SCOPE_SYNC = "dq.sync"
# A rule write may be a privileged custom_sql rule, which DQ additionally gates on dq.custom_sql.write (DQ-SEC-006).
DQ_RULE_WRITE_SCOPES = "dq.rules.write dq.custom_sql.write"


def mint_service_token(*, secret: str, audience: str, scope: str,
                       ttl_seconds: int = SERVICE_TOKEN_TTL_SECONDS) -> str:
    """One fresh token per call — DQ rejects a replayed jti."""
    now = int(time.time())
    claims = {
        "iss": SERVICE_TOKEN_ISSUER,
        "sub": SERVICE_TOKEN_ISSUER,
        "aud": audience,
        "scope": scope,
        "org_id": get_settings().org_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


async def post_with_service_token(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: Any,
    signing_secret: str | None,
    audience: str,
    scope: str,
    legacy_token: str | None,
) -> httpx.Response:
    """POST with a scoped service token; in dev only, retry once with the
    legacy raw token if the callee (an older image) rejects the JWT with 401."""
    settings = get_settings()
    if signing_secret:
        token = mint_service_token(secret=signing_secret, audience=audience, scope=scope)
        resp = await client.post(url, json=json, headers={"X-Internal-Service-Token": token})
        if resp.status_code != 401 or settings.environment != "dev" or not legacy_token:
            return resp
        log.warning("service_token_rejected_falling_back_to_legacy_dev_only", audience=audience)
    elif settings.environment != "dev":
        # No signing secret outside dev: never send a raw shared token.
        return httpx.Response(
            503, json={"code": "SERVICE_AUTH_NOT_CONFIGURED",
                       "message": f"no service-token secret configured for {audience}"},
        )
    return await client.post(url, json=json, headers={"X-Internal-Service-Token": legacy_token or ""})


# What each delegated scope may need at its runner: the token carries the caller's permissions out of this set, so the
# runner can check the permission its route (and, for a dbt-runner job, the job's kind) needs. A permission the caller does
# not hold is never put in a token.
_JOB_EDIT = ("workspace:dbt:edit", "workspace:sql-jobs:edit", "workspace:python-jobs:edit")
_JOB_RUN = ("workspace:dbt:run", "workspace:sql-jobs:run", "workspace:python-jobs:run")
SCOPE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    DBT_SCOPE_PROJECTS_READ: _JOB_EDIT + _JOB_RUN,
    DBT_SCOPE_PROJECTS_WRITE: _JOB_EDIT,
    DBT_SCOPE_RUNS_READ: _JOB_RUN,
    DBT_SCOPE_RUNS_TRIGGER: _JOB_RUN,
    FLINK_SCOPE_JOBS_READ: ("workspace:flink:edit", "workspace:flink:deploy", "workspace:catalog:view"),
    FLINK_SCOPE_JOBS_EDIT: ("workspace:flink:edit",),
    FLINK_SCOPE_JOBS_DEPLOY: ("workspace:flink:deploy",),
    "flink.catalog.read": ("workspace:catalog:view", "workspace:datasets:view"),
    DQ_SCOPE_READ: ("workspace:dq:view", "workspace:dq:run"),
    DQ_SCOPE_RULES_WRITE: ("workspace:dq:create", "workspace:dq:edit"),
    "dq.custom_sql.write": ("workspace:dq:manage",),
    DQ_SCOPE_BIND: ("workspace:dq:publish",),
    DQ_SCOPE_APPLY: ("workspace:dq:publish",),
    DQ_SCOPE_SYNC: ("workspace:dq:publish",),
}


def delegated_permissions(principal: Any, scope: str) -> list[str]:
    """The caller's permissions that `scope` (one or more space-separated scopes) may need downstream, sorted."""
    needed = {p for sc in scope.split() for p in SCOPE_PERMISSIONS.get(sc, ())}
    held = getattr(principal, "permissions", None) or frozenset()
    return sorted(needed & set(held))


def mint_delegated_token(*, secret: str, audience: str, scope: str, principal: Any,
                         ttl_seconds: int = DELEGATION_TTL_SECONDS, workspace_id: Any = None) -> str:
    """One fresh delegated token per call: the end user as `sub`, Workspace as the actor, and the caller's permissions this
    scope needs (`permissions`) — the runner authorises on those, never on role names.

    `workspace_id`, when given, must already have been checked with app.core.workspace_scope.authorize_workspace: it is
    the authorized workspace this one call may associate a resource with (W-016). The runners accept a workspace in a
    request body only if it equals this claim."""
    now = int(time.time())
    claims = {
        "iss": SERVICE_TOKEN_ISSUER,
        "act": {"sub": SERVICE_TOKEN_ISSUER},
        "sub": str(principal.user_id),
        "aud": audience,
        "scope": scope,
        "permissions": delegated_permissions(principal, scope),
        "org_id": principal.org_id,
        "email": principal.email,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    if audience == DQ_AUDIENCE:
        # Compatibility (open question, see docs/auth/): the Data Quality service still derives its dq.* permissions from the
        # caller's identity attributes until it reads `permissions`. Passed through as opaque identity, never compared here.
        claims["roles"] = list(principal.roles)
    if workspace_id is not None:
        claims["workspace_id"] = str(workspace_id)
    return jwt.encode(claims, secret, algorithm="HS256")


def delegated_authorization(principal: Any, *, audience: str, scope: str, workspace_id: Any = None) -> str:
    """`Authorization` header value for a runner call made as `principal`. Fails closed: with no
    delegation secret configured the call is refused (503); the user's own credential is never a fallback."""
    s = get_settings()
    secret = {DBTRUNNER_AUDIENCE: s.dbtrunner_delegation_jwt_secret,
              FLINK_AUDIENCE: s.flinksql_delegation_jwt_secret,
              DQ_AUDIENCE: s.dq_delegation_jwt_secret}.get(audience)
    if not secret:
        raise WorkspaceError(503, "SERVICE_AUTH_NOT_CONFIGURED",
                             f"no delegation secret configured for {audience}")
    return ("Bearer " + mint_delegated_token(secret=secret, audience=audience, scope=scope, principal=principal,
                                             workspace_id=workspace_id))
