"""Database connection and catalog schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

ConnectionType = Literal["trino", "pg_lake", "postgres", "flink", "other"]


class DatabaseConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    connection_type: ConnectionType
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = None
    catalog_name: str | None = None
    # Client sends credentials here for writing to vault; never stored raw
    username: str | None = None
    password: str | None = None
    # TLS
    use_tls: bool = True
    extra_properties: dict[str, Any] = Field(default_factory=dict)


class DatabaseConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = None
    catalog_name: str | None = None
    username: str | None = None
    password: str | None = None
    use_tls: bool | None = None
    extra_properties: dict[str, Any] | None = None


class DatabaseConnectionOut(ORMModel):
    id: UUID
    org_id: str
    created_by_user_id: UUID
    name: str
    description: str | None
    connection_type: ConnectionType
    host: str | None
    port: int | None
    database_name: str | None
    catalog_name: str | None
    # W-011: what UCS is asked about when this connection is used; persisted, unaffected by renames.
    ucs_resource_fqn: str | None = None
    use_tls: bool
    is_verified: bool
    last_verified_at: datetime | None
    extra_properties: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ConnectionTestResult(BaseModel):
    success: bool
    latency_ms: int | None = None
    server_version: str | None = None
    error_message: str | None = None


class UcsCatalogOption(BaseModel):
    """A Unified Catalog source a connection can be linked to. host/port/
    database/username are the target UCS reaches it at (relational only)."""
    name: str
    engine: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None


class CatalogNamespace(BaseModel):
    name: str
    type: str = "schema"


class CatalogTable(BaseModel):
    name: str
    schema_name: str
    catalog_name: str | None = None
    table_type: str = "BASE TABLE"
    row_count: int | None = None


class CatalogColumn(BaseModel):
    name: str
    data_type: str
    is_nullable: bool = True
    default_value: str | None = None
    description: str | None = None
