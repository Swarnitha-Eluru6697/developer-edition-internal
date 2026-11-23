"""Workspace authorisation by PERMISSION (contract 01-CONTRACT.md, docs/auth/).

No role names live here or anywhere else in this service. Roles are data owned by the Admin Portal: it maps roles to
permissions, Keycloak puts the user's permissions in the Data Plane ticket, and Envoy hands them to us in
`x-user-permissions` (read by the gateway-auth library into `Principal.permissions`, see app/core/deps.py). A new role, or a
change to what a role may do, needs no change and no redeploy here.

The permission IDs (`workspace:<area>:<action>`) are declared in ONE place: `permissions.json` at the repo root, which the
Admin Portal imports. This module loads it, so a route can only ask for a permission that is published there
(`require_permission` raises at import time otherwise).

`DOWNSTREAM` says how each permission is enforced again at a service that checks its own vocabulary (defence in depth). It is
exported, with the IDs, as the platform contract `contracts/rbac.contract.v1.json` (scripts/export_rbac_contract.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from app.core.auth import Principal
from app.core.data_policy import apply_route_policy
from app.core.deps import DB, get_current_user
from app.core.errors import forbidden

PERMISSIONS_FILE = Path(__file__).resolve().parents[2] / "permissions.json"
CONTRACT_VERSION = "2.0.0"


def _load_permissions() -> tuple[str, ...]:
    doc = json.loads(PERMISSIONS_FILE.read_text())
    if doc.get("app") != "workspace":
        raise RuntimeError(f"{PERMISSIONS_FILE} is not the workspace permission list")
    return tuple(p["id"] for p in doc["permissions"])


PERMISSIONS: tuple[str, ...] = _load_permissions()

ACCESS_VIEW = "workspace:access:view"
FILES_EDIT = "workspace:files:edit"
CATALOG_VIEW = "workspace:catalog:view"
DATASETS_VIEW = "workspace:datasets:view"
SQL_RUN = "workspace:sql:run"
NOTEBOOKS_RUN = "workspace:notebooks:run"
DBT_EDIT = "workspace:dbt:edit"
DBT_RUN = "workspace:dbt:run"
DBT_DEPLOY = "workspace:dbt:deploy"
SQL_JOBS_EDIT = "workspace:sql-jobs:edit"
SQL_JOBS_RUN = "workspace:sql-jobs:run"
PYTHON_JOBS_EDIT = "workspace:python-jobs:edit"
PYTHON_JOBS_RUN = "workspace:python-jobs:run"
FLINK_EDIT = "workspace:flink:edit"
FLINK_DEPLOY = "workspace:flink:deploy"
DQ_VIEW = "workspace:dq:view"
DQ_CREATE = "workspace:dq:create"
DQ_EDIT = "workspace:dq:edit"
DQ_RUN = "workspace:dq:run"
DQ_PUBLISH = "workspace:dq:publish"
DQ_MANAGE = "workspace:dq:manage"
GIT_WRITE = "workspace:git:write"
CONNECTIONS_MANAGE = "workspace:connections:manage"
SETTINGS_EDIT = "workspace:settings:edit"        # the ordinary Settings page
SETTINGS_MANAGE = "workspace:settings:manage"    # workspace administration (former admin-only checks)
ALERTS_VIEW = "workspace:alerts:view"
ALERTS_EDIT = "workspace:alerts:edit"            # acknowledge / resolve
ALERTS_MANAGE = "workspace:alerts:manage"        # rules and channels

# The dbt runner keeps dbt projects, SQL jobs and Python jobs; each kind has its own edit / run permission.
JOB_KIND_EDIT = {"dbt": DBT_EDIT, "sql": SQL_JOBS_EDIT, "python": PYTHON_JOBS_EDIT}
JOB_KIND_RUN = {"dbt": DBT_RUN, "sql": SQL_JOBS_RUN, "python": PYTHON_JOBS_RUN}
JOB_EDIT_PERMISSIONS = tuple(JOB_KIND_EDIT.values())
JOB_RUN_PERMISSIONS = tuple(JOB_KIND_RUN.values())
JOB_VIEW_PERMISSIONS = JOB_EDIT_PERMISSIONS + JOB_RUN_PERMISSIONS

# How each permission is enforced again downstream (each service checks its own vocabulary):
#   dataquality      -> the Data Quality service's `dq.*` permissions (InaiEra_dataquality_backend app/core/permissions.py);
#                       a delegated token's scope caps what DQ allows.
#   dbtrunner        -> the dbt runner checks the same permission ID, carried in the delegated token's `permissions` claim.
#   flinksql-runner  -> the FlinkSQL runner checks the same permission ID, carried the same way.
DOWNSTREAM: dict[str, dict[str, tuple[str, ...]]] = {
    DQ_VIEW: {"dataquality": ("dq.read",)},
    DQ_CREATE: {"dataquality": ("dq.rules.write",)},
    DQ_EDIT: {"dataquality": ("dq.rules.write",)},
    DQ_RUN: {"dataquality": ("dq.read",)},
    DQ_PUBLISH: {"dataquality": ("dq.bind", "dq.apply", "dq.sync"), "flinksql-runner": (DQ_PUBLISH,)},
    DQ_MANAGE: {"dataquality": ("dq.custom_sql.write",)},
    **{p: {"dbtrunner": (p,)} for p in JOB_VIEW_PERMISSIONS},
    FLINK_EDIT: {"flinksql-runner": (FLINK_EDIT,)},
    FLINK_DEPLOY: {"flinksql-runner": (FLINK_DEPLOY,)},
    CATALOG_VIEW: {"flinksql-runner": (CATALOG_VIEW,)},
    DATASETS_VIEW: {"flinksql-runner": (DATASETS_VIEW,)},
}


def _known(permission: str) -> str:
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown permission {permission!r}: it is not published in permissions.json")
    return permission


for _p in (*[v for k, v in dict(globals()).items() if k.isupper() and isinstance(v, str) and v.startswith("workspace:")],
           *DOWNSTREAM):
    _known(_p)


def has_permission(principal: Principal, permission: str) -> bool:
    """Whether the principal holds `permission` exactly. No permissions = nothing (least privilege)."""
    return principal.has(permission)


def effective_permissions(principal: Principal) -> list[str]:
    """This app's permissions the principal holds, sorted (what /auth/me reports and the UI gates on)."""
    return sorted(p for p in principal.permissions if p in PERMISSIONS)


def rbac_contract() -> dict:
    """The platform permission contract as a plain document (exported to contracts/rbac.contract.v1.json). It lists the
    permission IDs and where each is enforced downstream; it holds no roles (they live in the Admin Portal). Deterministic:
    sorted, no timestamps, so the committed file can be diffed."""
    return {
        "contract": "inaiera.permissions",
        "contract_version": CONTRACT_VERSION,
        "app": "workspace",
        "source": "permissions.json",
        "permissions": {
            p: {"downstream": {svc: list(perms) for svc, perms in sorted(DOWNSTREAM.get(p, {}).items())}}
            for p in sorted(PERMISSIONS)
        },
    }


def _missing(permissions: tuple[str, ...]):
    return forbidden(f"missing permission {' or '.join(permissions)}")


def require_any_permission(*permissions: str):
    """FastAPI dependency factory: 403 unless the caller holds at least ONE of `permissions`; then the route's data policy
    (UCS) runs. Use as a drop-in replacement for the `user: CurrentUser` parameter:

        async def deploy_job(job_id: UUID, user: RequireFlinkDeploy, ...):
    """
    for p in permissions:
        _known(p)

    async def _check(request: Request, user: Annotated[Principal, Depends(get_current_user)], db: DB) -> Principal:
        if not any(user.has(p) for p in permissions):
            raise _missing(permissions)
        # Product gate passed; now the resource/data policy gate (X-006): UCS decides on this route's action.
        await apply_route_policy(request, user, db)
        return user

    _check.required_permissions = permissions  # type: ignore[attr-defined]  # read by tests
    return _check


def require_permission(permission: str):
    """FastAPI dependency factory: 403 unless the caller holds `permission`."""
    return require_any_permission(permission)


def ensure_permission(principal: Principal, permission: str) -> None:
    """In-route check, for a decision that depends on the request (e.g. a job's kind)."""
    if not principal.has(_known(permission)):
        raise _missing((permission,))


# Named aliases, one per permission with a real endpoint — mirrors the CurrentUser/DB alias convention in app/core/deps.py.
RequireFilesEdit = Annotated[Principal, Depends(require_permission(FILES_EDIT))]
RequireCatalogBrowse = Annotated[Principal, Depends(require_permission(CATALOG_VIEW))]
RequireDatasetPreview = Annotated[Principal, Depends(require_permission(DATASETS_VIEW))]
RequireSqlExecute = Annotated[Principal, Depends(require_permission(SQL_RUN))]
RequireNotebook = Annotated[Principal, Depends(require_permission(NOTEBOOKS_RUN))]
RequireDbtEdit = Annotated[Principal, Depends(require_permission(DBT_EDIT))]
RequireDbtExecute = Annotated[Principal, Depends(require_permission(DBT_RUN))]
RequireDbtProductionDeploy = Annotated[Principal, Depends(require_permission(DBT_DEPLOY))]
# dbt-runner jobs of any kind: which kind the caller may touch is decided per job (register here; by id in the runner).
RequireJobView = Annotated[Principal, Depends(require_any_permission(*JOB_VIEW_PERMISSIONS))]
RequireJobEdit = Annotated[Principal, Depends(require_any_permission(*JOB_EDIT_PERMISSIONS))]
RequireJobRun = Annotated[Principal, Depends(require_any_permission(*JOB_RUN_PERMISSIONS))]
RequireFlinkEdit = Annotated[Principal, Depends(require_permission(FLINK_EDIT))]
RequireFlinkDeploy = Annotated[Principal, Depends(require_permission(FLINK_DEPLOY))]
RequireDqRead = Annotated[Principal, Depends(require_permission(DQ_VIEW))]
RequireDqRuleCreate = Annotated[Principal, Depends(require_permission(DQ_CREATE))]
RequireDqRuleEdit = Annotated[Principal, Depends(require_permission(DQ_EDIT))]
RequireDqExecute = Annotated[Principal, Depends(require_permission(DQ_RUN))]
RequireDqPublish = Annotated[Principal, Depends(require_permission(DQ_PUBLISH))]
RequireDqAdmin = Annotated[Principal, Depends(require_permission(DQ_MANAGE))]
RequireGitWrite = Annotated[Principal, Depends(require_permission(GIT_WRITE))]
RequireConnectionsManage = Annotated[Principal, Depends(require_permission(CONNECTIONS_MANAGE))]
RequireSettingsEdit = Annotated[Principal, Depends(require_permission(SETTINGS_EDIT))]
RequireSettingsManage = Annotated[Principal, Depends(require_permission(SETTINGS_MANAGE))]
RequireAlertsRead = Annotated[Principal, Depends(require_permission(ALERTS_VIEW))]
RequireAlertsAck = Annotated[Principal, Depends(require_permission(ALERTS_EDIT))]
RequireAlertsManage = Annotated[Principal, Depends(require_permission(ALERTS_MANAGE))]
