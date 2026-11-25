"""Workspaces API: CRUD on the user's personal workspaces."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status

from app.core.deps import CurrentUser, DB
from app.core.rbac import RequireFilesEdit
from app.schemas.workspace import WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(user: CurrentUser, db: DB) -> list[WorkspaceOut]:
    svc = WorkspaceService(db)
    user_row = await svc.ensure_user(user)
    workspaces = await svc.list_for_user(user_row)
    return [WorkspaceOut.model_validate(w) for w in workspaces]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate, user: RequireFilesEdit, db: DB, request: Request,
) -> WorkspaceOut:
    svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await svc.ensure_user(user)
    ws = await svc.create(user_row, payload)
    await audit.record(
        user_id=user_row.id,
        action="workspace.create",
        resource_type="workspace",
        resource_id=ws.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"name": ws.name},
    )
    return WorkspaceOut.model_validate(ws)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(workspace_id: UUID, user: CurrentUser, db: DB) -> WorkspaceOut:
    svc = WorkspaceService(db)
    user_row = await svc.ensure_user(user)
    ws = await svc.get(user_row, workspace_id)
    return WorkspaceOut.model_validate(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: UUID, payload: WorkspaceUpdate, user: RequireFilesEdit, db: DB, request: Request,
) -> WorkspaceOut:
    svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await svc.ensure_user(user)
    ws = await svc.update(user_row, workspace_id, payload)
    await audit.record(
        user_id=user_row.id,
        action="workspace.update",
        resource_type="workspace",
        resource_id=ws.id,
        ip_address=request.client.host if request.client else None,
        metadata=payload.model_dump(exclude_none=True),
    )
    return WorkspaceOut.model_validate(ws)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: UUID, user: RequireFilesEdit, db: DB, request: Request,
) -> None:
    svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await svc.ensure_user(user)
    await svc.delete(user_row, workspace_id)
    await audit.record(
        user_id=user_row.id,
        action="workspace.delete",
        resource_type="workspace",
        resource_id=workspace_id,
        ip_address=request.client.host if request.client else None,
    )
