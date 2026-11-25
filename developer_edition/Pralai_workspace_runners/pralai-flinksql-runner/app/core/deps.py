from __future__ import annotations
from typing import Annotated, AsyncIterator
from fastapi import Depends, Header, HTTPException, status
from jose import jwt
from jose.exceptions import JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.auth import Principal, validate_delegated_jwt, validate_jwt
from app.db import session as db_session


async def get_db() -> AsyncIterator[AsyncSession]:
    async with db_session.AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_current_user(authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "MISSING_TOKEN"})
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_TOKEN", "message": "Bearer token required"})
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


# RUN-FLINK-009: require_internal_token (unscoped, org-unbound plain
# equality against the static secret) was unused and has been removed —
# service callers authenticate via app.core.service_auth.require_service_scope.

CurrentUser = Annotated[Principal, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── permission checks (app/core/permissions.py) ─────────────────────────
from app.core.permissions import (  # noqa: E402
    CATALOG_VIEW, DATASETS_VIEW, DQ_ACTOR_SUBSTITUTE, FLINK_DEPLOY, FLINK_EDIT, SCOPE_CATALOG_READ, SCOPE_JOBS_DEPLOY,
    SCOPE_JOBS_EDIT, SCOPE_JOBS_READ, has_permission,
)


def _issued_by_dq(principal: Principal) -> bool:
    from app.core.config import get_settings
    actor = getattr(principal, "actor", None)
    return bool(actor) and actor == get_settings().dq_delegation_issuer


def effective_permissions_needed(principal: Principal, permissions: tuple[str, ...]) -> tuple[str, ...]:
    """The permissions this caller must hold one of: for a call the Data Quality service makes, flink:edit is replaced by
    the quality-job permission DQ has already checked (W-009)."""
    if _issued_by_dq(principal):
        return tuple(DQ_ACTOR_SUBSTITUTE.get(p, p) for p in permissions)
    return permissions


def require_permission(scope: str, *permissions: str):
    """The route needs exactly `scope` AND one of `permissions`, carried in the token."""
    async def _check(user: Annotated[Principal, Depends(get_current_user)]) -> Principal:
        if scope not in user.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "INSUFFICIENT_SCOPE", "message": f"token missing required scope '{scope}'"},
            )
        needed = effective_permissions_needed(user, permissions)
        if not any(has_permission(user, p) for p in needed):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail={"code": "FORBIDDEN", "message": f"missing permission {' or '.join(needed)}"})
        return user

    _check.required_scope = scope  # type: ignore[attr-defined]  # read by tests/test_delegated_auth.py (every route needs one)
    _check.required_permissions = permissions  # type: ignore[attr-defined]
    return _check


RequireJobRead = Annotated[Principal, Depends(require_permission(SCOPE_JOBS_READ, FLINK_EDIT, FLINK_DEPLOY, CATALOG_VIEW))]
RequireCatalogBrowse = Annotated[Principal, Depends(require_permission(SCOPE_CATALOG_READ, CATALOG_VIEW))]
RequireDatasetPreview = Annotated[Principal, Depends(require_permission(SCOPE_CATALOG_READ, DATASETS_VIEW))]
RequireFlinkEdit = Annotated[Principal, Depends(require_permission(SCOPE_JOBS_EDIT, FLINK_EDIT))]
RequireFlinkDeploy = Annotated[Principal, Depends(require_permission(SCOPE_JOBS_DEPLOY, FLINK_DEPLOY))]
