"""Internal: the dbt runner asks the Workspace to run a SQL job's statements on a Workspace connection.

POST /api/v1/internal/jobs/sql — not for browsers, not in the OpenAPI document. Authenticated by the one-run token the runner mints
(app/core/runner_job_token.py), never by a user session. What runs, and as whom, is decided here from the binding written when the
job was registered through the Workspace (JobConnectionBinding): the registering user must STILL be allowed to use the connection
(the same UCS decision as running SQL on it interactively), or the run is refused.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from app.core.auth import Principal, load_list
from app.core.config import get_settings
from app.core.deps import DB
from app.core.errors import WorkspaceError
from app.core.runner_job_token import consume_jti, decode_runner_job_token
from app.executors.connection_job_executor import PG_CONNECTION_TYPES, run_job_statements
from app.models import DatabaseConnection, JobConnectionBinding, User
from app.services.audit_service import AuditService
from app.services.ucs_client import UcsAuthorizationError, authorize_connection_use

log = structlog.get_logger()
router = APIRouter(prefix="/api/v1/internal/jobs", tags=["internal"], include_in_schema=False)

MAX_STATEMENTS = 500
MAX_STATEMENT_CHARS = 1_000_000
MAX_TOTAL_CHARS = 4_000_000


class SqlJobIn(BaseModel):
    statements: list[str] = Field(min_length=1, max_length=MAX_STATEMENTS)
    timeout_seconds: int = Field(default=3600, ge=1, le=7200)


@router.post("/sql")
async def run_sql_job(body: SqlJobIn, db: DB, x_internal_service_token: str | None = Header(default=None)) -> dict:
    claims = decode_runner_job_token(x_internal_service_token)
    if any(len(s) > MAX_STATEMENT_CHARS for s in body.statements) or sum(len(s) for s in body.statements) > MAX_TOTAL_CHARS:
        raise WorkspaceError(413, "SQL_JOB_TOO_LARGE", "the job's SQL is too large")
    await consume_jti(claims)

    project_id, connection_id = uuid.UUID(claims["project_id"]), uuid.UUID(claims["connection_id"])
    binding = await db.get(JobConnectionBinding, project_id)
    if binding is None or binding.connection_id != connection_id:
        raise WorkspaceError(403, "JOB_NOT_BOUND", "This job is not bound to that connection (the connection was removed, or the job "
                                                   "was not registered through the Workspace). Register it again.")
    conn = await db.get(DatabaseConnection, connection_id)
    user = await db.get(User, binding.user_id)
    if conn is None or user is None:
        raise WorkspaceError(403, "JOB_NOT_BOUND", "The connection or the user this job runs as no longer exists.")
    if conn.connection_type not in PG_CONNECTION_TYPES:
        raise WorkspaceError(400, "UNSUPPORTED_CONNECTION", f"SQL jobs run on Postgres connections only, not '{conn.connection_type}'.")

    principal = Principal(
        user_id=user.external_id, org_id=get_settings().org_id, roles=[],
        groups=load_list(binding.groups), permissions=frozenset(load_list(binding.permissions)),
        email=user.username or user.email or user.external_id, issued_at=0, expires_at=0, username=user.username)
    try:
        await authorize_connection_use(principal, user.id, conn)
    except UcsAuthorizationError as e:
        raise WorkspaceError(403, "CONNECTION_USE_DENIED", f"{user.username or user.external_id} may no longer use connection "
                                                           f"'{conn.name}': {e.reason}") from None

    result = await run_job_statements(conn=conn, statements=body.statements, timeout_seconds=body.timeout_seconds)
    try:
        await AuditService(db).record(
            user_id=user.id, action="sql_job.run", resource_type="database_connection", resource_id=conn.id,
            metadata={"project_id": str(project_id), "run_id": claims["run_id"], "statements": len(body.statements),
                      "success": result["success"]})
    except Exception:  # noqa: BLE001 — the run happened; failing to log it must not hide its result
        log.warning("sql_job_audit_failed", project_id=str(project_id))
    return result
