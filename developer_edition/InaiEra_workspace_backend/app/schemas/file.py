"""File schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

Language = Literal["sql", "flinksql", "ipynb", "py", "md", "yaml", "json", "other"]
# Phase 1 enforces sql + ipynb only (see file_service.py).
# 'flinksql' is authored in the same Monaco editor as 'sql' but is never sent to
# the lakehouse SQL executor — it is streaming DDL/DML for the FlinkSQL Runner.


class FileCreate(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    language: Language
    branch: str = "main"
    content: str = ""


class FileUpdate(BaseModel):
    """Partial update of file content (autosave)."""
    content: str
    # If client knows the expected current sha256, server uses it for optimistic
    # concurrency. Omit on first save.
    if_match_sha256: str | None = None


class FileMetadata(ORMModel):
    id: UUID
    workspace_id: UUID
    path: str
    language: str
    branch: str
    size_bytes: int
    sha256: str | None
    last_committed_sha: str | None
    is_dirty: bool
    created_at: datetime
    updated_at: datetime


class FileWithContent(FileMetadata):
    content: str


class FileTreeNode(BaseModel):
    """One entry in the file tree."""
    name: str
    path: str
    type: Literal["file", "directory"]
    id: str | None = None  # file id (only for files) — used to open the file
    language: str | None = None  # only for files
    size_bytes: int | None = None
    is_dirty: bool | None = None
    children: list["FileTreeNode"] = Field(default_factory=list)


FileTreeNode.model_rebuild()
