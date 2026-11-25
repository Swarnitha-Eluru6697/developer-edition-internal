"""SQL jobs on a Workspace connection (Database Explorer): the runner never holds the connection's credentials.

The job's statements are sent to the Workspace (`POST /api/v1/internal/jobs/sql`), which runs them against the connection as the user
who registered the job — after checking that this project is bound to that connection and that the user may still use it — in one
transaction, and answers with each statement's outcome. What authorises the call is a token the runner mints for THIS run
(`mint_run_token`): signed with WORKSPACE_JOB_JWT_SECRET, naming the project, run and connection, single-use, valid for the run's
duration. Whoever runs the statements (the runner process, or the isolated Kubernetes Job pod) gets only that token.

The result has the shape of a dbt result (success, errors, nodes), like every other job.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import structlog
from jose import jwt

from app.core.config import get_settings
from app.executors.sql_job_executor import split_statements
from app.services.data_dependencies import statement_lineage

log = structlog.get_logger()

ISSUER = "dbtrunner"
AUDIENCE = "workspace"
SCOPE = "sql.jobs.run"
SLACK_SECONDS = 600           # time to start, queue and answer on top of the statement timeout


def mint_run_token(*, project_id: uuid.UUID, run_id: uuid.UUID, connection_id: str) -> str:
    """The credential for one run's one call to the Workspace. Raises if the runner has no secret/URL configured for it."""
    s = get_settings()
    if not s.workspace_job_jwt_secret or not s.workspace_url:
        raise RuntimeError("SQL jobs on a connection are not configured on this runner (WORKSPACE_URL and WORKSPACE_JOB_JWT_SECRET)")
    now = int(time.time())
    return jwt.encode({
        "iss": ISSUER, "sub": ISSUER, "aud": AUDIENCE, "scope": SCOPE, "org_id": s.org_id,
        "project_id": str(project_id), "run_id": str(run_id), "connection_id": connection_id,
        "iat": now, "exp": now + s.sql_job_statement_timeout_seconds + SLACK_SECONDS, "jti": uuid.uuid4().hex,
    }, s.workspace_job_jwt_secret, algorithm="HS256")


def run_sql_file_on_connection(*, sql_path: Path, token: str) -> dict[str, Any]:
    s = get_settings()
    if not s.workspace_url:
        return {"success": False, "errors": ["SQL jobs on a connection are not configured on this runner (WORKSPACE_URL)"], "nodes": []}
    try:
        statements = split_statements(sql_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return {"success": False, "errors": [f"could not read the SQL file: {e}"], "nodes": []}
    if not statements:
        return {"success": False, "errors": ["the SQL file has no statements"], "nodes": []}
    url = f"{s.workspace_url.rstrip('/')}/api/v1/internal/jobs/sql"
    try:
        resp = httpx.post(url, json={"statements": statements, "timeout_seconds": s.sql_job_statement_timeout_seconds},
                          headers={"X-Internal-Service-Token": token}, timeout=s.sql_job_statement_timeout_seconds * len(statements) + 60)
    except httpx.HTTPError as e:
        log.warning("connection_sql_job_unreachable", error=type(e).__name__)
        return {"success": False, "errors": [f"the Workspace could not be reached to run the statements ({type(e).__name__})"], "nodes": []}
    if resp.status_code != 200:
        detail = ""
        try:
            body = resp.json().get("detail")
            detail = (body.get("message") if isinstance(body, dict) else body) or ""
        except Exception:  # noqa: BLE001
            pass
        return {"success": False, "nodes": [], "errors": [f"the Workspace refused the job (HTTP {resp.status_code})" + (f": {str(detail)[:300]}" if detail else "")]}
    body = resp.json()
    result = {"success": bool(body.get("success")), "errors": list(body.get("errors") or []), "nodes": list(body.get("nodes") or [])}
    if result["success"]:
        result["lineage"] = statement_lineage(statements)           # which tables it read and wrote: the data lineage (data_dependencies.py)
    return result
