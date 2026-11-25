"""DG-008: "run on new commit" is its own project setting, independent of the cron schedule.

Dagster's git sensor used to take its projects from the SCHEDULED listing: a project without a cron schedule could not be watched for
commits, and every scheduled project was watched whether or not anyone wanted that. Now `git_trigger_enabled` is a flag of its own, with
its own scoped internal listing.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest
from jose import jwt
from sqlalchemy import text

from app.core.config import get_settings
from app.main import app
from app.models import Project
from app.services.project_service import ProjectService

OTHER_ORG = "some-other-org"
SECRET = "dagster-scheduler-signing-secret"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])


@pytest.fixture
async def api(run_db, monkeypatch):
    async def _noop(run_id):
        return None
    monkeypatch.setattr("app.services.run_service.dispatch_run", _noop)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _scheduler_token(scope="dbt.projects.read") -> str:
    now = int(time.time())
    return jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": scope, "org_id": get_settings().org_id,
                       "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}, SECRET, algorithm="HS256")


async def _internal(api, path, scope="dbt.projects.read"):
    return await api.get(path, headers={"X-Internal-Service-Token": _scheduler_token(scope)})


def _project(org, *, cron=False, git=False, name=None) -> Project:
    return Project(org_id=org, name=name or f"p-{uuid.uuid4().hex[:6]}", git_url=f"https://github.com/acme/{uuid.uuid4().hex[:8]}.git",
                   git_ref="main", engine="pg_lake", schedule_cron="0 * * * *" if cron else None, schedule_enabled=cron,
                   git_trigger_enabled=git)


# ── the setting ──────────────────────────────────────────────────────────────
async def test_a_new_project_does_not_run_on_commit_by_default(api, auth_headers):
    r = await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": "https://github.com/acme/a.git", "git_ref": "main"})
    assert r.status_code == 201 and r.json()["git_trigger_enabled"] is False


async def test_a_project_can_be_registered_with_run_on_commit_and_no_schedule(api, auth_headers):
    r = await api.post("/api/v1/projects/register", headers=auth_headers,
                       json={"git_url": "https://github.com/acme/b.git", "git_ref": "main", "git_trigger_enabled": True})
    assert r.status_code == 201, r.text
    assert r.json()["git_trigger_enabled"] is True and r.json()["schedule_enabled"] is False and r.json()["schedule_cron"] is None


async def test_the_setting_is_changed_independently_of_the_schedule(api, auth_headers):
    p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                        json={"git_url": "https://github.com/acme/c.git", "git_ref": "main", "schedule_cron": "0 * * * *"})).json()
    assert p["schedule_enabled"] is True and p["git_trigger_enabled"] is False
    on = (await api.patch(f"/api/v1/projects/{p['id']}", headers=auth_headers, json={"git_trigger_enabled": True})).json()
    assert on["git_trigger_enabled"] is True and on["schedule_enabled"] is True and on["schedule_cron"] == "0 * * * *"
    off = (await api.patch(f"/api/v1/projects/{p['id']}", headers=auth_headers, json={"schedule_enabled": False})).json()
    assert off["schedule_enabled"] is False and off["git_trigger_enabled"] is True        # switching the cron off does not stop the commit trigger
    back = (await api.patch(f"/api/v1/projects/{p['id']}", headers=auth_headers, json={"git_trigger_enabled": False})).json()
    assert back["git_trigger_enabled"] is False


# ── the listing ──────────────────────────────────────────────────────────────
async def test_the_listing_is_by_the_commit_flag_not_the_schedule(run_db):
    org = get_settings().org_id
    async with run_db() as db:
        git_only, cron_only, both, neither = (_project(org, git=True), _project(org, cron=True), _project(org, cron=True, git=True), _project(org))
        db.add_all([git_only, cron_only, both, neither])
        await db.commit()
        triggered = {p.id for p in await ProjectService(db).list_git_triggered()}
        scheduled = {p.id for p in await ProjectService(db).list_scheduled()}
    assert triggered == {git_only.id, both.id}                   # a project with NO schedule is watched; a scheduled one is not, unless asked
    assert scheduled == {cron_only.id, both.id}                  # the cron listing is untouched


async def test_the_listing_is_limited_to_this_data_planes_org(run_db):
    async with run_db() as db:
        mine, theirs = _project(get_settings().org_id, git=True), _project(OTHER_ORG, git=True)
        db.add_all([mine, theirs])
        await db.commit()
        assert {p.id for p in await ProjectService(db).list_git_triggered()} == {mine.id}


async def test_the_internal_route_returns_the_triggered_projects_with_the_flag(api, run_db):
    async with run_db() as db:
        git_only, neither = _project(get_settings().org_id, git=True), _project(get_settings().org_id)
        db.add_all([git_only, neither])
        await db.commit()
    r = await _internal(api, "/api/v1/internal/projects/git-triggered")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [str(git_only.id)] and r.json()[0]["git_trigger_enabled"] is True


async def test_the_internal_route_needs_the_scheduler_identity(api):
    assert (await api.get("/api/v1/internal/projects/git-triggered")).status_code == 401
    assert (await _internal(api, "/api/v1/internal/projects/git-triggered", scope="dbt.runs.read")).status_code == 403


async def test_the_listing_path_is_not_mistaken_for_a_project_id(api):
    r = await _internal(api, "/api/v1/internal/projects/git-triggered")
    assert r.status_code == 200 and r.json() == []              # not a 422 for "git-triggered is not a UUID"


# ── the migration keeps what was watched ─────────────────────────────────────────
def test_migration_0012_keeps_watching_what_the_sensor_watched(migrated_test_database):
    import asyncio

    from alembic import command
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import _alembic_config

    url = migrated_test_database
    cfg = _alembic_config(url)

    def run(sql, params=None, fetch=False):
        async def _go():
            eng = create_async_engine(url)
            async with eng.begin() as conn:
                res = await conn.execute(text(sql), params or {})
                rows = res.fetchall() if fetch else None
            await eng.dispose()
            return rows
        return asyncio.run(_go())

    command.downgrade(cfg, "0011_project_credential_ref")
    try:
        run("DELETE FROM projects")
        ins = ("INSERT INTO projects (id, org_id, name, git_url, git_ref, project_path, engine, schedule_cron, schedule_enabled) "
               "VALUES (gen_random_uuid(), 'o', :n, :u, 'main', '', 'pg_lake', :c, :e)")
        for n, cron, enabled in [("sched", "0 * * * *", True), ("paused", "0 * * * *", False), ("manual", None, False)]:
            run(ins, {"n": n, "u": f"https://github.com/acme/{n}.git", "c": cron, "e": enabled})
        command.upgrade(cfg, "head")
        got = dict(run("SELECT name, git_trigger_enabled FROM projects", fetch=True))
        assert got == {"sched": True, "paused": False, "manual": False}       # exactly the projects the sensor used to watch
    finally:
        run("DELETE FROM projects")
