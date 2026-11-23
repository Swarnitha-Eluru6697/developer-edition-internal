"""dbt Runner proxy.

The frontend (SPA) talks to a single origin (workspace service). This
router forwards selected dbt Runner API calls through the workspace backend
so the browser never needs to reach the dbt Runner ClusterIP directly
(which would require CORS configuration on the dbt Runner and knowledge of
its internal URL in the browser).

Proxy scope — intentionally narrow:
  GET  /api/v1/dbt/projects               → list projects for this org
  POST /api/v1/dbt/projects/register      → register a new project
  GET  /api/v1/dbt/projects/{id}          → get one project
  PATCH /api/v1/dbt/projects/{id}         → update schedule / git_ref
  POST /api/v1/dbt/projects/{id}/run      → trigger manual run
  GET  /api/v1/dbt/projects/{id}/runs     → list runs
  GET  /api/v1/dbt/projects/{id}/runs/{rid} → get run detail
  GET  /api/v1/dbt/projects/{id}/runs/{rid}/events → run event log
  GET  /api/v1/dbt/projects/{id}/lineage  → lineage graph

Contract: every route declares a ``response_model`` (app/schemas/proxy.py)
so the OpenAPI document — and the SPA's generated types — describe the dbt
Runner's real shapes. The body is still forwarded byte-for-byte: returning a
``Response`` skips FastAPI's response validation, so an upstream addition can
never fail the proxy (WS-FE-014).

Auth (X-001): the user's own credential is NEVER forwarded. Each call carries a short-lived DELEGATED token Workspace mints (HS256,
aud "dbtrunner", sub = the gateway-authenticated user's immutable subject, one scope, signed with DBTRUNNER_DELEGATION_JWT_SECRET),
whose `permissions` claim carries the caller's job permissions that scope needs (app/core/service_tokens.py). Permissions: viewing
jobs needs any dbt / SQL-job / Python-job permission, editing any edit one (registering: the one for the job's kind), running any
run one; the runner checks the kind-specific permission against the job itself.
Failures reaching the runner are a 502 / 504 in the standard envelope (app/core/upstream.py), never a hang or a bare 500.

The /api/v1/validate endpoint is NOT proxied here — it is called
server-side by the codegen service, never from the browser.
"""
from __future__ import annotations

import json
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_failed
from app.core.upstream import upstream_guard, upstream_timeout
from app.core.deps import DB
from app.core.rbac import JOB_KIND_EDIT, SQL_RUN, RequireJobEdit, RequireJobRun, RequireJobView, ensure_permission
from app.executors.connection_job_executor import PG_CONNECTION_TYPES
from app.services import dagster_runs
from app.models import DatabaseConnection, JobConnectionBinding
from app.services.ucs_client import UcsAuthorizationError, authorize_connection_use
from app.services.workspace_service import WorkspaceService
from app.core.service_tokens import (
    DBT_SCOPE_PROJECTS_READ, DBT_SCOPE_PROJECTS_WRITE, DBT_SCOPE_RUNS_READ, DBT_SCOPE_RUNS_TRIGGER,
    DBTRUNNER_AUDIENCE, delegated_authorization,
)
from app.schemas.proxy import (
    DbtLineage,
    DbtProject,
    DbtProjectRegister,
    DbtProjectUpdate,
    DbtRun,
    DbtRunEvent,
    DbtRunTriggerRequest,
    json_body,
)
from app.core.workspace_scope import authorize_workspace, requested_workspace_id

log = structlog.get_logger()
router = APIRouter(prefix="/api/v1/dbt", tags=["dbt-proxy"])

_FORWARD_METHODS = {"GET", "POST", "PATCH", "DELETE"}


async def _proxy(request: Request, path: str, authorization: str) -> Response:
    """Forward the request to the dbt Runner and return its response."""
    settings = get_settings()
    url = f"{settings.dbtrunner_url}/api/v1/{path.lstrip('/')}"

    body = await request.body()
    headers = {
        "Authorization": authorization,
        "Content-Type": request.headers.get("content-type", "application/json"),
        "X-Forwarded-For": request.client.host if request.client else "unknown",
    }

    with upstream_guard("the dbt runner"):
        async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
            upstream = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )

    log.info(
        "dbt_proxy",
        method=request.method,
        path=path,
        status=upstream.status_code,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def _auth(user, scope: str, workspace_id=None) -> str:
    """X-001: a short-lived delegated token for exactly this call, never the user's own bearer/cookie.
    `workspace_id` is a workspace the caller was verified to own (W-016); it rides in the signed token."""
    return delegated_authorization(user, audience=DBTRUNNER_AUDIENCE, scope=scope, workspace_id=workspace_id)


def _requested_kind(body: bytes) -> str:
    """The job kind a registration asks for ("dbt" when absent, as the runner defaults it). Unknown kinds are the
    schema's 422, so treat them as dbt here and let validation answer."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "dbt"
    kind = data.get("kind") if isinstance(data, dict) else None
    return kind if kind in JOB_KIND_EDIT else "dbt"


# ── SQL jobs on a connection ───────────────────────────────────────────────

async def _authorize_job_connection(db, user, body: bytes) -> DatabaseConnection | None:
    """The connection a SQL job asks to run on (None when it names none): it must exist in this org, be a Postgres connection, and
    the caller must hold the SQL capability and pass the same UCS decision as running SQL on it interactively."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    raw = data.get("connection_id") if isinstance(data, dict) else None
    if raw in (None, ""):
        return None
    try:
        connection_id = UUID(str(raw))
    except ValueError:
        raise validation_failed("connection_id", "connection_id must be a connection id") from None
    if not user.has(SQL_RUN):
        raise forbidden(f"missing permission {SQL_RUN}: creating a job on a connection needs it")
    conn = await db.get(DatabaseConnection, connection_id)
    if conn is None or conn.org_id != user.org_id:
        raise not_found("database_connection", str(connection_id))
    if conn.connection_type not in PG_CONNECTION_TYPES:
        raise validation_failed("connection_id", f"SQL jobs run on Postgres connections only, not '{conn.connection_type}'.")
    row = await WorkspaceService(db).ensure_user(user)
    try:
        await authorize_connection_use(user, row.id, conn)
    except UcsAuthorizationError as e:
        raise forbidden(f"Not authorized to use connection '{conn.name}': {e.reason}") from None
    return conn


async def _bind_job_connection(db, user, response: Response, conn: DatabaseConnection) -> None:
    """Record that the job the runner just registered runs on `conn`, as `user` (with the groups and permissions they have now)."""
    project_id = UUID(json.loads(response.body)["id"])
    try:
        row = await WorkspaceService(db).ensure_user(user)
        db.add(JobConnectionBinding(project_id=project_id, connection_id=conn.id, user_id=row.id, **user.stored_identity()))
        await db.flush()
    except Exception:
        # The job exists in the runner but could never run (no binding): take it back rather than leave a dead job behind.
        try:
            async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
                await client.delete(f"{get_settings().dbtrunner_url}/api/v1/projects/{project_id}",
                                    headers={"Authorization": _auth(user, DBT_SCOPE_PROJECTS_WRITE)})
        except Exception:  # noqa: BLE001
            log.warning("sql_job_binding_rollback_failed")
        raise


# ── Project routes ─────────────────────────────────────────────────────────

@router.get("/projects", response_model=list[DbtProject])
async def list_projects(request: Request, user: RequireJobView) -> Response:
    return await _proxy(request, "projects", _auth(user, DBT_SCOPE_PROJECTS_READ))


@router.post(
    "/projects/register", status_code=status.HTTP_201_CREATED,
    response_model=DbtProject, openapi_extra=json_body(DbtProjectRegister),
)
async def register_project(request: Request, user: RequireJobEdit, db: DB) -> Response:
    # DBT-012: the project registry is shared by the whole org; `workspace_id` is a grouping label, accepted only for a
    # workspace the caller owns and carried in the signed token, never trusted from the request JSON alone.
    body = await request.body()
    # dbt projects, SQL jobs and Python jobs each have their own edit permission (the runner checks it again by job id).
    ensure_permission(user, JOB_KIND_EDIT[_requested_kind(body)])
    workspace_id = await authorize_workspace(db, user, requested_workspace_id(body))
    # A SQL job on a Workspace connection: the caller must be allowed to use that connection NOW, and the job then runs as them.
    connection = await _authorize_job_connection(db, user, body)
    response = await _proxy(request, "projects/register", _auth(user, DBT_SCOPE_PROJECTS_WRITE, workspace_id))
    if connection is not None and response.status_code == status.HTTP_201_CREATED:
        await _bind_job_connection(db, user, response, connection)
    return response


@router.get("/projects/{project_id}", response_model=DbtProject)
async def get_project(project_id: UUID, request: Request, user: RequireJobView) -> Response:
    return await _proxy(request, f"projects/{project_id}", _auth(user, DBT_SCOPE_PROJECTS_READ))


@router.patch(
    "/projects/{project_id}", response_model=DbtProject,
    openapi_extra=json_body(DbtProjectUpdate),
)
async def update_project(project_id: UUID, request: Request, user: RequireJobEdit) -> Response:
    return await _proxy(request, f"projects/{project_id}", _auth(user, DBT_SCOPE_PROJECTS_WRITE))


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: UUID, request: Request, user: RequireJobEdit, db: DB) -> Response:
    response = await _proxy(request, f"projects/{project_id}", _auth(user, DBT_SCOPE_PROJECTS_WRITE))
    if response.status_code in (status.HTTP_204_NO_CONTENT, status.HTTP_200_OK):
        # The runner has already deleted the job, which is what the caller asked for: a binding left behind (it names a project
        # that no longer exists, so nothing can use it) must not turn that into an error.
        try:
            await db.execute(delete(JobConnectionBinding).where(JobConnectionBinding.project_id == project_id))
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            log.warning("job_connection_binding_cleanup_failed", project_id=str(project_id), error=type(e).__name__)
    return response


@router.post(
    "/projects/{project_id}/run", status_code=status.HTTP_202_ACCEPTED,
    response_model=DbtRun, openapi_extra=json_body(DbtRunTriggerRequest, required=False),
)
async def trigger_run(project_id: UUID, request: Request, user: RequireJobRun) -> Response:
    if not get_settings().dagster_graphql_url:
        return await _proxy(request, f"projects/{project_id}/run", _auth(user, DBT_SCOPE_RUNS_TRIGGER))
    return await _trigger_run_through_dagster(project_id, user)


async def _runner_get(user, path: str, scope: str) -> httpx.Response:
    with upstream_guard("the dbt runner"):
        async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
            return await client.get(f"{get_settings().dbtrunner_url}/api/v1/{path}", headers={"Authorization": _auth(user, scope)})


async def _trigger_run_through_dagster(project_id: UUID, user) -> Response:
    """Run now when Dagster is configured: launch the project's Dagster job, so the dependency rule enforced there applies to a run
    started by hand too (app/services/dagster_runs.py). The caller must be able to see the project (the runner decides, as for any
    other call), because Dagster itself does not know who is asking. Answers with the runner's run, like the direct call."""
    visible = await _runner_get(user, f"projects/{project_id}", DBT_SCOPE_PROJECTS_READ)
    if visible.status_code != 200:
        return Response(content=visible.content, status_code=visible.status_code,
                        media_type=visible.headers.get("content-type", "application/json"))
    runner_run = await dagster_runs.run_now(str(project_id), triggered_by=str(user.user_id))
    run = await _runner_get(user, f"projects/{project_id}/runs/{runner_run}", DBT_SCOPE_RUNS_READ)
    log.info("dbt_run_now_via_dagster", project_id=str(project_id), run_id=runner_run, status=run.status_code)
    return Response(content=run.content, status_code=status.HTTP_202_ACCEPTED if run.status_code == 200 else run.status_code,
                    media_type=run.headers.get("content-type", "application/json"))


@router.get("/projects/{project_id}/runs", response_model=list[DbtRun])
async def list_runs(project_id: UUID, request: Request, user: RequireJobRun) -> Response:
    return await _proxy(request, f"projects/{project_id}/runs", _auth(user, DBT_SCOPE_RUNS_READ))


@router.get("/projects/{project_id}/runs/{run_id}", response_model=DbtRun)
async def get_run(project_id: UUID, run_id: UUID, request: Request, user: RequireJobRun) -> Response:
    return await _proxy(request, f"projects/{project_id}/runs/{run_id}", _auth(user, DBT_SCOPE_RUNS_READ))


@router.get("/projects/{project_id}/runs/{run_id}/events", response_model=list[DbtRunEvent])
async def list_run_events(
    project_id: UUID, run_id: UUID, request: Request, user: RequireJobRun,
) -> Response:
    return await _proxy(
        request, f"projects/{project_id}/runs/{run_id}/events", _auth(user, DBT_SCOPE_RUNS_READ),
    )


@router.get("/projects/{project_id}/lineage", response_model=DbtLineage)
async def get_lineage(project_id: UUID, request: Request, user: RequireJobView) -> Response:
    return await _proxy(request, f"projects/{project_id}/lineage", _auth(user, DBT_SCOPE_PROJECTS_READ))
