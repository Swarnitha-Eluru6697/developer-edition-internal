"""Permission-level authorization for the dbt Runner (DBT-001; contract 01-CONTRACT.md).

Scopes (app/core/deps.py) say what a TOKEN may do; permissions say what the CALLER may do. Both arrive in the token: a
delegated Workspace token carries the end user's Workspace permissions that its scope needs (`permissions` claim), an
external-CI service-principal token carries the permissions its principal was granted. Nothing is stored here and no role
names are known here: roles, and what they grant, live in the Admin Portal.

The IDs are Workspace's (Pralai_workspace_backend/permissions.json). Each job kind the runner keeps has its own pair:

  dbt      workspace:dbt:edit          / workspace:dbt:run
  sql      workspace:sql-jobs:edit     / workspace:sql-jobs:run
  python   workspace:python-jobs:edit  / workspace:python-jobs:run

  edit - register / read / update / delete a job, read its lineage
  run  - start / cancel a run, read run history and events (reading a job is also allowed with run)

A token with none of them gets nothing. Workspace's tests/test_rbac_contract.py fails if these tables and Workspace's
(permissions.json, app/core/service_tokens.py SCOPE_PERMISSIONS) ever diverge.

This module imports nothing from the app, so tooling can load it on its own.
"""
from __future__ import annotations

JOB_KIND_EDIT = {"dbt": "workspace:dbt:edit", "sql": "workspace:sql-jobs:edit", "python": "workspace:python-jobs:edit"}
JOB_KIND_RUN = {"dbt": "workspace:dbt:run", "sql": "workspace:sql-jobs:run", "python": "workspace:python-jobs:run"}
JOB_KINDS = tuple(JOB_KIND_EDIT)

EDIT_PERMISSIONS = tuple(JOB_KIND_EDIT.values())
RUN_PERMISSIONS = tuple(JOB_KIND_RUN.values())
PERMISSIONS = frozenset(EDIT_PERMISSIONS + RUN_PERMISSIONS)

# What each user-facing scope's routes accept, per job kind ("edit" / "run" / both).
SCOPE_ACTIONS: dict[str, tuple[str, ...]] = {
    "dbt.projects.read": ("edit", "run"),
    "dbt.projects.write": ("edit",),
    "dbt.runs.read": ("run",),
    "dbt.runs.trigger": ("run",),
}
_BY_ACTION = {"edit": JOB_KIND_EDIT, "run": JOB_KIND_RUN}

# scope -> every permission that can open one of its routes (for some job kind). Workspace puts exactly these (the ones the
# caller holds) in a delegated token for that scope.
SCOPE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    scope: tuple(_BY_ACTION[a][k] for a in actions for k in JOB_KINDS) for scope, actions in SCOPE_ACTIONS.items()
}


def has_permission(principal, permission: str) -> bool:
    """True if the principal holds `permission` exactly. No permissions = nothing."""
    perms = getattr(principal, "permissions", None)
    return bool(perms) and permission in perms


def permissions_for(scope: str, kind: str) -> tuple[str, ...]:
    """The permissions that allow `scope`'s routes on a job of `kind`."""
    return tuple(_BY_ACTION[a][kind] for a in SCOPE_ACTIONS[scope])


def may(principal, scope: str, kind: str | None = None) -> bool:
    """Whether the caller may use `scope`'s routes on a job of `kind` (any kind when None)."""
    allowed = SCOPE_PERMISSIONS[scope] if kind is None else permissions_for(scope, kind if kind in JOB_KINDS else "dbt")
    return any(has_permission(principal, p) for p in allowed)


def kinds_allowed(principal, scope: str) -> list[str]:
    """The job kinds the caller may use `scope`'s routes on."""
    return [k for k in JOB_KINDS if may(principal, scope, k)]
