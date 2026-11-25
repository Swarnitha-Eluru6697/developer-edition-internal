"""Project service. Implements POST /api/v1/projects/register and friends
(TDD 4.3.3). The registry is org-shared; workspace_id is an optional grouping label vouched for by Workspace (DBT-012),
so an external CI system can register a project too."""
from __future__ import annotations

import re
import uuid

from fastapi import HTTPException
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.errors import conflict, flush_or_conflict, not_found, validation_failed
from app.core.git_credentials import REF_PATTERN, token_for
from app.models import Project, Run
from app.schemas.project import ProjectRegister, ProjectUpdate
from app.services import dependents


def _default_name(git_url: str, project_path: str = "") -> str:
    match = re.search(r"/([^/]+?)(\.git)?/?$", git_url)
    repo = match.group(1) if match else git_url
    return f"{repo}/{project_path}" if project_path else repo


def _assert_credential_known(ref: str | None) -> None:
    """A project may only name a credential the operator provisioned for this runner (DBT-009): a typo or a guess is refused up
    front, instead of failing every run later."""
    if ref and (not REF_PATTERN.match(ref) or token_for(ref) is None):
        raise validation_failed("credential_ref", f"credential '{ref}' is not configured on this runner (GIT_CREDENTIAL_{ref.upper()})")


class ProjectService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def register(
        self, *, org_id: str, user_id: str, request: ProjectRegister,
    ) -> Project:
        existing = await self._db.execute(
            select(Project).where(
                Project.org_id == org_id,
                Project.git_url == request.git_url,
                Project.git_ref == request.git_ref,
                Project.project_path == (request.project_path or ""),
            )
        )
        if existing.scalar_one_or_none():
            where = f" (folder {request.project_path})" if request.project_path else ""
            raise conflict(f"Project already registered for {request.git_url}@{request.git_ref}{where}",
                           code="PROJECT_ALREADY_REGISTERED", resource="project", field="git_url+git_ref+project_path",
                           details={"git_url": request.git_url, "git_ref": request.git_ref, "project_path": request.project_path or ""})

        _assert_credential_known(request.credential_ref)
        project = Project(
            org_id=org_id,
            workspace_id=request.workspace_id,
            registered_by_user_id=user_id,
            name=request.name or _default_name(request.git_url, request.project_path or ""),
            git_url=request.git_url,
            credential_ref=request.credential_ref,
            git_ref=request.git_ref,
            project_path=request.project_path or "",
            engine=request.engine,
            kind=request.kind,
            connection_id=request.connection_id,
            schedule_cron=request.schedule_cron,
            schedule_enabled=request.schedule_cron is not None,
            git_trigger_enabled=request.git_trigger_enabled,
        )
        self._db.add(project)
        await self._db.flush()
        if request.upstream_project_ids and request.dependent_project_ids:
            await dependents.set_links(self._db, project, request.upstream_project_ids, request.dependent_project_ids)
        elif request.upstream_project_ids:
            await dependents.set_upstreams(self._db, project, request.upstream_project_ids)
        elif request.dependent_project_ids:
            await dependents.set_dependents(self._db, project, request.dependent_project_ids)
        return project

    async def get(self, org_id: str, project_id: uuid.UUID) -> Project:
        project = await self._db.get(Project, project_id)
        if not project or project.org_id != org_id:
            raise not_found("project", str(project_id))
        return project

    async def get_internal(self, project_id: uuid.UUID) -> Project:
        """Fetch a project for internal service-to-service calls (RUN-DBT-005):
        authenticated with a scoped service JWT (app.core.service_auth) rather
        than a user JWT, so there is no caller-supplied org_id to check against
        the way get() does. Authorizes against this deployment's own org
        instead — every project in a given data plane belongs to that one org
        (TDD 1.3's single-tenant-per-data-plane model) — so a project_id that
        somehow belongs to a different org is rejected exactly like get()
        rejects one outside the caller's org, rather than trusting that mere
        possession of a validly-scoped token authorizes any project_id in the
        table. Same 404 (not 403) as get(): existence of a project outside
        this boundary is not something to confirm to the caller either."""
        settings = get_settings()
        project = await self._db.get(Project, project_id)
        if not project or project.org_id != settings.org_id:
            raise not_found("project", str(project_id))
        return project

    async def store_schema_tests(
        self, project_id: uuid.UUID, target_table: str, yaml_content: str,
        *, meta: dict | None = None,
    ) -> Project:
        """Record DQ-rendered schema.yml text for one target model.

        With `meta` (ApplyDqArtifact): idempotent on meta["idempotency_key"] —
        re-sending the same key for the same artifact is a no-op; reusing a key
        for a different artifact or model is a conflict (409)."""
        project = await self.get_internal(project_id)
        metas = dict(project.dq_schema_tests_meta or {})
        key = (meta or {}).get("idempotency_key")
        if key:
            for model, m in metas.items():
                if m.get("idempotency_key") != key:
                    continue
                if model == target_table and m.get("artifact_version_id") == meta.get(
                        "artifact_version_id") and m.get("content_hash") == meta.get("content_hash"):
                    return project      # exact replay — already applied
                raise HTTPException(status_code=409, detail={
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": "idempotency key already used for a different artifact/model"})
        # Reassign rather than mutate: SQLAlchemy will not detect an in-place
        # change to a JSONB dict and the update would silently never persist.
        merged = dict(project.dq_schema_tests or {})
        merged[target_table] = yaml_content
        project.dq_schema_tests = merged
        if meta is not None:
            metas[target_table] = meta
            project.dq_schema_tests_meta = metas
        await self._db.flush()
        return project

    async def delete(self, org_id: str, project_id: uuid.UUID) -> None:
        """Delete a project and (via ON DELETE CASCADE) its runs, run events,
        and lineage edges."""
        project = await self.get(org_id, project_id)
        await self._db.delete(project)
        await self._db.flush()

    async def list_for_org(self, org_id: str, workspace_id: uuid.UUID | None = None, kind: str | None = None) -> list[Project]:
        stmt = select(Project).where(Project.org_id == org_id)
        if workspace_id:
            stmt = stmt.where(Project.workspace_id == workspace_id)
        if kind:
            stmt = stmt.where(Project.kind == kind)
        result = await self._db.execute(stmt.order_by(Project.updated_at.desc()))
        return list(result.scalars())

    async def latest_runs(self, project_ids: list[uuid.UUID]) -> dict[uuid.UUID, Run]:
        """Most recent run per project, in one query.

        Keyed by project so the caller can attach each without a second lookup.
        Done as a single windowed query rather than a query per project: the
        list page renders every project at once, so per-row fetching turns one
        page load into N round trips that all wait on each other.

        Ordered by created_at, not started_at — a queued run has no start time
        yet, and it is still the latest thing that happened to the project.
        """
        if not project_ids:
            return {}
        ranked = (
            select(
                Run,
                func.row_number()
                .over(partition_by=Run.project_id, order_by=Run.created_at.desc())
                .label("rn"),
            )
            .where(Run.project_id.in_(project_ids))
            .subquery()
        )
        latest = aliased(Run, ranked)
        rows = await self._db.execute(select(latest).where(ranked.c.rn == 1))
        return {r.project_id: r for r in rows.scalars()}

    async def update(self, org_id: str, project_id: uuid.UUID, request: ProjectUpdate) -> Project:
        project = await self.get(org_id, project_id)
        if request.git_ref is not None:
            project.git_ref = request.git_ref
        if request.credential_ref is not None:
            ref = request.credential_ref or None                   # "" clears it
            _assert_credential_known(ref)
            project.credential_ref = ref
        if request.schedule_cron is not None:
            project.schedule_cron = request.schedule_cron
        if request.schedule_enabled is not None:
            project.schedule_enabled = request.schedule_enabled
        if request.git_trigger_enabled is not None:
            project.git_trigger_enabled = request.git_trigger_enabled
        if request.upstream_project_ids is not None and request.dependent_project_ids is not None:
            await dependents.set_links(self._db, project, request.upstream_project_ids, request.dependent_project_ids)
        elif request.upstream_project_ids is not None:
            await dependents.set_upstreams(self._db, project, request.upstream_project_ids)
        elif request.dependent_project_ids is not None:
            await dependents.set_dependents(self._db, project, request.dependent_project_ids)
        project.updated_at = datetime.now(timezone.utc)
        where = f"{project.git_url}@{request.git_ref or project.git_ref}" + (f" ({project.project_path})" if project.project_path else "")
        conflict_details = {"git_url": project.git_url, "git_ref": request.git_ref or project.git_ref, "project_path": project.project_path or ""}
        # (org, repo, ref, path) is unique: moving a project onto a ref another project already registered is a conflict the caller can act on,
        # not an internal error (W-022, X-008): the standard 409, with the failed flush rolled back.
        await flush_or_conflict(self._db, f"Project already registered for {where}", constraint="uq_project_repo_ref", details=conflict_details)
        return project

    async def list_all(self) -> list[Project]:
        """Every project of THIS data plane's org. The deployment model is one customer per data-plane database, but an internal
        credential must never be able to enumerate another org's rows if a data or migration mistake put them here (DBT-004), so
        the configured ORG_ID is a predicate of the query, like every user-facing read. Used by the Dagster code-location manager."""
        result = await self._db.execute(
            select(Project).where(Project.org_id == get_settings().org_id).order_by(Project.created_at, Project.name))
        return list(result.scalars())

    async def list_git_triggered(self) -> list[Project]:
        """Projects set to run on every new commit (DG-008), independent of any cron schedule. Used by Dagster's git sensor.
        Limited to this data plane's ORG_ID like every internal listing (DBT-004)."""
        result = await self._db.execute(
            select(Project).where(Project.org_id == get_settings().org_id, Project.git_trigger_enabled.is_(True))
            .order_by(Project.created_at, Project.name))
        return list(result.scalars())

    async def list_scheduled(self) -> list[Project]:
        """Used by the scheduler background task."""
        result = await self._db.execute(
            select(Project).where(
                Project.org_id == get_settings().org_id,        # DBT-004: only this data plane's org, even if the table holds others
                Project.schedule_enabled.is_(True),
                Project.schedule_cron.isnot(None),
            )
        )
        return list(result.scalars())
