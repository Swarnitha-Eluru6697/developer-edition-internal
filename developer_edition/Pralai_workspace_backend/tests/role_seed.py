"""TEST SEED ONLY: example roles -> Workspace permissions, so tests can describe a caller as "a data engineer".

The application holds no role names (contract 01-CONTRACT.md): roles and what they grant are data owned by the Admin Portal,
and the gateway hands the app only the caller's permissions (x-user-permissions). This table reproduces the former in-code
capability matrix in permission IDs, so the existing tests keep checking the same decisions. Nothing under app/ imports it.
"""
from __future__ import annotations

ADMIN, ANALYST, TRANSFORMER, STREAMING, ENGINEER, DQ_ENGINEER, STEWARD, AUDITOR = (
    "workspace_admin", "analyst", "data_transformer", "streaming_data_engineer", "data_engineer",
    "data_quality_engineer", "data_steward", "auditor")
ALL = (ADMIN, ANALYST, TRANSFORMER, STREAMING, ENGINEER, DQ_ENGINEER, STEWARD, AUDITOR)

# Every signed-in caller in the old model could open Workspace, manage their own files and open the Settings page.
BASE = ("workspace:access:view", "workspace:files:edit", "workspace:settings:edit")

_GRANTS: dict[str, tuple[str, ...]] = {
    "workspace:catalog:view": ALL,
    "workspace:datasets:view": ALL,
    "workspace:dq:view": ALL,
    "workspace:sql:run": (ADMIN, ANALYST, TRANSFORMER, STREAMING, ENGINEER, DQ_ENGINEER),
    "workspace:notebooks:run": (ADMIN, ANALYST, ENGINEER),
    "workspace:dbt:edit": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:dbt:run": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:dbt:deploy": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:sql-jobs:edit": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:sql-jobs:run": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:python-jobs:edit": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:python-jobs:run": (ADMIN, TRANSFORMER, ENGINEER),
    "workspace:flink:edit": (ADMIN, STREAMING, ENGINEER),
    "workspace:flink:deploy": (ADMIN, STREAMING, ENGINEER),
    "workspace:dq:create": (ADMIN, ENGINEER, DQ_ENGINEER),
    "workspace:dq:edit": (ADMIN, ENGINEER, DQ_ENGINEER),
    "workspace:dq:run": (ADMIN, ENGINEER, DQ_ENGINEER),
    "workspace:dq:publish": (ADMIN, ENGINEER, STEWARD),
    "workspace:dq:manage": (ADMIN,),
    "workspace:git:write": (ADMIN, TRANSFORMER, STREAMING, ENGINEER),
    "workspace:connections:manage": (ADMIN,),
    "workspace:settings:manage": (ADMIN,),
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    role: frozenset(BASE) | {p for p, roles in _GRANTS.items() if role in roles} for role in ALL
}
ROLE_PERMISSIONS["admin"] = ROLE_PERMISSIONS[ADMIN]   # the old back-compat alias


def permissions_for(roles) -> list[str]:
    """Union of the seed roles' permissions; any caller (even with no known role) gets BASE."""
    out: set[str] = set(BASE)
    for r in roles or ():
        out |= ROLE_PERMISSIONS.get(r, frozenset())
    return sorted(out)
