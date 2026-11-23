"""DQ permissions.

Vocabulary:
  dq.read              read rules/rulesets/bindings/artifacts/capabilities
  dq.rules.write       create/update/delete ordinary rules and rulesets
  dq.custom_sql.write  create/update/approve PRIVILEGED custom_sql rules (DQ-SEC-006)
  dq.bind              bind a ruleset to a job + dataset (DQ-BIND-003; UCS must
                       additionally authorise its own dq.bind action on the dataset)
  dq.apply             push a compiled artifact to a runner
  dq.sync              register checks with UCS

A principal's effective permissions are the union of explicit `permissions`
/ `scope` claims (only `dq.*` entries are honoured) and the permissions of
its roles under settings.role_permissions.
"""
# (no `from __future__ import annotations`: the dependency closure below must
# keep real, eagerly-evaluated annotations for FastAPI)

from typing import Annotated, Any

from fastapi import Depends, HTTPException, status

from app.core.auth import Principal
from app.core.config import Settings, get_settings

ALL_PERMISSIONS = frozenset({
    "dq.read", "dq.rules.write", "dq.custom_sql.write", "dq.bind", "dq.apply", "dq.sync",
})


def resolve_permissions(roles: list[str], claims: dict[str, Any] | None = None,
                        settings: Settings | None = None) -> frozenset[str]:
    settings = settings or get_settings()
    perms: set[str] = set()
    for role in roles or []:
        perms.update(settings.role_permissions.get(role, []))
    claims = claims or {}
    explicit = list(claims.get("permissions") or [])
    explicit += str(claims.get("scope") or "").split()
    perms.update(p for p in explicit if p in ALL_PERMISSIONS)
    return frozenset(perms & ALL_PERMISSIONS)


def effective_permissions(principal: Principal) -> frozenset[str]:
    return frozenset(principal.permissions) | resolve_permissions(principal.roles)


def has_permission(principal: Principal, permission: str) -> bool:
    return permission in effective_permissions(principal)


def ensure_permission(principal: Principal, permission: str) -> None:
    if not has_permission(principal, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN",
                    "message": f"permission '{permission}' is required for this operation",
                    "details": {"required_permission": permission}})


def require_permission(permission: str):
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")

    from app.core.deps import get_current_user

    async def _check(user: Annotated[Principal, Depends(get_current_user)]) -> Principal:
        ensure_permission(user, permission)
        return user

    return _check
