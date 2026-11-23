"""FlinkSQL Runner proxy.

Same idea as dbt_proxy.py: the SPA talks to a single origin (workspace
service), and this router forwards FlinkSQL Runner Jobs API calls through the
backend so the browser never reaches the runner's ClusterIP directly.

Proxy scope (the runner's Jobs API):
  GET    /api/v1/flink/jobs                 → list jobs for this org
  POST   /api/v1/flink/jobs                 → create a job
  GET    /api/v1/flink/jobs/{id}            → get one job
  PATCH  /api/v1/flink/jobs/{id}            → update a job
  DELETE /api/v1/flink/jobs/{id}            → delete a job
  POST   /api/v1/flink/jobs/{id}/validate   → validate SQL
  POST   /api/v1/flink/jobs/{id}/deploy     → deploy to Flink
  POST   /api/v1/flink/jobs/{id}/stop       → stop (with savepoint)
  POST   /api/v1/flink/jobs/{id}/cancel     → cancel
  GET    /api/v1/flink/jobs/{id}/status     → live status

Git-backed job creation (this service, not a pass-through):
  POST   /api/v1/flink/jobs/from-git        → clone a repo, create the job from it
  POST   /api/v1/flink/jobs/{id}/sync       → re-read the repo, update the job SQL

The runner's own Jobs API only accepts inline `sql`, so — exactly like the dbt
Transformer's "register a project from a Git URL" flow — the workspace service
owns the repository side: it shallow-clones the repo, assembles the FlinkSQL,
and submits that to the runner. Provenance (url/ref/path/sha) is round-tripped
through the job's `description` so the UI can show it and resync later.

Contract: routes declare a ``response_model`` (app/schemas/proxy.py) so the
OpenAPI document describes the runner's real shapes; bodies are still
forwarded untouched (a returned ``Response`` skips response validation), so
an upstream addition can never fail the proxy (WS-FE-014).

Auth: the workspace JWT is forwarded as-is; the runner validates it against
the same JWKS (no re-minting). The runner's standalone /validate endpoint takes
the internal service token only, so /api/v1/flink/validate below attaches it
server-side rather than forwarding the caller's JWT.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.upstream import upstream_guard, upstream_timeout
from app.core.deps import DB
from app.core.workspace_scope import authorize_workspace, requested_workspace_id
from app.core.service_tokens import (
    FLINK_AUDIENCE, FLINK_SCOPE_JOBS_DEPLOY, FLINK_SCOPE_JOBS_EDIT, FLINK_SCOPE_JOBS_READ,
    FLINK_SCOPE_VALIDATE, delegated_authorization, post_with_service_token,
)
from app.core.errors import validation_failed
from app.git.safe_read import FileTooLarge, UnsafeRepoPath, ensure_safe_path, read_repo_file, walk_repo_files
from app.core.rbac import RequireFlinkDeploy, RequireFlinkEdit
from app.schemas.proxy import (
    FlinkDeployment,
    FlinkJob,
    FlinkJobCreate,
    FlinkJobLiveStatus,
    FlinkJobUpdate,
    FlinkUiOut,
    FlinkValidateResponse,
    json_body,
)

log = structlog.get_logger()
router = APIRouter(prefix="/api/v1/flink", tags=["flink-proxy"])


# Guard rails for the repo → SQL assembly step.
_MAX_SQL_FILES = 50
_MAX_SQL_BYTES = 1_000_000

# Conventional single-entrypoint filenames, tried in order before falling back
# to "concatenate every .sql file in the repo".
_ENTRYPOINT_CANDIDATES = (
    "job.flink.sql", "job.sql", "main.flink.sql", "main.sql",
    "flink.sql", "pipeline.sql",
)


def _auth(user, scope: str, workspace_id=None) -> str:
    """X-001: a short-lived delegated token for exactly this call, never the user's own bearer/cookie.
    `workspace_id` is a workspace the caller was verified to own (W-016); it rides in the signed token."""
    return delegated_authorization(user, audience=FLINK_AUDIENCE, scope=scope, workspace_id=workspace_id)


def _without_source(body: bytes) -> bytes:
    """Drop a client-supplied `source` from a job create/update body (X-004).

    `source` is the runner's record of where a job's SQL was imported from. Only the Git-import routes below set it,
    because only they actually cloned the repository; a user-supplied value on the pass-through routes would be a
    forged provenance in the audit record. Anything that is not a JSON object is forwarded untouched."""
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if isinstance(data, dict) and "source" in data:
        data.pop("source")
        return json.dumps(data).encode()
    return body


def _canonical_repo_url(url: str) -> str:
    """One spelling per repository: trimmed, lower-case scheme and host, no trailing slash or `.git`."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _source_for_runner(*, git_url: str, git_ref: str | None, requested_ref: str | None, sql_path: str, sha: str,
                       provider: str | None) -> dict:
    """The structured provenance sent to the runner with an imported job's SQL (X-004, W-015): canonical repository URL, the ref that was
    actually checked out, the ref the user asked for (None = the repository default), the path, the immutable commit and when it was
    imported. It travels as typed fields, never inside user-editable prose. The commit must be a real 40-character SHA: provenance is
    evidence, so a missing one fails the import rather than being guessed or dropped."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha or ""):
        raise validation_failed("git_url", "Could not determine the commit that was imported; refusing to record provenance without it")
    return {"git_url": _canonical_repo_url(git_url), "git_ref": git_ref or requested_ref or "HEAD", "requested_ref": requested_ref,
            "sql_path": sql_path, "commit_sha": sha, "provider": provider,
            "imported_at": datetime.now(timezone.utc).isoformat()}


async def _proxy(request: Request, path: str, user, scope: str, *, strip_source: bool = False,
                 workspace_id=None) -> Response:
    settings = get_settings()
    url = f"{settings.flinksql_runner_url}/api/v1/{path.lstrip('/')}"
    body = await request.body()
    if strip_source:
        body = _without_source(body)
    headers = {
        "Authorization": _auth(user, scope, workspace_id),
        "Content-Type": request.headers.get("content-type", "application/json"),
        "X-Forwarded-For": request.client.host if request.client else "unknown",
    }
    with upstream_guard("the FlinkSQL runner"):
        async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
            upstream = await client.request(
                method=request.method, url=url, content=body,
                headers=headers, params=dict(request.query_params),
            )
    log.info("flink_proxy", method=request.method, path=path, status=upstream.status_code)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


# ── Legacy provenance marker (read-only) ─────────────────────────────────────
# Jobs imported before provenance was a structured field carry it as a trailing "[flink-git] url=… ref=… path=… sha=…" line in their
# description. Nothing writes that line any more (W-015: provenance is the runner's typed `source`, not user-editable prose); it is only
# READ, for those older jobs, and removed from the description the next time such a job is synced.
_GIT_MARKER = "[flink-git]"
_GIT_LINE_RE = re.compile(r"^\[flink-git\][^\n]*$", re.MULTILINE)


def _strip_legacy_marker(description: str | None) -> str:
    return _GIT_LINE_RE.sub("", description or "").strip()


def _decode_legacy_provenance(description: str | None) -> dict[str, str]:
    """url/ref/path/sha from a legacy marker line. {} when absent."""
    if not description:
        return {}
    match = _GIT_LINE_RE.search(description)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for token in match.group(0)[len(_GIT_MARKER):].strip().split(" "):
        key, _, value = token.partition("=")
        if key and value:
            fields[key] = value
    return fields


def _job_provenance(job: dict) -> dict[str, str | None]:
    """The job's Git provenance: the runner's structured `source`, else (older jobs) the legacy description marker. {} if neither."""
    source = job.get("source")
    if isinstance(source, dict) and source.get("git_url"):
        return {"url": source["git_url"], "ref": source.get("requested_ref"), "branch": source.get("git_ref"),
                "path": source.get("sql_path"), "sha": source.get("commit_sha"), "provider": source.get("provider")}
    legacy = _decode_legacy_provenance(job.get("description"))
    return {"url": legacy.get("url"), "ref": legacy.get("ref"), "branch": legacy.get("ref"), "path": legacy.get("path"),
            "sha": legacy.get("sha"), "provider": None} if legacy else {}


# ── Repo → FlinkSQL assembly ─────────────────────────────────────────────────
def _read_sql_text(path: Path, repo_dir: Path) -> str:
    """Read one file of the clone — never through a symlink, never from outside it (W-012)."""
    try:
        data = read_repo_file(repo_dir, path, max_bytes=_MAX_SQL_BYTES)
    except UnsafeRepoPath as e:
        raise validation_failed("sql_path", f"'{path.relative_to(repo_dir)}' is not read: {e.reason}")
    except FileTooLarge:
        raise validation_failed("sql_path", f"{path.name} is larger than 1 MB")
    return data.decode("utf-8")


def _collect_sql(repo_dir: Path, sql_path: str | None) -> tuple[str, str]:
    """Assemble the job's FlinkSQL from a cloned repo.

    Returns ``(sql, resolved_path)``. ``resolved_path`` is what gets recorded as
    provenance so a later resync reads exactly the same thing:
      - an explicit ``sql_path`` (file or directory) is used as given
      - otherwise the first conventional entrypoint filename that exists
      - otherwise every ``.sql`` file in the repo, concatenated in path order
    """
    if sql_path:
        clean = sql_path.strip().strip("/")
        if ".." in clean.split("/"):
            raise validation_failed("sql_path", "Path may not contain '..'")
        target = repo_dir / clean
        if not os.path.lexists(target):
            raise validation_failed("sql_path", f"'{clean}' does not exist in the repository")
        try:
            ensure_safe_path(repo_dir, target)       # the same rule as the workspace import (app/git/safe_read.py)
        except UnsafeRepoPath as e:
            raise validation_failed("sql_path", f"'{clean}' is not read: {e.reason}")
        if target.is_file():
            return _read_sql_text(target, repo_dir), clean
        return _concat_sql_dir(target, repo_dir), clean

    for candidate in _ENTRYPOINT_CANDIDATES:
        target = repo_dir / candidate
        if target.is_file():
            return _read_sql_text(target, repo_dir), candidate

    return _concat_sql_dir(repo_dir, repo_dir), "."


def _concat_sql_dir(search_dir: Path, repo_dir: Path) -> str:
    """Concatenate every .sql file under search_dir, in stable path order."""
    files: list[Path] = []
    for entry in walk_repo_files(search_dir):        # the same walker, and verdicts, as the workspace import
        if not entry.path.name.lower().endswith(".sql"):
            continue
        if entry.rejected:
            raise validation_failed("git_url", f"'{entry.path.relative_to(repo_dir)}' is not read: {entry.rejected}")
        files.append(entry.path)
    files.sort(key=lambda p: str(p.relative_to(repo_dir)))
    if not files:
        raise validation_failed("git_url", "No .sql files found in the repository")
    if len(files) > _MAX_SQL_FILES:
        raise validation_failed(
            "git_url",
            f"Repository has {len(files)} .sql files (max {_MAX_SQL_FILES}) — "
            "point sql_path at a single file or folder",
        )
    parts: list[str] = []
    total = 0
    for path in files:
        text = _read_sql_text(path, repo_dir).strip()
        if not text:
            continue
        total += len(text)
        if total > _MAX_SQL_BYTES:
            raise validation_failed("git_url", "Combined SQL exceeds 1 MB")
        # Statements are split on ';' downstream — a file whose last statement
        # is unterminated would otherwise swallow the next file's first one.
        if not text.endswith(";"):
            text += ";"
        parts.append(f"-- source: {path.relative_to(repo_dir)}\n{text}")
    if not parts:
        raise validation_failed("git_url", "The repository's .sql files are all empty")
    return "\n\n".join(parts)


async def _fetch_sql_from_git(
    *, git_url: str, git_ref: str | None, sql_path: str | None, provider: str, user_id: str,
) -> tuple[str, str, str, str | None]:
    """Shallow-clone a repo and assemble its FlinkSQL.

    Returns ``(sql, resolved_path, commit_sha, branch)``: the commit and branch that were ACTUALLY checked out. `git_ref` given is a
    requirement — if it cannot be cloned the call fails (W-014); None clones the repository's default branch. The clone lands in a
    temp dir that is always removed, so nothing user-supplied outlives the request.

    Credentials come from the token this user already stored for the provider
    (see save_git_token / load_git_token) — job registration never asks for one.
    A public repo needs none.
    """
    from app.api.git import GitRefUnavailable, _do_clone, _redact, load_git_token
    from app.core.git_egress_policy import GitEgressPolicyError, assert_binding_url_allowed

    # Same egress rules as every other clone (WS-GIT-009): https only, an
    # allowlisted host, and that host must belong to `provider` — otherwise
    # this is a server-side fetch of an arbitrary URL, and the provider's
    # token would go to whatever host the request names.
    try:
        assert_binding_url_allowed(provider, git_url)
    except GitEgressPolicyError as e:
        raise validation_failed("git_url", f"Git URL not permitted: {e.reason}")

    token = await load_git_token(user_id, provider)
    dest = Path(tempfile.gettempdir()) / f"flink-git-{uuid4().hex}"
    try:
        try:
            # The token goes to the git subprocess via env (Authorization
            # header, see app/git/git_local.git_auth_env) — never into a URL
            # or .git/config (WS-GIT-009).
            cloned = await asyncio.to_thread(
                lambda: _do_clone(git_url, str(dest), git_ref, provider=provider, token=token)
            )
        except GitRefUnavailable as e:
            log.warning("flink_git_ref_unavailable", git_url=git_url, git_ref=git_ref, error=e.reason)
            raise validation_failed(
                "git_ref", f"Could not clone {git_url} @ {git_ref}: {e.reason}. The requested ref is not replaced by another branch.",
            )
        except Exception as e:  # noqa: BLE001 — surface as a 422, not a 500
            log.warning("flink_git_clone_failed", git_url=git_url, error=_redact(e, token))
            raise validation_failed(
                "git_url", f"Could not clone {git_url}: {_redact(e, token)}",
            )
        sql, resolved = await asyncio.to_thread(_collect_sql, dest, sql_path)
        return sql, resolved, cloned.commit_sha, cloned.branch
    finally:
        await asyncio.to_thread(shutil.rmtree, str(dest), True)


def _name_from_repo(git_url: str) -> str:
    """Derive a job name from the repo, the way dbt names a project."""
    tail = git_url.rstrip("/").split("/")[-1]
    stem = tail[:-4] if tail.endswith(".git") else tail
    slug = re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")
    return slug or "flink_job"


async def _runner_call(
    user, scope: str, method: str, path: str, json_body: dict | None = None, *, workspace_id=None,
) -> httpx.Response:
    """Call the runner as the caller (a delegated token for `scope`) with a JSON body we built."""
    settings = get_settings()
    headers = {"Authorization": _auth(user, scope, workspace_id)}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    with upstream_guard("the FlinkSQL runner"):
        async with httpx.AsyncClient(timeout=upstream_timeout()) as client:
            return await client.request(
                method=method,
                url=f"{settings.flinksql_runner_url}/api/v1/{path.lstrip('/')}",
                json=json_body,
                headers=headers,
            )


class FlinkJobFromGit(BaseModel):
    """Create a FlinkSQL job from a Git repository (mirrors dbt registration).

    No credential field: registration never asks for a token. A private repo is
    cloned with the PAT the user already saved via /git/.../token, the same
    credential push/pull uses.
    """

    git_url: str = Field(min_length=1)
    # Branch or tag to import; omit for the repository's default branch. A ref that is given must be cloneable (W-014): there is no
    # silent fallback to another branch.
    git_ref: str | None = None
    # File or folder inside the repo. Omit to auto-detect a conventional
    # entrypoint, else concatenate every .sql file in the repo.
    sql_path: str | None = None
    name: str | None = None
    description: str | None = None
    parallelism: int = Field(default=1, ge=1, le=512)
    sink_engine: str = "trino"
    workspace_id: UUID | None = None
    provider: str = "github"


class FlinkJobSync(BaseModel):
    """Re-read a Git-backed job's repo and push the new SQL to the runner."""

    git_ref: str | None = None
    provider: str | None = None


@router.get("/jobs", response_model=list[FlinkJob])
async def list_jobs(request: Request, user: RequireFlinkEdit) -> Response:
    return await _proxy(request, "jobs", user, FLINK_SCOPE_JOBS_READ)


@router.post(
    "/jobs", status_code=status.HTTP_201_CREATED,
    response_model=FlinkJob, openapi_extra=json_body(FlinkJobCreate),
)
async def create_job(request: Request, user: RequireFlinkEdit, db: DB) -> Response:
    # W-016 / FL-003: a workspace is attached only if the caller owns it, and the verified id travels in the signed token.
    workspace_id = await authorize_workspace(db, user, requested_workspace_id(await request.body()))
    return await _proxy(request, "jobs", user, FLINK_SCOPE_JOBS_EDIT, strip_source=True, workspace_id=workspace_id)


@router.post("/jobs/from-git", status_code=status.HTTP_201_CREATED, response_model=FlinkJob)
async def create_job_from_git(
    payload: FlinkJobFromGit, request: Request, user: RequireFlinkEdit, db: DB,
) -> Response:
    """Clone the repo, assemble its FlinkSQL, and create the job upstream."""
    workspace_id = await authorize_workspace(db, user, payload.workspace_id)      # before any clone or network call
    requested_ref = (payload.git_ref or "").strip() or None
    sql, resolved_path, sha, branch = await _fetch_sql_from_git(
        git_url=payload.git_url,
        git_ref=requested_ref,
        sql_path=payload.sql_path,
        provider=payload.provider,
        user_id=str(user.user_id),
    )
    body = {
        "name": payload.name.strip() if payload.name and payload.name.strip()
        else _name_from_repo(payload.git_url),
        # The user's own words, untouched: provenance is the structured `source`, never encoded into the description (W-015).
        "description": payload.description,
        "sql": sql,
        "parallelism": payload.parallelism,
        "sink_engine": payload.sink_engine,
        "source": _source_for_runner(git_url=payload.git_url, git_ref=branch, requested_ref=requested_ref,
                                     sql_path=resolved_path, sha=sha, provider=payload.provider),
    }
    if workspace_id:
        body["workspace_id"] = str(workspace_id)

    upstream = await _runner_call(user, FLINK_SCOPE_JOBS_EDIT, "POST", "jobs", body, workspace_id=workspace_id)
    log.info(
        "flink_job_from_git",
        git_url=payload.git_url, git_ref=branch, requested_ref=requested_ref,
        sql_path=resolved_path, sha=sha[:8], status=upstream.status_code,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@router.post("/jobs/{job_id}/sync", response_model=FlinkJob)
async def sync_job_from_git(
    job_id: UUID, payload: FlinkJobSync, request: Request, user: RequireFlinkEdit,
) -> Response:
    """Pull the job's repo again and PATCH the runner with the latest SQL."""
    current = await _runner_call(user, FLINK_SCOPE_JOBS_READ, "GET", f"jobs/{job_id}")
    if current.status_code >= 400:
        return Response(
            content=current.content,
            status_code=current.status_code,
            media_type=current.headers.get("content-type", "application/json"),
        )
    job = current.json()
    provenance = _job_provenance(job)
    git_url = provenance.get("url")
    if not git_url:
        raise validation_failed(
            "job_id", "This job was not created from Git — nothing to sync",
        )
    # The ref the user ORIGINALLY asked for (None = the repository default, which then follows the default branch), unless this call names
    # one. A requested ref that is gone fails the sync; it is never replaced by another branch (W-014).
    requested_ref = (payload.git_ref or "").strip() or provenance.get("ref") or None
    stored_path = provenance.get("path")
    sql_path = None if stored_path in (None, "", ".") else stored_path
    provider = payload.provider or provenance.get("provider") or "github"

    sql, resolved_path, sha, branch = await _fetch_sql_from_git(
        git_url=git_url,
        git_ref=requested_ref,
        sql_path=sql_path,
        provider=provider,
        user_id=str(user.user_id),
    )
    patch: dict[str, Any] = {
        "sql": sql,
        # Older jobs carried provenance in a description marker; it is dropped here, the user's own text kept (W-015).
        "source": _source_for_runner(git_url=git_url, git_ref=branch, requested_ref=requested_ref,
                                     sql_path=resolved_path, sha=sha, provider=provider),
    }
    if _GIT_LINE_RE.search(job.get("description") or ""):
        patch["description"] = _strip_legacy_marker(job.get("description"))
    upstream = await _runner_call(user, FLINK_SCOPE_JOBS_EDIT, "PATCH", f"jobs/{job_id}", patch)
    log.info("flink_job_synced", job_id=str(job_id), sha=sha[:8], status=upstream.status_code)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@router.get("/jobs/{job_id}", response_model=FlinkJob)
async def get_job(job_id: UUID, request: Request, user: RequireFlinkEdit) -> Response:
    return await _proxy(request, f"jobs/{job_id}", user, FLINK_SCOPE_JOBS_READ)


@router.patch(
    "/jobs/{job_id}", response_model=FlinkJob, openapi_extra=json_body(FlinkJobUpdate),
)
async def update_job(job_id: UUID, request: Request, user: RequireFlinkEdit) -> Response:
    return await _proxy(request, f"jobs/{job_id}", user, FLINK_SCOPE_JOBS_EDIT, strip_source=True)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: UUID, request: Request, user: RequireFlinkEdit) -> Response:
    return await _proxy(request, f"jobs/{job_id}", user, FLINK_SCOPE_JOBS_EDIT)


@router.post("/jobs/{job_id}/validate", response_model=FlinkJob)
async def validate_job(job_id: UUID, request: Request, user: RequireFlinkEdit) -> Response:
    return await _proxy(request, f"jobs/{job_id}/validate", user, FLINK_SCOPE_JOBS_EDIT)


@router.post("/jobs/{job_id}/deploy", status_code=status.HTTP_202_ACCEPTED, response_model=FlinkDeployment)
async def deploy_job(job_id: UUID, request: Request, user: RequireFlinkDeploy) -> Response:
    return await _proxy(request, f"jobs/{job_id}/deploy", user, FLINK_SCOPE_JOBS_DEPLOY)


@router.post("/jobs/{job_id}/stop", status_code=status.HTTP_202_ACCEPTED, response_model=FlinkDeployment)
async def stop_job(job_id: UUID, request: Request, user: RequireFlinkDeploy) -> Response:
    return await _proxy(request, f"jobs/{job_id}/stop", user, FLINK_SCOPE_JOBS_DEPLOY)


@router.post("/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED, response_model=FlinkDeployment)
async def cancel_job(job_id: UUID, request: Request, user: RequireFlinkDeploy) -> Response:
    return await _proxy(request, f"jobs/{job_id}/cancel", user, FLINK_SCOPE_JOBS_DEPLOY)


@router.get("/jobs/{job_id}/status", response_model=FlinkJobLiveStatus)
async def job_status(job_id: UUID, request: Request, user: RequireFlinkEdit) -> Response:
    return await _proxy(request, f"jobs/{job_id}/status", user, FLINK_SCOPE_JOBS_READ)


@router.get("/ui", response_model=FlinkUiOut)
async def flink_ui(user: RequireFlinkEdit) -> FlinkUiOut:
    """Where the browser can reach the Flink Web UI.

    The runner's own flink_rest_url is an in-cluster address the browser can't
    resolve, so the externally reachable base is configured separately. An empty
    string means the SPA hides its "View logs" action.
    """
    return FlinkUiOut(url=get_settings().flink_ui_url.rstrip("/"))


class FlinkSqlValidate(BaseModel):
    """Ad-hoc syntax check for SQL being authored in the FlinkSQL editor."""

    sql: str = Field(min_length=1)
    workspace_id: UUID | None = None


@router.post("/validate", response_model=FlinkValidateResponse)
async def validate_sql(
    payload: FlinkSqlValidate, user: RequireFlinkEdit, db: DB,
) -> Response:
    """Validate FlinkSQL without creating a job.

    The runner's /validate takes the internal service token and never a user
    JWT, so this is not a pass-through: the caller is authenticated here and the
    service token is attached server-side.
    """
    settings = get_settings()
    if not settings.flinksql_internal_token:
        raise validation_failed(
            "sql", "FlinkSQL validation is not configured on this deployment",
        )
    body: dict = {"sql": payload.sql}
    workspace_id = await authorize_workspace(db, user, payload.workspace_id)
    if workspace_id:
        body["workspace_id"] = str(workspace_id)
    with upstream_guard("the FlinkSQL runner"):
        async with httpx.AsyncClient(timeout=upstream_timeout(slow=True)) as client:
            # The runner verifies an org-bound JWT signed with its
            # INTERNAL_SERVICE_TOKEN (== FLINKSQL_INTERNAL_TOKEN here).
            upstream = await post_with_service_token(
                client, f"{settings.flinksql_runner_url}/api/v1/validate",
                json=body,
                signing_secret=settings.flinksql_internal_token,
                audience=FLINK_AUDIENCE, scope=FLINK_SCOPE_VALIDATE,
                legacy_token=settings.flinksql_internal_token,
            )
    log.info("flink_validate", status=upstream.status_code)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )
