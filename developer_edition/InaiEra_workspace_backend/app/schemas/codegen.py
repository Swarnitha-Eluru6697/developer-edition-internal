"""Codegen chat schemas.

Request/response shapes for the Workspace-embedded chat that generates
SQL, dbt, and FlinkSQL code. See app/services/codegen.py for the
generate -> validate -> fix loop these wrap.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

Target = Literal["sql", "python", "dbt", "flinksql"]
Engine = Literal["trino", "pg_lake", "flink", "python"]
# The SQL engine a user can pick in the assistant for a SQL or dbt turn. "clickhouse" is listed so the UI can show it, but it is not implemented:
# the API refuses it (see api/codegen.py) until its dialect rules and validation exist.
SqlEngine = Literal["pg_lake", "trino", "clickhouse"]


class CodegenSessionCreate(BaseModel):
    workspace_id: UUID
    target: Target
    file_id: UUID | None = None
    # Defaults from workspace config (default_warehouse) if omitted.
    engine: Engine | None = None
    title: str | None = None


class CodegenSessionOut(ORMModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    file_id: UUID | None
    target: Target
    engine: Engine | None
    title: str | None
    created_at: datetime
    updated_at: datetime


class CodegenMessageOut(ORMModel):
    id: UUID
    session_id: UUID
    role: Literal["user", "assistant", "tool"]
    content: list[dict[str, Any]]
    generated_code: str | None
    generated_path: str | None
    validation_status: Literal["passed", "failed", "skipped"] | None
    validation_detail: dict[str, Any] | None
    created_at: datetime


class CodegenPromptRequest(BaseModel):
    """One turn from the user. Streamed back over WS on the session channel
    (see app/ws/gateway.py — same envelope shape as executions)."""
    prompt: str = Field(min_length=1, max_length=20_000)
    # Optional: the content of the file currently open in the editor, so the
    # assistant can propose an edit/diff rather than a from-scratch file.
    # Whether this actually reaches the model is a governed decision, not a
    # given — see app/services/codegen_governance.py (WS-AI-011).
    current_file_content: str | None = None
    # The user's explicit, per-turn opt-in to sending that content. Off by
    # default: without it the content is withheld no matter what else allows
    # it (WS-AI-011).
    include_current_file: bool = False
    # Optional per-turn target override (sql/python/dbt/flinksql). Lets the user
    # switch the model mid-conversation without a new session.
    target: Target | None = None
    # The caller's live workspace Session id (the one its WebSocket is
    # connected to — see /ws/sessions/{id}). Turn progress is multiplexed
    # onto THIS session's channel rather than a separate one keyed by the
    # codegen session, so the existing connection delivers it (WS-AI-011) —
    # the client no longer has to poll for a reply.
    ws_session_id: UUID
    # Optional generation-model selection — one of the in-perimeter models the
    # platform exposes (GET /api/v1/codegen/models). Invalid or omitted -> the
    # platform default. Every choice is self-hosted behind the model gateway.
    model: str | None = None
    # The database connection the user is working against (the editor's active one). The assistant reads THAT connection's real
    # schemas, tables and columns (through the same governed catalog the Object Explorer shows) so it writes SQL for tables and columns
    # that exist, instead of guessing. Omitted -> the generic lakehouse catalog, when one is reachable.
    connection_id: UUID | None = None
    # Optional per-turn SQL engine for the sql and dbt targets: the dialect the assistant writes and the engine its dbt check runs on.
    # Omitted -> trino. Ignored for python and flinksql (they have one engine each).
    engine: SqlEngine | None = None


class CodegenModelInfo(BaseModel):
    """A user-selectable, self-hosted generation model."""
    name: str          # logical gateway name, e.g. "codegen-generation"
    label: str         # human label for the UI
    is_default: bool = False


class CodegenModelsOut(BaseModel):
    models: list[CodegenModelInfo]


class CodegenPromptAccepted(BaseModel):
    session_id: UUID
    turn_id: UUID
    websocket_topic: str
    # Auditable, non-content signal for the UI: did the current-file content
    # actually go out with this turn, and if not, why (see ContextDecision).
    context_included: bool
    context_decision_reason: str


class CodegenArtifact(BaseModel):
    """A generated, validated artifact ready for the user to preview/apply."""
    path: str
    content: str
    # dbt targets may also produce/modify a schema.yml alongside the model.
    companion_path: str | None = None
    companion_content: str | None = None
    validation_status: Literal["passed", "failed"]
    validation_detail: dict[str, Any] = Field(default_factory=dict)


class CodegenApplyRequest(BaseModel):
    """Apply a generated artifact from a chat turn into the workspace file
    tree (creates or updates a file, same optimistic-concurrency rules as
    the regular file API)."""
    message_id: UUID
    if_match_sha256: str | None = None


class CodegenOrgSettingsOut(ORMModel):
    """Org-level codegen context-egress policy (WS-AI-011). See
    app/services/codegen_governance.py for how these gate a turn."""
    org_id: str
    allow_external_context: bool
    allowed_providers: list[str]
    redact_secrets: bool
    updated_by: UUID | None
    updated_at: datetime


class CodegenOrgSettingsUpdate(BaseModel):
    """Admin-only update. All fields optional so a caller can flip one
    without re-sending the rest."""
    allow_external_context: bool | None = None
    allowed_providers: list[str] | None = None
    redact_secrets: bool | None = None
