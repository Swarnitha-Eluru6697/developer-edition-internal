"""Workspace schemas."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    default_warehouse: str | None = None
    default_kernel_image: str | None = None


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    default_warehouse: str | None = None
    default_kernel_image: str | None = None


class WorkspaceOut(ORMModel):
    id: UUID
    owner_id: UUID
    name: str
    description: str | None
    workspace_type: str
    storage_prefix: str
    default_warehouse: str | None
    default_kernel_image: str | None
    created_at: datetime
    updated_at: datetime
