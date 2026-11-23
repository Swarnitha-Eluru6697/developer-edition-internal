"""FastAPI dependencies."""
from __future__ import annotations

from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, validate_jwt
from app.db.session import AsyncSessionLocal


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_current_user(authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "MISSING_TOKEN", "message": "No JWT provided"},
                            headers={"WWW-Authenticate": "Bearer"})
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_AUTH_HEADER", "message": "Expected 'Bearer <token>'"})
    return await validate_jwt(token)


def bearer_from_header(authorization: str | None) -> str | None:
    """The caller's own bearer, for user-delegated runner calls (job lookups
    and Flink job creation run under the caller's org and RBAC)."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip() or None
    return None


def _perm(name: str):
    from app.core.permissions import require_permission
    return Depends(require_permission(name))


def _validate_caller():
    from app.core.service_auth import SCOPE_DQ_RULES_VALIDATE, require_inbound_service_scope
    return Depends(require_inbound_service_scope(SCOPE_DQ_RULES_VALIDATE))


CurrentUser = Annotated[Principal, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
Reader = Annotated[Principal, _perm("dq.read")]
RulesWriter = Annotated[Principal, _perm("dq.rules.write")]
Binder = Annotated[Principal, _perm("dq.bind")]
Applier = Annotated[Principal, _perm("dq.apply")]
Syncer = Annotated[Principal, _perm("dq.sync")]
ValidateCaller = Annotated[dict | None, _validate_caller()]
