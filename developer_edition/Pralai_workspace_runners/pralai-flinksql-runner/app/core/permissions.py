"""Permissions for the FlinkSQL Runner (RUN-FLINK-009; contract 01-CONTRACT.md).

Scopes say what a TOKEN may do; permissions say what the CALLER may do. Both arrive in the token: a delegated token carries
the end user's Workspace permissions that its scope needs (`permissions` claim), an external-CI service-principal token the
permissions its principal was granted. Nothing is stored here and no role names are known here: roles, and what they grant,
live in the Admin Portal.

The IDs are Workspace's (Pralai_workspace_backend/permissions.json):

  workspace:flink:edit      create / update / delete / validate a job
  workspace:flink:deploy    deploy / stop / cancel / savepoint
  workspace:catalog:view    Kafka topic catalog, schemas, SQL templates
  workspace:datasets:view   Kafka topic preview
  workspace:dq:publish      create / update the quality job Data Quality generates (W-009). Used INSTEAD of flink:edit when
                            the call was issued by the Data Quality service on a user's behalf (token actor = DQ): DQ has
                            already required that permission, so its holders (not necessarily Flink editors) may have their
                            quality job created.

Reading jobs is open to any holder of flink:edit, flink:deploy or catalog:view. A token with none of them gets nothing.
Workspace's tests/test_rbac_contract.py fails if these tables and Workspace's ever diverge.

This module imports nothing from the app, so tooling can load it on its own.
"""
from __future__ import annotations

FLINK_EDIT = "workspace:flink:edit"
FLINK_DEPLOY = "workspace:flink:deploy"
CATALOG_VIEW = "workspace:catalog:view"
DATASETS_VIEW = "workspace:datasets:view"
DQ_PUBLISH = "workspace:dq:publish"

PERMISSIONS = frozenset({FLINK_EDIT, FLINK_DEPLOY, CATALOG_VIEW, DATASETS_VIEW, DQ_PUBLISH})

# X-001: what a token may do. Every user-facing route needs its scope AND one of its permissions.
SCOPE_JOBS_READ = "flink.jobs.read"        # list / get a job, its status, metrics, deployments
SCOPE_JOBS_EDIT = "flink.jobs.edit"        # create / update / delete / validate a job
SCOPE_JOBS_DEPLOY = "flink.jobs.deploy"    # deploy / stop / cancel / savepoint
SCOPE_CATALOG_READ = "flink.catalog.read"  # Kafka topic catalog, schemas, preview, SQL templates

# scope -> every permission that can open one of its routes (Workspace puts the ones the caller holds in the token).
SCOPE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    SCOPE_JOBS_READ: (FLINK_EDIT, FLINK_DEPLOY, CATALOG_VIEW),
    SCOPE_JOBS_EDIT: (FLINK_EDIT,),
    SCOPE_JOBS_DEPLOY: (FLINK_DEPLOY,),
    SCOPE_CATALOG_READ: (CATALOG_VIEW, DATASETS_VIEW),
}

# The permission a Flink-edit route needs when the Data Quality service is the actor.
DQ_ACTOR_SUBSTITUTE = {FLINK_EDIT: DQ_PUBLISH}


def has_permission(principal, permission: str) -> bool:
    """True if the principal holds `permission` exactly. No permissions = nothing."""
    perms = getattr(principal, "permissions", None)
    return bool(perms) and permission in perms
