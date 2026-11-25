"""Executions and sessions API."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import APIRouter, Request, status

from app.core.authz import get_owned_execution, get_owned_session
from app.core.config import get_settings
from app.core.deps import CurrentUser, DB
from app.core.errors import forbidden, not_found
from app.core.rbac import RequireNotebook, RequireSqlExecute
from app.models import Session as SessionModel
from app.schemas.execution import (
    ExecutionAccepted, ExecutionOut, NotebookCellExecutionRequest, SessionCreate,
    SessionOut, SqlExecutionRequest,
)
from app.services.audit_service import AuditService
from app.services.execution_router import get_execution_router
from app.services.ucs_client import UcsAuthorizationError, authorize_connection_use
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1", tags=["executions"])


def _submit_context(user) -> dict:
    """Request correlation + the caller's groups and permissions, persisted on the execution
    row so the worker process can log with the same ids and re-check
    authorization as the same principal (WS-016, WS-DATA-007)."""
    ctx = structlog.contextvars.get_contextvars()
    return {
        "request_id": ctx.get("request_id"),
        "trace_id": ctx.get("trace_id"),
        "principal_groups": list(user.groups or []),
        "principal_permissions": sorted(user.permissions or ()),
    }


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate, user: CurrentUser, db: DB, request: Request,
) -> SessionOut:
    settings = get_settings()
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, payload.workspace_id)
    session = SessionModel(
        user_id=user_row.id,
        workspace_id=ws.id,
        status="active",
        runtime_metadata={},
    )
    db.add(session)
    await db.flush()
    await audit.record(
        user_id=user_row.id,
        action="session.create",
        resource_type="session",
        resource_id=session.id,
        ip_address=request.client.host if request.client else None,
    )
    return SessionOut(
        id=session.id,
        user_id=session.user_id,
        workspace_id=session.workspace_id,
        status=session.status,
        started_at=session.started_at,
        last_activity_at=session.last_activity_at,
        websocket_url=f"/ws/sessions/{session.id}",
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def terminate_session(session_id: UUID, user: CurrentUser, db: DB) -> None:
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    session = await get_owned_session(db, user_row.id, session_id)
    session.status = "terminated"
    # Was never written here (WS-LIFE-013) despite the column existing since
    # the initial migration — a terminated session looked identical to one
    # still theoretically active by every timestamp on the row.
    session.ended_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
    # Revoke the session's open WebSockets (the gateway closes on this).
    import json
    from app.services.execution_router import get_redis, session_channel_for
    try:
        await get_redis().publish(session_channel_for(str(session.id)), json.dumps({
            "v": 1, "type": "session.terminated", "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": str(session.id),
        }))
    except Exception:  # noqa: BLE001 — new tickets are refused regardless
        structlog.get_logger().warning("session_terminate_publish_failed", session_id=str(session.id))


@router.post("/executions/sql", response_model=ExecutionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_sql_execution(
    payload: SqlExecutionRequest, user: RequireSqlExecute, db: DB, request: Request,
) -> ExecutionAccepted:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    session = await get_owned_session(db, user_row.id, payload.session_id, for_use=True)

    # If a connection is selected, target it by reference only — the raw,
    # credential-bearing DSN is never built here. It's resolved in memory
    # inside the execution runtime (sql_executor.py), right before use, so
    # a password can never end up in the Execution row, an audit log, or a
    # trace (WS-DATA-007). Using someone else's registered connection also
    # requires an explicit UCS grant — existence of the row is not permission
    # to use it.
    target = payload.warehouse
    if payload.connection_id:
        from app.models import DatabaseConnection
        conn = await db.get(DatabaseConnection, payload.connection_id)
        if not conn:
            raise not_found("database_connection", str(payload.connection_id))
        try:
            await authorize_connection_use(user, user_row.id, conn)
        except UcsAuthorizationError as e:
            raise forbidden(f"Not authorized to use connection '{conn.name}': {e.reason}")
        target = f"connection:{conn.id}"

    router_svc = get_execution_router()
    execution = await router_svc.submit(
        db=db,
        session_id=session.id,
        user_id=user_row.id,
        workspace_id=session.workspace_id,
        file_id=payload.file_id,
        cell_id=None,
        language="sql",
        code_text=payload.code_text,
        target=target,
        max_rows=payload.max_rows,
        timeout_seconds=payload.timeout_seconds,
        **_submit_context(user),
    )
    await audit.record(
        user_id=user_row.id,
        action="execution.submit",
        resource_type="execution",
        resource_id=execution.id,
        workspace_id=session.workspace_id,
        ip_address=request.client.host if request.client else None,
        metadata={
            "language": "sql",
            "file_id": str(payload.file_id) if payload.file_id else None,
            "warehouse": payload.warehouse,
            "code_sha256": execution.code_sha256,
        },
    )
    return ExecutionAccepted(
        execution_id=execution.id,
        status=execution.status,
        websocket_topic=f"execution:{execution.id}",
    )


@router.post("/executions/notebook-cell", response_model=ExecutionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_notebook_cell_execution(
    payload: NotebookCellExecutionRequest, user: RequireNotebook, db: DB, request: Request,
) -> ExecutionAccepted:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    session = await get_owned_session(db, user_row.id, payload.session_id, for_use=True)

    router_svc = get_execution_router()
    # An explicit kernel_id wins, else the session's existing kernel. With
    # neither, target stays empty and the WORKER allocates the session's
    # kernel when it runs the cell — starting a kernel can take a minute and
    # must not hold this request open (WS-EXEC-005).
    kernel_id = payload.kernel_id or (session.runtime_metadata or {}).get("kernel_id")

    execution = await router_svc.submit(
        db=db,
        session_id=session.id,
        user_id=user_row.id,
        workspace_id=session.workspace_id,
        file_id=payload.file_id,
        cell_id=payload.cell_id,
        language="ipynb_cell",
        code_text=payload.code_text,
        target=kernel_id,
        max_rows=10_000,
        timeout_seconds=600,
        **_submit_context(user),
    )
    await audit.record(
        user_id=user_row.id,
        action="execution.submit",
        resource_type="execution",
        resource_id=execution.id,
        workspace_id=session.workspace_id,
        ip_address=request.client.host if request.client else None,
        metadata={
            "language": "ipynb_cell",
            "file_id": str(payload.file_id) if payload.file_id else None,
            "cell_id": payload.cell_id,
            "kernel_id": kernel_id,
        },
    )
    return ExecutionAccepted(
        execution_id=execution.id,
        status=execution.status,
        websocket_topic=f"execution:{execution.id}",
    )


@router.get("/executions", response_model=list[ExecutionOut])
async def list_executions(
    user: CurrentUser,
    db: DB,
    workspace_id: UUID | None = None,
    language: str | None = None,
    limit: int = 50,
) -> list[ExecutionOut]:
    """The current user's execution history, most recent first — powers the
    SQL query-history panel. Scoped to the caller; optionally filtered by
    workspace and language (e.g. language=sql)."""
    from sqlalchemy import select
    from app.models import Execution
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    q = select(Execution).where(Execution.user_id == user_row.id)
    if workspace_id is not None:
        q = q.where(Execution.workspace_id == workspace_id)
    if language is not None:
        q = q.where(Execution.language == language)
    q = q.order_by(Execution.created_at.desc()).limit(min(max(limit, 1), 200))
    rows = (await db.execute(q)).scalars().all()
    return [ExecutionOut.model_validate(r) for r in rows]


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
async def get_execution(execution_id: UUID, user: CurrentUser, db: DB) -> ExecutionOut:
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    execution = await get_owned_execution(db, user_row.id, execution_id)
    return ExecutionOut.model_validate(execution)


def assemble_sql_outputs(output_rows) -> tuple[list, list, list, list]:
    """(columns, rows, outputs, results) from an execution's stored chunks.

    `results` holds one entry per statement of a script; columns/rows are the last statement that
    returned a result set (what a one-result client has always been shown); `outputs` is the
    notebook cell output, carried through verbatim.
    """
    outputs: list = []
    # One entry per statement of a script (index 0 for a single statement / older chunks).
    by_statement: dict[int, dict] = {}

    def entry(i: int) -> dict:
        return by_statement.setdefault(i, {
            "statement": i, "sql": None, "columns": [], "rows": [],
            "row_count": None, "truncated": False, "status": None, "error": None,
        })

    for o in output_rows:
        payload = o.payload or {}
        if o.output_type == "rows":
            e = entry(payload.get("statement", 0))
            if not e["columns"] and payload.get("columns"):
                e["columns"] = payload["columns"]
            if payload.get("sql") and not e["sql"]:
                e["sql"] = payload["sql"]
            e["rows"].extend(payload.get("rows", []))
        elif o.output_type == "statement":
            e = entry(payload.get("statement", 0))
            e["sql"] = payload.get("sql") or e["sql"]
            e["row_count"] = payload.get("total_rows")
            e["truncated"] = bool(payload.get("truncated", False))
            e["status"] = payload.get("status")
        elif o.output_type in ("stream", "display_data", "execute_result", "error"):
            # Notebook (Python) cell output — carried through verbatim.
            outputs.append({"output_type": o.output_type, "payload": payload})
            if o.output_type == "error" and "statement" in payload:
                entry(payload["statement"])["error"] = payload.get("error_message")
    ordered = [by_statement[k] for k in sorted(by_statement)]
    for e in ordered:
        if e["row_count"] is None:
            e["row_count"] = len(e["rows"])
    # Top level stays what it always was — the last result set that has columns — so a client that
    # only knows one result per run (and history seeded from it) keeps working unchanged.
    shown = next((e for e in reversed(ordered) if e["columns"]), None)
    columns = shown["columns"] if shown else []
    rows = shown["rows"] if shown else []
    return columns, rows, outputs, ordered


@router.get("/executions/{execution_id}/result")
async def get_execution_result(execution_id: UUID, user: CurrentUser, db: DB) -> dict:
    """Return the fully materialized result of an execution from persisted
    output chunks — a reliable REST fallback to the WebSocket stream.

    For SQL: { status, columns:[{name,type}], rows:[[...]], row_count, ... }
    and, for a script of several statements, `results`: one entry per statement
    ({statement, sql, columns, rows, row_count, truncated, status, error}); the top-level
    columns/rows are then the last statement that returned a result set.
    For notebook cells: the same envelope plus `outputs`, an ordered list of
    Jupyter output chunks ({output_type, payload}) — stream / display_data /
    execute_result / error — so the client can render cell output faithfully.
    """
    from sqlalchemy import select
    from app.models import ExecutionOutput
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    execution = await get_owned_execution(db, user_row.id, execution_id)
    output_rows = (await db.execute(
        select(ExecutionOutput)
        .where(ExecutionOutput.execution_id == execution_id)
        .order_by(ExecutionOutput.chunk_index)
    )).scalars().all()
    columns, rows, outputs, ordered = assemble_sql_outputs(output_rows)
    result = {
        "execution_id": str(execution_id),
        "status": execution.status,
        "language": execution.language,
        "columns": columns,
        "rows": rows,
        "outputs": outputs,
        "row_count": execution.row_count or len(rows),
        "error_code": execution.error_code,
        "error_message": execution.error_message,
    }
    # Only a multi-statement script adds `results` (every statement's own columns/rows); a single
    # statement is already fully described by the fields above, so its rows are not sent twice.
    if len(ordered) > 1:
        result["results"] = ordered
    return result


@router.post("/executions/{execution_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_execution(execution_id: UUID, user: CurrentUser, db: DB) -> dict:
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    execution = await get_owned_execution(db, user_row.id, execution_id)
    # "cancelled" (was queued), "cancelling" (running; its worker stops it
    # and the terminal event follows) or "finished" (already terminal).
    outcome = await get_execution_router().cancel(execution_id, execution.language)
    return {"status": outcome}
