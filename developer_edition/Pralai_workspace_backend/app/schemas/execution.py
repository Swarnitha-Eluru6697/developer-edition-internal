"""Execution schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel

Language = Literal["sql", "ipynb_cell"]
Status = Literal["pending", "running", "completed", "failed", "cancelled", "timeout"]

# The internal reference format app/api/executions.py builds ONLY after
# ucs_authorize has granted access to a connection (see submit_sql_execution)
# — the execution worker treats any "connection:<uuid>"-shaped target as a
# connection reference and runs it with that connection's credentials (it
# re-checks UCS at run time too, but the submit-time check is the first
# gate). `warehouse` must never be allowed to smuggle that shape in from the
# client, bypassing the submit-time check (WS-DATA-007).
_CONNECTION_TARGET_PREFIX = "connection:"


class SqlExecutionRequest(BaseModel):
    session_id: UUID
    file_id: UUID | None = None
    code_text: str
    warehouse: str | None = None
    connection_id: UUID | None = None  # run against this connection's database
    max_rows: int = Field(default=10_000, le=1_000_000)
    timeout_seconds: int = Field(default=300, le=1800)

    @field_validator("warehouse")
    @classmethod
    def _warehouse_cannot_impersonate_a_connection_reference(cls, v: str | None) -> str | None:
        if v is not None and v.strip().lower().startswith(_CONNECTION_TARGET_PREFIX):
            raise ValueError(
                "warehouse may not use the reserved 'connection:' prefix — "
                "use connection_id to target a registered connection"
            )
        return v


class NotebookCellExecutionRequest(BaseModel):
    session_id: UUID
    # None for a notebook / Python file that has not been saved yet (it exists only in the editor).
    file_id: UUID | None = None
    cell_id: str
    code_text: str
    kernel_id: str | None = None  # if None, server allocates one for the session


class ExecutionAccepted(BaseModel):
    execution_id: UUID
    status: Status
    websocket_topic: str


class ExecutionOut(ORMModel):
    id: UUID
    session_id: UUID
    workspace_id: UUID
    file_id: UUID | None
    cell_id: str | None
    language: str
    status: str
    code_text: str
    target: str | None
    row_count: int | None
    duration_ms: int | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class SqlResultChunk(BaseModel):
    execution_id: UUID
    chunk_index: int
    columns: list[dict[str, str]] | None = None  # only on chunk 0
    rows: list[list[Any]]
    row_count: int
    is_final: bool = False


class NotebookOutput(BaseModel):
    execution_id: UUID
    output_index: int
    output_type: Literal["stream", "display_data", "execute_result", "error"]
    mime_bundle: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # For stream: { name: 'stdout'|'stderr', text: '...' }
    # For execute_result: { execution_count, mime_bundle }
    # For error: { ename, evalue, traceback }


class SessionCreate(BaseModel):
    workspace_id: UUID


class SessionOut(ORMModel):
    id: UUID
    user_id: UUID
    workspace_id: UUID
    status: str
    started_at: datetime
    last_activity_at: datetime
    websocket_url: str
