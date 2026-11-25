"""File service.

Files have:
  - Metadata in PostgreSQL (workspace_files table)
  - Content in cloud object storage at storage_key_for_file(...)

This service is the only place that touches storage.get_blob_store() for files.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import conflict, flush_or_conflict, not_found, validation_failed
from app.models import Workspace, WorkspaceFile
from app.schemas.file import FileCreate, FileTreeNode, FileUpdate
from app.storage import get_blob_store, storage_key_for_file


# Phase 1 enforces these. Add to ALLOWED in later phases.
ALLOWED_LANGUAGES_PHASE_1 = {"sql", "flinksql", "ipynb", "py", "md", "yaml", "json", "other"}


class FileService:
    def __init__(self, db: AsyncSession, org_id: str):
        self._db = db
        self._org_id = org_id
        self._storage = get_blob_store()

    def _key(self, workspace: Workspace, branch: str, path: str) -> str:
        return storage_key_for_file(
            org_id=self._org_id,
            user_id=str(workspace.owner_id),
            workspace_id=str(workspace.id),
            branch=branch,
            path=path,
        )

    async def list_files(
        self,
        workspace: Workspace,
        branch: str = "main",
        language: str | None = None,
    ) -> list[WorkspaceFile]:
        stmt = select(WorkspaceFile).where(
            WorkspaceFile.workspace_id == workspace.id,
            WorkspaceFile.branch == branch,
            WorkspaceFile.is_deleted.is_(False),
        )
        if language:
            stmt = stmt.where(WorkspaceFile.language == language)
        result = await self._db.execute(stmt.order_by(WorkspaceFile.path))
        return list(result.scalars())

    async def find_by_path(
        self, workspace: Workspace, path: str, branch: str = "main",
    ) -> WorkspaceFile | None:
        """Used by codegen 'apply' to decide create vs. update for a
        generated artifact's target path."""
        norm_path = self._normalize_path(path)
        result = await self._db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == workspace.id,
                WorkspaceFile.path == norm_path,
                WorkspaceFile.branch == branch,
                WorkspaceFile.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def build_tree(
        self, workspace: Workspace, branch: str = "main",
    ) -> FileTreeNode:
        files = await self.list_files(workspace, branch=branch)
        root = FileTreeNode(name="", path="", type="directory")
        # Sort to ensure directories come before files
        for f in sorted(files, key=lambda x: x.path):
            parts = f.path.strip("/").split("/")
            cur = root
            for i, part in enumerate(parts):
                is_last = i == len(parts) - 1
                child = next((c for c in cur.children if c.name == part), None)
                if not child:
                    if is_last:
                        child = FileTreeNode(
                            name=part,
                            path=f.path,
                            type="file",
                            id=str(f.id),
                            language=f.language,
                            size_bytes=f.size_bytes,
                            is_dirty=f.is_dirty,
                        )
                    else:
                        child = FileTreeNode(
                            name=part,
                            path="/".join(parts[: i + 1]),
                            type="directory",
                        )
                    cur.children.append(child)
                cur = child
        return root

    async def create(
        self, workspace: Workspace, request: FileCreate,
    ) -> tuple[WorkspaceFile, str]:
        settings = get_settings()
        if request.language not in ALLOWED_LANGUAGES_PHASE_1:
            raise validation_failed(
                "language",
                f"Language '{request.language}' not enabled in Phase 1",
            )
        content_bytes = request.content.encode("utf-8")
        if len(content_bytes) > settings.max_file_size_bytes:
            raise validation_failed("content", "File too large")
        # Validate path
        norm_path = self._normalize_path(request.path)
        # Look up ANY existing row at this path. The unique key
        # (workspace_id, path, branch) ignores is_deleted, so a soft-deleted
        # row still occupies the key — we must revive it rather than insert a
        # duplicate (which would raise a 500 IntegrityError).
        existing = (await self._db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == workspace.id,
                WorkspaceFile.path == norm_path,
                WorkspaceFile.branch == request.branch,
            )
        )).scalar_one_or_none()
        if existing and not existing.is_deleted:
            raise conflict(f"File already exists: {norm_path}", code="FILE_PATH_TAKEN", resource="file", field="path")
        # For .ipynb, validate it's actual JSON
        if request.language == "ipynb":
            self._validate_ipynb(request.content)
        # Write to storage
        key = self._key(workspace, request.branch, norm_path)
        sha256 = await self._storage.put(key, content_bytes, content_type=self._mime(request.language))
        if existing:  # soft-deleted row — revive it in place (key already taken)
            existing.is_deleted = False
            existing.language = request.language
            existing.size_bytes = len(content_bytes)
            existing.sha256 = sha256
            existing.is_dirty = True
            existing.last_committed_sha = None
            await self._db.flush()
            # Refresh so server-side onupdate columns (updated_at) are loaded in
            # the async context — otherwise serializing the row lazy-loads and
            # raises MissingGreenlet.
            await self._db.refresh(existing)
            return existing, request.content
        # Write metadata (new row)
        file_row = WorkspaceFile(
            workspace_id=workspace.id,
            path=norm_path,
            language=request.language,
            branch=request.branch,
            size_bytes=len(content_bytes),
            sha256=sha256,
            is_dirty=True,
        )
        self._db.add(file_row)
        await flush_or_conflict(self._db, f"File already exists: {norm_path}", constraint="uq_file_path_branch")
        return file_row, request.content

    async def get_with_content(
        self, workspace: Workspace, file_id: uuid.UUID,
    ) -> tuple[WorkspaceFile, str]:
        row = await self._get(workspace, file_id)
        key = self._key(workspace, row.branch, row.path)
        content_bytes = await self._storage.get(key)
        return row, content_bytes.decode("utf-8")

    async def update(
        self, workspace: Workspace, file_id: uuid.UUID, request: FileUpdate,
    ) -> WorkspaceFile:
        settings = get_settings()
        row = await self._get(workspace, file_id)
        content_bytes = request.content.encode("utf-8")
        if len(content_bytes) > settings.max_file_size_bytes:
            raise validation_failed("content", "File too large")
        if row.language == "ipynb":
            self._validate_ipynb(request.content)
        if request.if_match_sha256 and request.if_match_sha256 != row.sha256:
            raise conflict("File content changed since last fetch", code="FILE_VERSION_CONFLICT", resource="file", field="sha256")
        key = self._key(workspace, row.branch, row.path)
        sha256 = await self._storage.put(key, content_bytes, content_type=self._mime(row.language))
        row.size_bytes = len(content_bytes)
        row.sha256 = sha256
        row.is_dirty = True
        row.updated_at = datetime.now(timezone.utc)
        await self._db.flush()
        return row

    async def delete(self, workspace: Workspace, file_id: uuid.UUID) -> None:
        row = await self._get(workspace, file_id)
        # Soft delete in metadata; keep blob until next git commit/cleanup
        row.is_deleted = True
        row.is_dirty = True
        await self._db.flush()

    async def _get(self, workspace: Workspace, file_id: uuid.UUID) -> WorkspaceFile:
        row = await self._db.get(WorkspaceFile, file_id)
        if not row or row.workspace_id != workspace.id or row.is_deleted:
            raise not_found("file", str(file_id))
        return row

    @staticmethod
    def _normalize_path(path: str) -> str:
        # Strip leading/trailing slashes; reject ..
        norm = path.strip().lstrip("/")
        if ".." in norm.split("/"):
            raise validation_failed("path", "Path may not contain '..'")
        return norm

    @staticmethod
    def _mime(language: str) -> str:
        return {
            "sql": "application/sql",
            "flinksql": "application/sql",
            "ipynb": "application/x-ipynb+json",
            "py": "text/x-python",
            "md": "text/markdown",
            "yaml": "application/yaml",
            "json": "application/json",
        }.get(language, "text/plain")

    @staticmethod
    def _validate_ipynb(content: str) -> None:
        import json
        try:
            doc = json.loads(content)
        except json.JSONDecodeError as e:
            raise validation_failed("content", f"Invalid notebook JSON: {e}")
        # Minimal nbformat check: must have nbformat and cells
        if "nbformat" not in doc or "cells" not in doc:
            raise validation_failed("content", "Notebook missing nbformat or cells")
