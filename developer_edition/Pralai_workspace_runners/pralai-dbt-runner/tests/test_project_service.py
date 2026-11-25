"""Tests for RUN-DBT-005: ProjectService.get_internal() used to fetch any
project_id in the table with no authorization at all — "internal" meant
"no org check" rather than "checked a different way." Since the DQ
Service authenticates with a scoped service JWT (app.core.service_auth)
instead of a user JWT, there is no caller-supplied org_id to compare
against the way the ordinary get() does; the fix is to authorize against
this deployment's own org instead (every project in a data plane belongs
to that one org — TDD 1.3's single-tenant-per-data-plane model), so a
project_id that doesn't belong to this deployment is rejected exactly
like get() would reject one outside the caller's org.

Uses the real Postgres-backed `run_db` fixture (see conftest.py) rather
than mocks, since the point is verifying an actual authorization decision
against real rows — a project belonging to this deployment's org
(RUN-DBT-005 fixed) vs. one that doesn't (the exact scenario the finding
describes: "any project in the data plane").
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings
from app.core.errors import DbtRunnerError
from app.models import Project
from app.services.project_service import ProjectService


async def _make_project(run_db, *, org_id: str) -> uuid.UUID:
    async with run_db() as db:
        project = Project(
            org_id=org_id, name="analytics", git_url="https://github.com/acme/analytics.git",
            git_ref="main", engine="pg_lake",
        )
        db.add(project)
        await db.commit()
        return project.id


async def test_get_internal_returns_a_project_in_this_deployments_org(run_db, monkeypatch):
    monkeypatch.setattr("app.services.project_service.get_settings", lambda: Settings(org_id="org-this-deployment"))
    project_id = await _make_project(run_db, org_id="org-this-deployment")

    async with run_db() as db:
        project = await ProjectService(db).get_internal(project_id)
    assert project.id == project_id


async def test_get_internal_rejects_a_project_belonging_to_a_different_org(run_db, monkeypatch):
    """The exact vulnerability: a scoped-but-otherwise-unrestricted service
    token could previously reach any project_id in the table. A project
    that belongs to some other org must now be treated as not found, the
    same way the ordinary user-JWT get() already treats one outside the
    caller's org."""
    monkeypatch.setattr("app.services.project_service.get_settings", lambda: Settings(org_id="org-this-deployment"))
    foreign_project_id = await _make_project(run_db, org_id="org-some-other-tenant")

    async with run_db() as db:
        with pytest.raises(DbtRunnerError) as exc_info:
            await ProjectService(db).get_internal(foreign_project_id)
    assert exc_info.value.status_code == 404


async def test_get_internal_rejects_a_nonexistent_project(run_db, monkeypatch):
    monkeypatch.setattr("app.services.project_service.get_settings", lambda: Settings(org_id="org-this-deployment"))
    async with run_db() as db:
        with pytest.raises(DbtRunnerError) as exc_info:
            await ProjectService(db).get_internal(uuid.uuid4())
    assert exc_info.value.status_code == 404


async def test_store_schema_tests_persists_for_a_project_in_this_deployments_org(run_db, monkeypatch):
    monkeypatch.setattr("app.services.project_service.get_settings", lambda: Settings(org_id="org-this-deployment"))
    project_id = await _make_project(run_db, org_id="org-this-deployment")

    async with run_db() as db:
        await ProjectService(db).store_schema_tests(project_id, "stg_orders", "models: []\n")
        await db.commit()

    async with run_db() as db:
        project = await db.get(Project, project_id)
        assert project.dq_schema_tests == {"stg_orders": "models: []\n"}


async def test_store_schema_tests_rejects_a_project_belonging_to_a_different_org(run_db, monkeypatch):
    """Confirms the authorization fix actually blocks the mutation this
    finding was about — not just the read path."""
    monkeypatch.setattr("app.services.project_service.get_settings", lambda: Settings(org_id="org-this-deployment"))
    foreign_project_id = await _make_project(run_db, org_id="org-some-other-tenant")

    async with run_db() as db:
        with pytest.raises(DbtRunnerError) as exc_info:
            await ProjectService(db).store_schema_tests(foreign_project_id, "stg_orders", "models: []\n")
        await db.rollback()
    assert exc_info.value.status_code == 404

    async with run_db() as db:
        project = await db.get(Project, foreign_project_id)
        assert project.dq_schema_tests is None  # untouched
