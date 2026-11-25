"""Dependents (links between jobs): setting them, reading them back, refusing a loop. A job link is about jobs, not data: the projects
need not share a table. The rule the links drive (a job fails when a job above it failed or is out of date) is enforced in Dagster
(Pralai-dbt-runner-dagster: dependencies.py), which reads the links from the Runner; the Runner only stores them.

Real migrated Postgres.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.main import app
from app.models import Project, ProjectDependent, Run
from app.services import dependents, run_reconciler

FAIL = {"success": False, "errors": ["Compilation Error in model combined_check"], "nodes": []}


@pytest.fixture(autouse=True)
def _reset():
    run_reconciler._IN_FLIGHT.clear()
    yield
    run_reconciler._IN_FLIGHT.clear()


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── helpers ──────────────────────────────────────────────────────────────
async def _project(run_db, name: str, *, org_id: str | None = None) -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=org_id or get_settings().org_id, name=f"inaiera-dbt-test/{name}",
                    git_url=f"https://github.com/acme/{name}-{uuid.uuid4().hex[:6]}.git", git_ref="main", engine="pg_lake")
        db.add(p)
        await db.commit()
        return p.id


async def _link(run_db, upstream: uuid.UUID, *downstream: uuid.UUID) -> None:
    async with run_db() as db:
        await dependents.set_dependents(db, await db.get(Project, upstream), list(downstream))
        await db.commit()


async def _run(run_db, project_id, *, status="queued", trigger="scheduled", minutes_ago=0, job=None) -> uuid.UUID:
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    finished = status in ("succeeded", "failed", "cancelled")
    async with run_db() as db:
        r = Run(project_id=project_id, trigger=trigger, status=status, attempt=1 if status != "queued" else 0,
                started_at=at if status != "queued" else None, completed_at=at if finished else None,
                k8s_job_name=job, lease_owner="owner-1" if status == "running" else None,
                lease_expires_at=at + timedelta(minutes=10) if status == "running" else None)
        db.add(r)
        await db.commit()
        return r.id


async def _get(run_db, run_id) -> Run:
    async with run_db() as db:
        return await db.get(Run, run_id)


async def _fail_running_run(run_db, project_id) -> uuid.UUID:
    """A running build of `project_id` finishes with a failure, through the real finalize."""
    from app.services.run_service import _finalize_run
    run_id = await _run(run_db, project_id, status="running")
    assert await _finalize_run(run_id, project_id, None, {"success": False, "errors": ["Compilation Error"], "nodes": []})
    return run_id


# ── setting Dependents: API ──────────────────────────────────────────────────────────────────────────────────────────────
async def _register(api, headers, name, **extra):
    r = await api.post("/api/v1/projects/register", headers=headers,
                       json={"git_url": f"https://github.com/acme/{name}-{uuid.uuid4().hex[:6]}.git", "name": name, **extra})
    return r


async def test_dependents_are_set_at_registration_and_returned(api, auth_headers):
    customers = (await _register(api, auth_headers, "customers")).json()
    r = await _register(api, auth_headers, "combined", dependent_project_ids=[customers["id"]])
    assert r.status_code == 201, r.text
    assert r.json()["dependent_project_ids"] == [customers["id"]] and "blocked_by" not in r.json()
    got = (await api.get(f"/api/v1/projects/{r.json()['id']}", headers=auth_headers)).json()
    assert got["dependent_project_ids"] == [customers["id"]]


async def test_dependents_can_be_changed_cleared_or_left_alone(api, auth_headers):
    a, b, c = [(await _register(api, auth_headers, n)).json()["id"] for n in ("customers", "revenue", "scorecard")]
    url = f"/api/v1/projects/{a}"
    # returned by name: revenue, scorecard
    assert (await api.patch(url, headers=auth_headers, json={"dependent_project_ids": [c, b]})).json()["dependent_project_ids"] == [b, c]
    assert (await api.patch(url, headers=auth_headers, json={"schedule_cron": "0 * * * *"})).json()["dependent_project_ids"] == [b, c]
    assert (await api.patch(url, headers=auth_headers, json={"dependent_project_ids": [c]})).json()["dependent_project_ids"] == [c]
    assert (await api.patch(url, headers=auth_headers, json={"dependent_project_ids": []})).json()["dependent_project_ids"] == []


@pytest.mark.parametrize("bad", ["self", "unknown", "other_org"])
async def test_a_dependent_must_be_another_existing_project_of_this_org(api, auth_headers, run_db, bad):
    a = (await _register(api, auth_headers, "customers")).json()["id"]
    target = {"self": a, "unknown": str(uuid.uuid4()),
              "other_org": str(await _project(run_db, "theirs", org_id="another-org"))}[bad]
    r = await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"dependent_project_ids": [target]})
    assert r.status_code == 400 and r.json()["detail"]["details"]["field"] == "dependent_project_ids", r.text
    assert (await api.get(f"/api/v1/projects/{a}", headers=auth_headers)).json()["dependent_project_ids"] == []


async def test_links_that_would_make_a_loop_are_refused_with_the_loop_named(api, auth_headers):
    a, b, c = [(await _register(api, auth_headers, n)).json()["id"] for n in ("customers", "scorecard", "executive")]
    assert (await api.patch(f"/api/v1/projects/{a}", headers=auth_headers, json={"dependent_project_ids": [b]})).status_code == 200
    assert (await api.patch(f"/api/v1/projects/{b}", headers=auth_headers, json={"dependent_project_ids": [c]})).status_code == 200
    r = await api.patch(f"/api/v1/projects/{c}", headers=auth_headers, json={"dependent_project_ids": [a]})
    assert r.status_code == 400
    assert r.json()["detail"]["message"] == "These links would make a loop: executive → customers → scorecard → executive."
    direct = await api.patch(f"/api/v1/projects/{b}", headers=auth_headers, json={"dependent_project_ids": [a]})
    assert direct.status_code == 400


async def test_deleting_a_project_removes_its_links(api, auth_headers, run_db):
    customers = (await _register(api, auth_headers, "customers")).json()["id"]
    combined = (await _register(api, auth_headers, "combined", dependent_project_ids=[customers])).json()["id"]
    assert (await api.delete(f"/api/v1/projects/{customers}", headers=auth_headers)).status_code == 204
    assert (await api.get(f"/api/v1/projects/{combined}", headers=auth_headers)).json()["dependent_project_ids"] == []
    async with run_db() as db:
        assert (await db.execute(select(ProjectDependent))).all() == []
