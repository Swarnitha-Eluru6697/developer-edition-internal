"""Codegen chat API — the Workspace-embedded chat that generates SQL, dbt,
FlinkSQL and Python code with the self-hosted codegen agent (no external LLM
provider; see app/services/codegen.py).

Sessions are scoped to a workspace; turns stream over the existing
/ws/sessions/{session_id} gateway using the codegen.* message types (see
app/services/codegen.py), the same way SQL/notebook executions stream
execution.* messages. One WebSocket per user covers both.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.core.authz import get_owned_session
from app.core.config import get_settings
from app.core.deps import CurrentUser, DB
from app.core.errors import forbidden, not_found, validation_failed
from app.core.rbac import CATALOG_VIEW, SETTINGS_MANAGE, RequireFilesEdit
from app.models import CodegenMessage, CodegenSession, DatabaseConnection
from app.schemas.codegen import (
    CodegenApplyRequest, CodegenMessageOut, CodegenModelInfo, CodegenModelsOut, CodegenOrgSettingsOut,
    CodegenOrgSettingsUpdate, CodegenPromptAccepted, CodegenPromptRequest,
    CodegenSessionCreate, CodegenSessionOut,
)
from app.schemas.file import FileCreate, FileUpdate, FileMetadata
from app.services.audit_service import AuditService
from app.services.codegen import get_backend, get_codegen_service, resolve_turn_engine
from app.services.codegen_governance import decide_context_inclusion, get_org_settings
from app.services.file_service import FileService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/codegen", tags=["codegen"])

# Friendly labels for the logical model names. Kept here (not in the model
# gateway) so the UI copy is owned by the app; unknown names fall back to the
# raw logical name as the label.
_MODEL_LABELS: dict[str, str] = {
    "codegen-generation": "Default (inai-codegen)",
    "codegen-generation-alt": "Alternate (Qwen3-Coder 30B)",
}


async def _active_governance_provider() -> str | None:
    """The governance name of the model that answers right now ("codegen_gateway" / "openai" / "anthropic"); None if the agent
    setting cannot be read (nothing is then sent to it)."""
    from app.services.codegen_provider import GOVERNANCE_NAMES, CodegenConfigError, get_codegen_agent_config

    try:
        return GOVERNANCE_NAMES[(await get_codegen_agent_config())["provider"]]
    except CodegenConfigError:
        return None


@router.get("/models", response_model=CodegenModelsOut)
async def list_codegen_models(user: CurrentUser) -> CodegenModelsOut:
    """The generation models a user can pick between. By default the self-hosted ones behind the in-perimeter gateway. When an
    admin switched the agent to OpenAI/Anthropic there is exactly one answer — that provider's model — and no choice to offer."""
    from app.services.codegen_provider import EXTERNAL_PROVIDERS, LABELS, CodegenConfigError, get_codegen_agent_config, model_for

    settings = get_settings()
    default = settings.codegen_model_generation
    try:
        cfg = await get_codegen_agent_config()
    except CodegenConfigError:
        from app.services.codegen_provider import default_provider
        cfg = {"provider": default_provider()}
    if cfg["provider"] in EXTERNAL_PROVIDERS:
        label = f"{LABELS[cfg['provider']].replace(' key', '')} · {model_for(cfg['provider'])}"
        return CodegenModelsOut(models=[CodegenModelInfo(name=default, label=label, is_default=True)])
    return CodegenModelsOut(models=[
        CodegenModelInfo(name=name, label=_MODEL_LABELS.get(name, name), is_default=(name == default))
        for name in settings.codegen_generation_models
    ])


@router.post("/sessions", response_model=CodegenSessionOut, status_code=status.HTTP_201_CREATED)
async def create_codegen_session(
    payload: CodegenSessionCreate, user: CurrentUser, db: DB,
) -> CodegenSessionOut:
    settings = get_settings()
    if payload.target == "flinksql" and not settings.codegen_flinksql_enabled:
        raise validation_failed("target", "FlinkSQL authoring is not enabled in this data plane yet")

    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, payload.workspace_id)

    session = CodegenSession(
        workspace_id=ws.id,
        user_id=user_row.id,
        file_id=payload.file_id,
        target=payload.target,
        engine=payload.engine or get_backend(payload.target).default_engine,
        title=payload.title,
    )
    db.add(session)
    await db.flush()
    return CodegenSessionOut.model_validate(session)


@router.get("/sessions/{session_id}", response_model=CodegenSessionOut)
async def get_codegen_session(session_id: UUID, user: CurrentUser, db: DB) -> CodegenSessionOut:
    session = await _get_owned_session(db, session_id, user)
    return CodegenSessionOut.model_validate(session)


@router.get("/sessions/{session_id}/messages", response_model=list[CodegenMessageOut])
async def list_codegen_messages(session_id: UUID, user: CurrentUser, db: DB) -> list[CodegenMessageOut]:
    session = await _get_owned_session(db, session_id, user)
    result = await db.execute(
        select(CodegenMessage)
        .where(CodegenMessage.session_id == session.id)
        .order_by(CodegenMessage.created_at)
    )
    return [CodegenMessageOut.model_validate(m) for m in result.scalars().all()]


@router.post(
    "/sessions/{session_id}/messages",
    response_model=CodegenPromptAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_codegen_prompt(
    session_id: UUID, payload: CodegenPromptRequest, user: CurrentUser, db: DB, request: Request,
) -> CodegenPromptAccepted:
    """Submit a chat turn. Returns immediately (202); the reply streams over
    the caller's own WebSocket (`payload.ws_session_id`) as codegen.started /
    codegen.agent_step / codegen.tool_call / codegen.completed envelopes —
    mirrors how SQL/notebook executions stream (see /api/v1/executions/sql).
    """
    session = await _get_owned_session(db, session_id, user)
    settings = get_settings()
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)

    # The turn's progress is multiplexed onto the caller's own already-open
    # workspace-session channel (WS-AI-011) — verify they actually own that
    # session before publishing anything onto it, the same ownership check
    # SQL/notebook execution submission uses for the same field.
    ws_session = await get_owned_session(db, user_row.id, payload.ws_session_id, for_use=True)

    project_context = await _build_project_context(db, session, user)

    # Ground the assistant on the connection the user is working against (what the Object Explorer shows). The same capability that
    # lets them browse that catalog is required, and the catalog itself still applies UCS grants to this principal.
    if payload.connection_id is not None:
        if not user.has(CATALOG_VIEW):
            raise forbidden("Browsing the catalog is required to ground the assistant on a connection")
        if await db.get(DatabaseConnection, payload.connection_id) is None:
            raise not_found("database_connection", str(payload.connection_id))

    # Per-turn target override lets the user switch model mid-chat. Gate flinksql
    # the same way session creation does.
    turn_target = payload.target or session.target
    if turn_target == "flinksql" and not settings.codegen_flinksql_enabled:
        raise validation_failed("target", "FlinkSQL authoring is not enabled in this data plane yet")

    turn_engine = resolve_turn_engine(turn_target, payload.engine)

    # Whether the current-file content actually reaches the model is a
    # governed decision (org policy + UCS grant + provider allowlist), not
    # the client's default to make — see app/services/codegen_governance.py.
    context_content, decision = await decide_context_inclusion(
        db, user, workspace_id=session.workspace_id, raw_context=payload.current_file_content,
        user_opted_in=payload.include_current_file, provider=await _active_governance_provider(),
    )

    svc = get_codegen_service()
    turn = await svc.submit_turn(
        db,
        session=session,
        ws_session_id=ws_session.id,
        prompt=payload.prompt,
        current_file_content=context_content,
        project_context=project_context,
        target=turn_target,
        model_name=payload.model,
        connection_id=payload.connection_id,
        principal=user,
        engine=payload.engine,
    )
    await audit.record(
        user_id=user_row.id,
        action="codegen.prompt",
        resource_type="codegen_session",
        resource_id=session.id,
        workspace_id=session.workspace_id,
        ip_address=request.client.host if request.client else None,
        metadata={
            "target": turn_target,
            "engine": turn_engine,
            "model": payload.model,
            "connection_id": str(payload.connection_id) if payload.connection_id else None,
            # Auditable context category trail (WS-AI-011) — never the raw
            # content itself, only what category was (or wasn't) sent, and why.
            "context_included": decision.include_context,
            "context_categories": decision.categories,
            "context_redacted_categories": decision.redacted_categories,
            "context_decision_reason": decision.reason,
        },
    )
    return CodegenPromptAccepted(
        session_id=session.id,
        turn_id=turn.id,
        websocket_topic=f"session:{ws_session.id}",
        context_included=decision.include_context,
        context_decision_reason=decision.reason,
    )


@router.get("/org-settings", response_model=CodegenOrgSettingsOut)
async def get_codegen_org_settings(user: CurrentUser, db: DB) -> CodegenOrgSettingsOut:
    """Current org's codegen context-egress policy (WS-AI-011). Needs workspace:settings:manage —
    this controls whether proprietary source or secrets may leave the
    customer boundary at all."""
    if not user.has(SETTINGS_MANAGE):
        raise forbidden(f"missing permission {SETTINGS_MANAGE}: viewing the codegen governance settings needs it")
    row = await get_org_settings(db)
    return CodegenOrgSettingsOut.model_validate(row)


@router.put("/org-settings", response_model=CodegenOrgSettingsOut)
async def update_codegen_org_settings(
    payload: CodegenOrgSettingsUpdate, user: CurrentUser, db: DB, request: Request,
) -> CodegenOrgSettingsOut:
    """Update the org's codegen context-egress policy. Every field is
    optional; only what's provided changes. Recorded to the audit log since
    this is exactly the kind of policy decision that evidence trail exists
    for — turning it on is a deliberate, attributable act, not a default."""
    if not user.has(SETTINGS_MANAGE):
        raise forbidden(f"missing permission {SETTINGS_MANAGE}: changing the codegen governance settings needs it")
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    row = await get_org_settings(db)

    changes: dict[str, object] = {}
    if payload.allow_external_context is not None:
        changes["allow_external_context"] = payload.allow_external_context
        row.allow_external_context = payload.allow_external_context
    if payload.allowed_providers is not None:
        changes["allowed_providers"] = payload.allowed_providers
        row.allowed_providers = payload.allowed_providers
    if payload.redact_secrets is not None:
        changes["redact_secrets"] = payload.redact_secrets
        row.redact_secrets = payload.redact_secrets
    row.updated_by = user_row.id
    await db.flush()
    # onupdate=func.now() computes updated_at server-side on UPDATE, which
    # flush() only expires (not repopulates) — an explicit refresh is needed
    # before model_validate reads it, or the lazy-load it would otherwise
    # trigger fails outside an awaited context (MissingGreenlet).
    await db.refresh(row)

    await audit.record(
        user_id=user_row.id,
        action="codegen.org_settings_updated",
        resource_type="codegen_org_settings",
        resource_id=row.id,
        ip_address=request.client.host if request.client else None,
        metadata=changes,
    )
    return CodegenOrgSettingsOut.model_validate(row)


@router.post("/sessions/{session_id}/apply", response_model=FileMetadata)
async def apply_codegen_artifact(
    session_id: UUID, payload: CodegenApplyRequest, user: RequireFilesEdit, db: DB, request: Request,
) -> FileMetadata:
    """Take a validated artifact from a prior assistant message and write it
    into the workspace file tree — creating the file if new, or updating it
    with the same If-Match semantics as a manual save, so the chat panel
    and the editor never disagree about the current file state."""
    session = await _get_owned_session(db, session_id, user)
    message = await db.get(CodegenMessage, payload.message_id)
    if not message or message.session_id != session.id:
        raise not_found("codegen_message", str(payload.message_id))
    if not message.generated_code or not message.generated_path:
        raise validation_failed("message_id", "This message has no generated artifact to apply")
    if message.validation_status != "passed":
        raise validation_failed("message_id", "Cannot apply an artifact that failed validation")

    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, session.workspace_id)
    file_svc = FileService(db, user.org_id)

    existing = await file_svc.find_by_path(ws, message.generated_path, branch="main")
    # Infer file language from the extension; dbt models and ad hoc SQL are
    # both .sql, FlinkSQL maps to sql in the Phase 1 layout, Python is .py.
    _ext = message.generated_path.rsplit(".", 1)[-1].lower()
    language = {"py": "py", "sql": "sql", "ipynb": "ipynb", "yml": "yaml", "yaml": "yaml"}.get(_ext, "sql")
    if existing:
        row = await file_svc.update(
            ws, existing.id,
            FileUpdate(content=message.generated_code, if_match_sha256=payload.if_match_sha256),
        )
    else:
        row, _ = await file_svc.create(
            ws, FileCreate(path=message.generated_path, language=language, content=message.generated_code),
        )

    await audit.record(
        user_id=user_row.id,
        action="codegen.apply",
        resource_type="file",
        resource_id=row.id,
        workspace_id=session.workspace_id,
        ip_address=request.client.host if request.client else None,
        metadata={"session_id": str(session.id), "message_id": str(message.id), "target": session.target},
    )
    return FileMetadata.model_validate(row)


async def _get_owned_session(db: DB, session_id: UUID, user) -> CodegenSession:
    session = await db.get(CodegenSession, session_id)
    if not session:
        raise not_found("codegen_session", str(session_id))
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    if session.user_id != user_row.id:
        raise forbidden("Not your codegen session")
    return session


async def _build_project_context(db: DB, session: CodegenSession, user) -> str:
    """Cheap, bounded project context for the system prompt: nearby model
    files and their paths, so generated dbt code uses correct {{ ref() }}
    targets and matches the project's folder conventions. Kept lightweight
    (paths only, not full file contents) to control token cost; the agent
    can call get_table_schema for anything it needs in depth."""
    file_svc = FileService(db, user.org_id)
    ws_svc = WorkspaceService(db)
    ws = await ws_svc.get_by_id(session.workspace_id)
    files = await file_svc.list_files(ws, branch="main", language=None)
    paths = sorted(f.path for f in files if f.path.startswith("models/"))[:200]
    if not paths:
        return "(empty project — no existing models yet)"
    return "Existing model files in this project:\n" + "\n".join(f"  - {p}" for p in paths)
