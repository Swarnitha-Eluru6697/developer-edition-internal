"""The credential a dbt-runner run presents to run its SQL job on a Workspace connection.

The runner mints one token per run (app/executors/connection_sql_job.py there), signed with the secret both services share
(RUNNER_JOB_JWT_SECRET here, WORKSPACE_JOB_JWT_SECRET there):

    iss/sub  "dbtrunner"        aud  "workspace"        scope  "sql.jobs.run"        org_id  this data plane's org
    project_id, run_id, connection_id   what this run is allowed to ask for
    iat/exp  at most MAX_LIFETIME apart      jti  consumed once: a replayed token is refused

The token names the job, not the user: who it runs as, and on which connection, comes from the binding the Workspace wrote when the
job was registered (app.models.JobConnectionBinding).
"""
from __future__ import annotations

import uuid
from typing import Any

from jose import JWTError, jwt

from app.core.config import get_settings
from app.core.errors import WorkspaceError

ISSUER = "dbtrunner"
AUDIENCE = "workspace"
SCOPE = "sql.jobs.run"
MAX_LIFETIME_SECONDS = 3 * 3600
JTI_PREFIX = "workspace:runner_job_jti:"


def _refuse(status: int, code: str, message: str) -> WorkspaceError:
    return WorkspaceError(status, code, message)


def decode_runner_job_token(token: str | None) -> dict[str, Any]:
    """The verified claims (signature, issuer, audience, expiry, scope, org, shape). Does not consume the jti."""
    s = get_settings()
    if not s.runner_job_jwt_secret:
        raise _refuse(503, "RUNNER_JOBS_NOT_CONFIGURED", "SQL jobs on a connection are not enabled on this Workspace")
    if not token:
        raise _refuse(401, "RUNNER_TOKEN_MISSING", "a run token is required")
    try:
        claims = jwt.decode(token, s.runner_job_jwt_secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
                            options={"require_exp": True, "require_iat": True, "require_iss": True, "require_aud": True, "leeway": 30})
    except JWTError:
        raise _refuse(401, "RUNNER_TOKEN_INVALID", "the run token is invalid or has expired") from None
    if claims.get("scope") != SCOPE:
        raise _refuse(403, "RUNNER_TOKEN_SCOPE", "the run token is not for running SQL jobs")
    if claims.get("org_id") != s.org_id:
        raise _refuse(403, "ORG_MISMATCH", "the run token belongs to another organization")
    if int(claims["exp"]) - int(claims["iat"]) > MAX_LIFETIME_SECONDS:
        raise _refuse(401, "RUNNER_TOKEN_INVALID", "the run token's lifetime is too long")
    for name in ("project_id", "run_id", "connection_id"):
        try:
            uuid.UUID(str(claims.get(name)))
        except ValueError:
            raise _refuse(401, "RUNNER_TOKEN_INVALID", f"the run token has no valid {name}") from None
    if not claims.get("jti"):
        raise _refuse(401, "RUNNER_TOKEN_INVALID", "the run token has no jti")
    return claims


async def consume_jti(claims: dict[str, Any]) -> None:
    """One call per token: a second presentation (a replay) is refused. Fails closed when the store is down."""
    import time

    from app.services.execution_router import get_redis

    ttl = max(int(claims["exp"]) - int(time.time()), 1) + 60
    try:
        fresh = await get_redis().set(f"{JTI_PREFIX}{claims['jti']}", "1", nx=True, ex=ttl)
    except Exception:  # noqa: BLE001
        raise _refuse(503, "REPLAY_STORE_UNAVAILABLE", "the run token could not be checked; try again") from None
    if not fresh:
        raise _refuse(401, "RUNNER_TOKEN_REPLAYED", "this run token was already used")
