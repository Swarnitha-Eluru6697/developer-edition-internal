"""Files API: file metadata + content within a workspace."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from app.core.deps import CurrentUser, DB
from app.core.rbac import RequireFilesEdit
from app.schemas.file import (
    FileCreate, FileMetadata, FileTreeNode, FileUpdate, FileWithContent,
)
from app.services.audit_service import AuditService
from app.services.file_service import FileService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/files", tags=["files"])


@router.get("/tree", response_model=FileTreeNode)
async def file_tree(
    workspace_id: UUID,
    user: CurrentUser,
    db: DB,
    branch: str = Query("main"),
) -> FileTreeNode:
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    file_svc = FileService(db, user.org_id)
    return await file_svc.build_tree(ws, branch=branch)


@router.get("", response_model=list[FileMetadata])
async def list_files(
    workspace_id: UUID,
    user: CurrentUser,
    db: DB,
    branch: str = Query("main"),
    language: str | None = Query(None),
) -> list[FileMetadata]:
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    file_svc = FileService(db, user.org_id)
    files = await file_svc.list_files(ws, branch=branch, language=language)
    return [FileMetadata.model_validate(f) for f in files]


@router.post("", response_model=FileWithContent, status_code=status.HTTP_201_CREATED)
async def create_file(
    workspace_id: UUID,
    payload: FileCreate,
    user: RequireFilesEdit,
    db: DB,
    request: Request,
) -> FileWithContent:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    file_svc = FileService(db, user.org_id)
    row, content = await file_svc.create(ws, payload)
    await audit.record(
        user_id=user_row.id,
        action="file.create",
        resource_type="file",
        resource_id=row.id,
        ip_address=request.client.host if request.client else None,
        metadata={"path": row.path, "language": row.language},
    )
    return FileWithContent(
        **FileMetadata.model_validate(row).model_dump(),
        content=content,
    )


@router.get("/{file_id}", response_model=FileWithContent)
async def get_file(
    workspace_id: UUID, file_id: UUID, user: CurrentUser, db: DB,
) -> FileWithContent:
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    file_svc = FileService(db, user.org_id)
    row, content = await file_svc.get_with_content(ws, file_id)
    return FileWithContent(
        **FileMetadata.model_validate(row).model_dump(),
        content=content,
    )


@router.put("/{file_id}", response_model=FileMetadata)
async def update_file(
    workspace_id: UUID,
    file_id: UUID,
    payload: FileUpdate,
    user: RequireFilesEdit,
    db: DB,
    request: Request,
) -> FileMetadata:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    file_svc = FileService(db, user.org_id)
    row = await file_svc.update(ws, file_id, payload)
    await audit.record(
        user_id=user_row.id,
        action="file.update",
        resource_type="file",
        resource_id=row.id,
        ip_address=request.client.host if request.client else None,
        metadata={"path": row.path, "size_bytes": row.size_bytes},
    )
    return FileMetadata.model_validate(row)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    workspace_id: UUID, file_id: UUID, user: RequireFilesEdit, db: DB, request: Request,
) -> None:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    file_svc = FileService(db, user.org_id)
    await file_svc.delete(ws, file_id)
    await audit.record(
        user_id=user_row.id,
        action="file.delete",
        resource_type="file",
        resource_id=file_id,
        ip_address=request.client.host if request.client else None,
    )
