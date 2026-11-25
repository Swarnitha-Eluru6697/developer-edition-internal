"""FastAPI dependencies.

Two auth paths, deliberately different in scope:
  - End-user JWT (forwarded by Workspace, or sent directly by an external
    CI system per TDD 4.3.3) — used for everything that registers, runs,
    or reads a project. Full Principal, same validation as Workspace.
  - Internal service token — used ONLY by the narrow POST /api/v1/validate
    endpoint that Workspace's codegen chat calls during the
    generate -> validate -> fix loop. Cannot register, enumerate or run a
    project, so a leaked token has a small blast radius (DBT-003: it used to
    open the /internal scheduler routes as well; those now need Dagster's own
    scoped JWT, see app/core/service_auth.py require_scheduler_scope).
"""
from __future__ import annotations

import hmac
from typing import Annotated, AsyncIterator

from fastapi import Header, HTTPException, status
from fastapi import Depends
from jose import jwt
from jose.exceptions import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, validate_delegated_jwt, validate_jwt
from app.core.config import get_settings
from app.core.permissions import SCOPE_ACTIONS, kinds_allowed, may, permissions_for
from app.db.session import AsyncSessionLocal


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_TOKEN", "message": "No JWT provided"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_AUTH_HEADER", "message": "Expected 'Bearer <token>'"},
        )
    # Two trust domains, told apart by how the token is signed (never by what it claims): an HS256
    # delegation from Workspace, or an RS256 control-plane service-principal token (external CI).
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_TOKEN", "message": str(e)})
    if alg == "HS256":
        return await validate_delegated_jwt(token)
    return await validate_jwt(token)


def require_scope(scope: str):
    """A user-facing route demands exactly this scope AND that the token carries a permission that opens it for at least
    one job kind, whichever trust domain the token came from (X-001 scopes, DBT-001 permissions). The route then checks the
    permission for the job's own kind (`ensure_job_permission`)."""
    if scope not in SCOPE_ACTIONS:
        raise ValueError(f"Unknown scope: {scope!r}")

    async def _check(user: Annotated[Principal, Depends(get_current_user)]) -> Principal:
        if scope not in user.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "INSUFFICIENT_SCOPE", "message": f"token missing required scope '{scope}'"},
            )
        if not may(user, scope):
            raise _forbidden(scope, None)
        return user
    # Read by tests/test_delegated_auth.py and tests/test_dbt_rbac.py (every route needs one).
    _check.required_scope = scope  # type: ignore[attr-defined]
    return _check


def _forbidden(scope: str, kind: str | None) -> HTTPException:
    needed = permissions_for(scope, kind) if kind else sorted({p for k in ("dbt", "sql", "python")
                                                               for p in permissions_for(scope, k)})
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                         detail={"code": "FORBIDDEN", "message": f"missing permission {' or '.join(needed)}"})


def ensure_job_permission(user: Principal, scope: str, kind: str | None) -> None:
    """403 unless the caller holds the permission `scope` needs for a job of this kind (dbt / sql / python)."""
    if not may(user, scope, kind or "dbt"):
        raise _forbidden(scope, kind or "dbt")


def allowed_kinds(user: Principal, scope: str) -> list[str]:
    return kinds_allowed(user, scope)


async def require_internal_service_token(
    x_internal_service_token: Annotated[str | None, Header()] = None,
) -> None:
    settings = get_settings()
    if not settings.internal_service_token or not x_internal_service_token or not hmac.compare_digest(
            x_internal_service_token.encode(), settings.internal_service_token.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_SERVICE_TOKEN", "message": "Missing or invalid internal service token"},
        )


# X-001: what a token may do on the user-facing routes.
SCOPE_PROJECTS_READ = "dbt.projects.read"      # list / get a project, its lineage
SCOPE_PROJECTS_WRITE = "dbt.projects.write"    # register / update / delete a project
SCOPE_RUNS_READ = "dbt.runs.read"              # list / get runs and their events
SCOPE_RUNS_TRIGGER = "dbt.runs.trigger"        # start / cancel a run

CurrentUser = Annotated[Principal, Depends(get_current_user)]
# DBT-001: each scope also needs a permission (app/core/permissions.py); the job's kind decides which one.
ProjectsRead = Annotated[Principal, Depends(require_scope(SCOPE_PROJECTS_READ))]
ProjectsWrite = Annotated[Principal, Depends(require_scope(SCOPE_PROJECTS_WRITE))]
RunsRead = Annotated[Principal, Depends(require_scope(SCOPE_RUNS_READ))]
RunsTrigger = Annotated[Principal, Depends(require_scope(SCOPE_RUNS_TRIGGER))]
DB = Annotated[AsyncSession, Depends(get_db)]
InternalCaller = Annotated[None, Depends(require_internal_service_token)]
