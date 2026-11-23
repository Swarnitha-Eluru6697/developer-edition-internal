"""Projects / runs / lineage API.

Endpoint shapes match Workspace TDD 4.3.3 exactly:
  POST   /api/v1/projects/register
  POST   /api/v1/projects/{id}/run
  GET    /api/v1/projects/{id}/runs
  GET    /api/v1/projects/{id}/lineage

Plus endpoints the TDD implies but doesn't enumerate (list/get a single
project, run detail, run events for the 'recent runs' panel), and the
internal-only POST /api/v1/validate used by Workspace's codegen chat.

Workspace and dbt Runner are intentionally decoupled (TDD 4.3.3) — every
endpoint here works identically whether the caller is Workspace, an
external CI system, or a future Agents/Workflow IDE surface. Auth is the
same JWT the rest of the platform uses; org scoping comes from the
validated token, never from a client-supplied org_id.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.core.deps import (
    DB, SCOPE_PROJECTS_READ, SCOPE_PROJECTS_WRITE, SCOPE_RUNS_READ, SCOPE_RUNS_TRIGGER, InternalCaller, ProjectsRead,
    ProjectsWrite, RunsRead, RunsTrigger, allowed_kinds, ensure_job_permission,
)
from app.core.config import get_settings
from app.core.errors import not_found, validation_failed
from app.core.git_credentials import token_for
from app.core.workspace_scope import authorize_workspace
from app.core.service_auth import require_scheduler_scope, require_service_scope
from app.models import Run
from app.schemas.project import (
    RunCancelByKey,
    LineageEdgeOut, ProjectHeadOut, ProjectLineageOut, ProjectOut, ProjectRegister, ProjectUpdate,
    RunEventOut, RunManifestOut, RunOut, RunTriggerRequest, SchemaTestsPayload,
    ValidateRequest, ValidateResponse,
    LastRunOut,
)
from app.services.git_service import get_git_service
from app.services import run_manifest
from app.services import data_dependencies, dependents
from app.services.project_service import ProjectService
from app.services.run_service import RunService, _git_error
from app.services.validate_service import validate_model as run_validate

router = APIRouter(prefix="/api/v1", tags=["projects"])


async def _project_out(db, org_id: str, projects: list, *, with_last_run: bool = False) -> list[ProjectOut]:
    """ProjectOut for each project, with its upstream and downstream links (and, for the list, its last run)."""
    ids = [p.id for p in projects]
    latest = await ProjectService(db).latest_runs(ids) if with_last_run else {}
    links = await dependents.dependents_of(db, ids)
    above = await dependents.upstreams_of(db, ids)
    upstream = await data_dependencies.data_upstream(db, org_id)
    out: list[ProjectOut] = []
    for p in projects:
        model = ProjectOut.model_validate(p)
        run = latest.get(p.id)
        if run is not None:
            model.last_run = LastRunOut.model_validate(run, from_attributes=True)
        model.dependent_project_ids = links.get(p.id, [])
        model.upstream_project_ids = above.get(p.id, [])
        model.data_upstream_project_ids = upstream.get(p.id, [])
        out.append(model)
    return out


async def _check_project_exists(payload: ProjectRegister) -> None:
    """Reject a registration whose repo can't be read or whose project name
    (folder) isn't a dbt project in it — with the names that do exist."""
    settings = get_settings()
    if not settings.validate_project_on_register:
        return
    if payload.kind in ("sql", "python"):
        await _check_job_file_exists(payload)
        return
    try:
        found = await asyncio.wait_for(
            asyncio.to_thread(get_git_service().list_dbt_projects,
                              git_url=payload.git_url, git_ref=payload.git_ref, token=token_for(payload.credential_ref)),
            timeout=settings.git_clone_timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — git errors can echo a credentialed URL; say nothing of them
        raise validation_failed(
            "git_url",
            "Could not read the repository. Check the URL and branch, and that the project "
            "has been pushed to Git.",
        )
    wanted = payload.project_path or ""
    if wanted in found or (not wanted and len(found) == 1):
        return
    names = ", ".join(f or "." for f in found[:10])
    if not found:
        raise validation_failed("git_url", "No dbt project (dbt_project.yml) found in this repository.")
    if wanted:
        raise validation_failed(
            "project_path",
            f"Project name '{wanted}' was not found in the repository. "
            f"Available dbt projects: {names}.",
        )
    raise validation_failed(
        "project_path",
        f"This repository has several dbt projects ({names}). Enter the project name to register.",
    )


async def _check_job_file_exists(payload: ProjectRegister) -> None:
    """A SQL or Python job's file must be in the repository at its branch (checked before anything is stored)."""
    settings = get_settings()
    try:
        exists = await asyncio.wait_for(
            asyncio.to_thread(get_git_service().file_exists, git_url=payload.git_url, git_ref=payload.git_ref,
                              path=payload.project_path or "", token=token_for(payload.credential_ref)),
            timeout=settings.git_clone_timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — git errors can echo a credentialed URL; say nothing of them
        raise validation_failed(
            "git_url", "Could not read the repository. Check the URL and branch, and that the file has been pushed to Git.")
    if not exists:
        raise validation_failed("project_path", f"File '{payload.project_path}' was not found in the repository at "
                                                f"'{payload.git_ref}'. Push it first, and give its path from the repo root.")


@router.post("/projects/register", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def register_project(payload: ProjectRegister, user: ProjectsWrite, db: DB) -> ProjectOut:
    ensure_job_permission(user, SCOPE_PROJECTS_WRITE, payload.kind)              # each job kind has its own edit permission
    payload.workspace_id = authorize_workspace(user, payload.workspace_id)       # DBT-012: a label Workspace vouched for
    await _check_project_exists(payload)
    svc = ProjectService(db)
    project = await svc.register(org_id=user.org_id, user_id=user.user_id, request=payload)
    return (await _project_out(db, user.org_id, [project]))[0]


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    user: ProjectsRead, db: DB, workspace_id: UUID | None = Query(None),
    kind: Literal["dbt", "sql", "python"] | None = Query(None),
) -> list[ProjectOut]:
    svc = ProjectService(db)
    if kind is not None:
        ensure_job_permission(user, SCOPE_PROJECTS_READ, kind)
    visible = set(allowed_kinds(user, SCOPE_PROJECTS_READ))     # only the kinds the caller holds a permission for
    projects = [p for p in await svc.list_for_org(user.org_id, workspace_id=workspace_id, kind=kind) if p.kind in visible]
    return await _project_out(db, user.org_id, projects, with_last_run=True)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: UUID, user: ProjectsRead, db: DB) -> ProjectOut:
    svc = ProjectService(db)
    project = await svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_PROJECTS_READ, project.kind)
    return (await _project_out(db, user.org_id, [project]))[0]


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(project_id: UUID, payload: ProjectUpdate, user: ProjectsWrite, db: DB) -> ProjectOut:
    svc = ProjectService(db)
    ensure_job_permission(user, SCOPE_PROJECTS_WRITE, (await svc.get(user.org_id, project_id)).kind)
    project = await svc.update(user.org_id, project_id, payload)
    return (await _project_out(db, user.org_id, [project]))[0]


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: UUID, user: ProjectsWrite, db: DB) -> None:
    svc = ProjectService(db)
    ensure_job_permission(user, SCOPE_PROJECTS_WRITE, (await svc.get(user.org_id, project_id)).kind)
    await svc.delete(user.org_id, project_id)


@router.post("/projects/{project_id}/run", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(
    project_id: UUID, payload: RunTriggerRequest, user: RunsTrigger, db: DB,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> RunOut:
    if payload.git_sha:
        # DG-007: pinning a commit is the scheduler's (Dagster's) ability, not a user's: a user must not be able to
        # run a commit that is not on the project's ref. Refuse rather than silently build the tip instead.
        raise HTTPException(status_code=422, detail={
            "code": "GIT_SHA_NOT_ACCEPTED",
            "message": "git_sha is only accepted from the scheduler; the run builds the project's ref"})
    if payload.scheduled_fire_time:
        raise HTTPException(status_code=422, detail={
            "code": "SCHEDULED_FIRE_TIME_NOT_ACCEPTED",
            "message": "scheduled_fire_time is only accepted from the scheduler"})
    project_svc = ProjectService(db)
    run_svc = RunService(db)
    project = await project_svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_RUNS_TRIGGER, project.kind)
    run = await run_svc.submit(
        project=project, trigger="manual", triggered_by_user_id=user.user_id, request=payload,
        subject_id=user.user_id, idempotency_key=idempotency_key or None,
    )
    return RunOut.model_validate(run)


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
async def list_runs(
    project_id: UUID, user: RunsRead, db: DB, limit: int = Query(50, le=200),
) -> list[RunOut]:
    """Surfaced in Workspace's 'recent runs' panel by polling (TDD 4.3.1)."""
    project_svc = ProjectService(db)
    run_svc = RunService(db)
    project = await project_svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_RUNS_READ, project.kind)
    runs = await run_svc.list_for_project(project, limit=limit)
    return [RunOut.model_validate(r) for r in runs]


@router.get("/projects/{project_id}/runs/{run_id}", response_model=RunOut)
async def get_run(project_id: UUID, run_id: UUID, user: RunsRead, db: DB) -> RunOut:
    project_svc = ProjectService(db)
    run_svc = RunService(db)
    project = await project_svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_RUNS_READ, project.kind)
    run = await run_svc.get(project, run_id)
    return RunOut.model_validate(run)


@router.get("/projects/{project_id}/runs/{run_id}/manifest", response_model=RunManifestOut)
async def get_run_manifest(project_id: UUID, run_id: UUID, user: RunsRead, db: DB) -> RunManifestOut:
    """The audit record of what this run executed: commit, DQ artifact versions and content hashes (DBT-006/007)."""
    project = await ProjectService(db).get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_RUNS_READ, project.kind)
    run = await RunService(db).get(project, run_id)
    return RunManifestOut.model_validate(run)


@router.post("/projects/{project_id}/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(project_id: UUID, run_id: UUID, user: RunsTrigger, db: DB) -> RunOut:
    project_svc = ProjectService(db)
    run_svc = RunService(db)
    project = await project_svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_RUNS_TRIGGER, project.kind)
    run = await run_svc.cancel(project, run_id)
    return RunOut.model_validate(run)


@router.get("/projects/{project_id}/runs/{run_id}/events", response_model=list[RunEventOut])
async def list_run_events(project_id: UUID, run_id: UUID, user: RunsRead, db: DB) -> list[RunEventOut]:
    project_svc = ProjectService(db)
    run_svc = RunService(db)
    project = await project_svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_RUNS_READ, project.kind)
    run = await run_svc.get(project, run_id)
    events = await run_svc.list_events(run)
    return [RunEventOut.model_validate(e) for e in events]


@router.get("/projects/{project_id}/lineage", response_model=ProjectLineageOut)
async def get_lineage(project_id: UUID, user: ProjectsRead, db: DB) -> ProjectLineageOut:
    """Project-level dataset lineage, surfaced as a small graph in
    Workspace (TDD 4.3.3)."""
    project_svc = ProjectService(db)
    run_svc = RunService(db)
    project = await project_svc.get(user.org_id, project_id)
    ensure_job_permission(user, SCOPE_PROJECTS_READ, project.kind)
    edges = await run_svc.list_lineage(project)
    return ProjectLineageOut(
        project_id=project.id,
        edges=[LineageEdgeOut.model_validate(e) for e in edges],
    )


def _installed_dbt_version() -> str:
    try:
        from importlib.metadata import version
        return version("dbt-core")
    except Exception:  # noqa: BLE001
        return "0"


def _ver(v: str) -> tuple[int, ...]:
    import re
    parts = [int(m.group(0)) for m in (re.match(r"\d+", p) for p in str(v).split(".")[:3]) if m]
    return tuple(parts + [0] * (3 - len(parts)))


def check_engine_requirements(req: dict | None, installed: str) -> dict | None:
    """None if this runner can run an artifact with these requirements."""
    if not req:
        return None
    if req.get("engine") not in (None, "dbt-core"):
        return {"code": "ENGINE_INCOMPATIBLE", "message": f"artifact targets {req.get('engine')!r}",
                "runner_engine": "dbt-core", "runner_engine_version": installed, "required": req}
    lo, hi = req.get("min"), req.get("max_exclusive")
    v = _ver(installed)
    if (lo and v < _ver(lo)) or (hi and v >= _ver(hi)):
        return {"code": "ENGINE_INCOMPATIBLE",
                "message": f"artifact requires dbt-core >={lo},<{hi}; this runner has {installed}",
                "runner_engine": "dbt-core", "runner_engine_version": installed, "required": req}
    return None


@router.post(
    "/projects/{project_id}/schema-tests",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def inject_schema_tests(
    project_id: UUID, payload: SchemaTestsPayload, db: DB,
    _claims: Annotated[dict, Depends(require_service_scope("dbt.schema-tests.write"))],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> None:
    """Store a DQ-rendered schema.yml block for this project.

    Auth (RUN-DBT-005): short-lived JWT signed with SERVICE_JWT_SECRET,
    issuer/subject allowlisted, org-bound, bound to THIS project_id,
    single-use jti — see app.core.service_auth.

    Body: ApplyDqArtifact (artifact_version_id, binding_id, project_id,
    org_id, target_model, dbt_schema_fragment, idempotency_key, ...) or the
    legacy {target_table, yaml_content}. The body's project_id/org_id must
    match the path / token, the Idempotency-Key header must match the body's
    key, and the artifact's engine_requirements must be satisfiable by this
    runner's dbt-core. Re-sending the same key + artifact is a no-op.
    """
    if problem := payload.shape_problem():
        raise HTTPException(status_code=422, detail={"code": "INVALID_ARTIFACT", "message": problem})
    if payload.project_id is not None and payload.project_id != project_id:
        raise HTTPException(status_code=422, detail={"code": "PROJECT_MISMATCH",
                                                     "message": "body project_id != path"})
    if payload.org_id is not None and payload.org_id != _claims.get("org_id"):
        raise HTTPException(status_code=403, detail={"code": "ORG_MISMATCH",
                                                     "message": "body org_id != token org_id"})
    if idempotency_key and payload.idempotency_key and idempotency_key != payload.idempotency_key:
        raise HTTPException(status_code=422, detail={"code": "IDEMPOTENCY_KEY_MISMATCH",
                                                     "message": "Idempotency-Key header != body"})
    incompatible = check_engine_requirements(payload.engine_requirements, _installed_dbt_version())
    if incompatible:
        raise HTTPException(status_code=422, detail=incompatible)
    meta = None
    key = payload.idempotency_key or idempotency_key
    if payload.dbt_schema_fragment is not None or key:
        from datetime import datetime, timezone
        meta = {"artifact_version_id": payload.artifact_version_id,
                "binding_id": payload.binding_id, "idempotency_key": key,
                "content_hash": payload.content_hash,
                "renderer_version": payload.renderer_version,
                "applied_at": datetime.now(timezone.utc).isoformat()}
    await ProjectService(db).store_schema_tests(project_id, payload.model_key, payload.yaml_text,
                                                meta=meta)


# ---------------------------------------------------------------------
# Internal-only: Workspace's codegen chat validate loop. Narrow scope — the
# static INTERNAL_SERVICE_TOKEN can validate candidate code but cannot register, enumerate or run a
# project (see app/core/deps.py); the scheduler routes below use a separate scoped identity.
# ---------------------------------------------------------------------
@router.post("/validate", response_model=ValidateResponse)
async def validate_candidate(payload: ValidateRequest, _: InternalCaller) -> ValidateResponse:
    return await run_validate(payload)


# ---------------------------------------------------------------------
# Internal-only: the Dagster orchestrator (dbt-runner-dagster repo). It has no user JWT, so it
# authenticates with its OWN scoped service identity (DBT-003): a short-lived JWT signed with
# SCHEDULER_JWT_SECRET, one scope per route. The static INTERNAL_SERVICE_TOKEN is NOT accepted here.
# Scoped to this deployment's own org (get_internal). Kept under /internal so these routes can never
# shadow the user-facing /projects/{id} routes above.
# ---------------------------------------------------------------------
SchedulerProjectsRead = Annotated[dict, Depends(require_scheduler_scope("dbt.projects.read"))]
SchedulerRunsRead = Annotated[dict, Depends(require_scheduler_scope("dbt.runs.read"))]
SchedulerRunsTrigger = Annotated[dict, Depends(require_scheduler_scope("dbt.runs.trigger"))]
# DG-017: stopping a run is its own scope, so a token that may only start runs cannot stop them and vice versa.
SchedulerRunsCancel = Annotated[dict, Depends(require_scheduler_scope("dbt.runs.cancel"))]


@router.get("/internal/projects", response_model=list[ProjectOut])
async def internal_list_projects(_: SchedulerProjectsRead, db: DB) -> list[ProjectOut]:
    """Every project of this org, with its data upstream (Dagster draws them as asset lineage)."""
    projects = await ProjectService(db).list_all()
    upstream = await data_dependencies.data_upstream(db, get_settings().org_id)
    ids = [p.id for p in projects]
    links, above = await dependents.dependents_of(db, ids), await dependents.upstreams_of(db, ids)
    out = []
    for p in projects:
        model = ProjectOut.model_validate(p)
        model.data_upstream_project_ids = upstream.get(p.id, [])
        model.dependent_project_ids, model.upstream_project_ids = links.get(p.id, []), above.get(p.id, [])
        out.append(model)
    return out


@router.get("/internal/projects/scheduled", response_model=list[ProjectOut])
async def internal_list_scheduled(_: SchedulerProjectsRead, db: DB) -> list[ProjectOut]:
    projects = await ProjectService(db).list_scheduled()
    return [ProjectOut.model_validate(p) for p in projects]


@router.get("/internal/projects/git-triggered", response_model=list[ProjectOut])
async def internal_list_git_triggered(_: SchedulerProjectsRead, db: DB) -> list[ProjectOut]:
    """DG-008: the projects whose commits Dagster's git sensor watches — their own flag, not the cron schedule's. Declared before
    /internal/projects/{project_id} so the path is not taken for an id."""
    projects = await ProjectService(db).list_git_triggered()
    return [ProjectOut.model_validate(p) for p in projects]


@router.get("/internal/projects/{project_id}/head", response_model=ProjectHeadOut)
async def internal_project_head(project_id: UUID, _: SchedulerProjectsRead, db: DB) -> ProjectHeadOut:
    """DG-009: the commit a project's ref points to now, resolved BY THE RUNNER — the one component that holds repository credentials
    (`credential_ref`) and has the Git egress. Dagster's git sensor asks here instead of running `git ls-remote` itself, so a private
    repository works the same in every environment and Dagster needs no Git credentials or Git egress.

    The same resolver the run submission uses (exact ref match, a branch beats a tag, annotated tags peeled, a full SHA as is). 422
    GIT_REF_NOT_FOUND / 502 GIT_UNREACHABLE, like a submission."""
    project = await ProjectService(db).get_internal(project_id)
    git_url, git_ref, token = project.git_url, project.git_ref, token_for(project.credential_ref)
    await db.rollback()                         # end the read transaction: ls-remote can take seconds, and must not hold a connection
    try:
        sha = await run_manifest.resolve_commit(git_url, git_ref, token)      # by module attribute: tests replace it
    except run_manifest.GitRefError as e:
        raise _git_error(e)
    return ProjectHeadOut(project_id=project_id, git_ref=git_ref, sha=sha)


@router.get("/internal/projects/{project_id}", response_model=ProjectOut)
async def internal_get_project(project_id: UUID, _: SchedulerProjectsRead, db: DB) -> ProjectOut:
    """One project with its upstream and downstream links: Dagster's start check reads them (the dependency rule lives there)."""
    project = await ProjectService(db).get_internal(project_id)
    model = ProjectOut.model_validate(project)
    model.dependent_project_ids = (await dependents.dependents_of(db, [project.id])).get(project.id, [])
    model.upstream_project_ids = (await dependents.upstreams_of(db, [project.id])).get(project.id, [])
    return model


@router.post("/internal/projects/{project_id}/run", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def internal_trigger_run(
    project_id: UUID, payload: RunTriggerRequest, claims: SchedulerRunsTrigger, db: DB,
    trigger: Literal["scheduled", "api", "manual"] = Query("scheduled"),
    triggered_by: Annotated[str | None, Query(max_length=255)] = None,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> RunOut:
    """Dagster submits a run. `trigger`: "scheduled" (cron and new commits, the default) or "manual" (someone clicked Run now, or launched the
    job by hand: `triggered_by` then names who, as the Workspace's own Run now does); "api" stays for other callers."""
    project = await ProjectService(db).get_internal(project_id)
    run = await RunService(db).submit(
        project=project, trigger=trigger, triggered_by_user_id=(triggered_by or None) if trigger == "manual" else None, request=payload,
        subject_id=str(claims.get("sub") or "scheduler"), idempotency_key=idempotency_key or None,
    )
    return RunOut.model_validate(run)


@router.post("/internal/runs/{run_id}/cancel", response_model=RunOut)
async def internal_cancel_run(run_id: UUID, _: SchedulerRunsCancel, db: DB) -> RunOut:
    """DG-017: the orchestrator stops a run it started (a terminated Dagster run must not leave the dbt Job running).
    The same DBT-005 transition as the user route: queued -> cancelled, running with a Job -> cancelling -> cancelled
    only once the cluster confirms. A finished run is a 409; an unknown one (or another org's) a 404."""
    run = await db.get(Run, run_id)
    if run is None:
        raise not_found("run", str(run_id))
    project = await ProjectService(db).get_internal(run.project_id)  # same org boundary as the other internal routes
    return RunOut.model_validate(await RunService(db).cancel(project, run_id))


@router.post("/internal/projects/{project_id}/runs/cancel", response_model=RunOut)
async def internal_cancel_run_by_key(
    project_id: UUID, payload: RunCancelByKey, _: SchedulerRunsCancel, db: DB,
) -> RunOut:
    """DG-017: cancel the run created under this Idempotency-Key, for a submission whose answer the orchestrator never
    saw (it was terminated while the request was in flight, so it has no run id). 404 if no run has that key (yet)."""
    project = await ProjectService(db).get_internal(project_id)
    run = await RunService(db)._run_by_key(project.id, payload.idempotency_key)
    if run is None:
        raise not_found("run", payload.idempotency_key)
    return RunOut.model_validate(await RunService(db).cancel(project, run.id))


@router.get("/internal/runs/{run_id}", response_model=RunOut)
async def internal_get_run(run_id: UUID, _: SchedulerRunsRead, db: DB) -> RunOut:
    run = await db.get(Run, run_id)
    if run is None:
        raise not_found("run", str(run_id))
    await ProjectService(db).get_internal(run.project_id)  # same org boundary as get_internal
    return RunOut.model_validate(run)
