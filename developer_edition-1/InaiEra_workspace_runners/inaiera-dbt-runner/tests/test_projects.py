"""Smoke tests for the projects/runs API surface.

Exercises auth + the register/list/get contract documented in Workspace
TDD 4.3.3. Run execution itself (git clone + dbt build) is integration-
level and not covered here — see executors/dbt_executor.py for the unit
of work that would need a real Git remote and engine to test end-to-end.

DB-touching tests run against the guarded, Alembic-migrated *_test
database (the `run_db` fixture) through the real app via an in-loop ASGI
client; they are skipped only when TEST_DATABASE_URL is unset.
"""
from __future__ import annotations

import httpx
import pytest

from app.main import app


@pytest.fixture
async def api(run_db, monkeypatch):
    # register/run must not start real dispatches in these API tests
    monkeypatch.setattr("app.services.run_service.dispatch_run", _noop_dispatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _noop_dispatch(run_id):
    return None


def test_register_requires_auth(client):
    resp = client.post("/api/v1/projects/register", json={
        "git_url": "https://github.com/acme/analytics.git",
        "git_ref": "main",
    })
    assert resp.status_code == 401


async def test_register_and_list_project(api, auth_headers):
    resp = await api.post(
        "/api/v1/projects/register",
        headers=auth_headers,
        json={"git_url": "https://github.com/acme/analytics.git", "git_ref": "main", "engine": "trino"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["git_url"] == "https://github.com/acme/analytics.git"
    assert body["engine"] == "trino"
    assert body["name"] == "analytics"

    list_resp = await api.get("/api/v1/projects", headers=auth_headers)
    assert list_resp.status_code == 200
    assert any(p["id"] == body["id"] for p in list_resp.json())


def _error_code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or body.get("detail") or {}).get("code")


async def test_register_duplicate_conflicts(api, auth_headers):
    payload = {"git_url": "https://github.com/acme/dup.git", "git_ref": "main"}
    first = await api.post("/api/v1/projects/register", headers=auth_headers, json=payload)
    assert first.status_code == 201
    second = await api.post("/api/v1/projects/register", headers=auth_headers, json=payload)
    assert second.status_code == 409
    assert _error_code(second) == "PROJECT_ALREADY_REGISTERED"


async def test_trigger_then_cancel_run_via_api(api, auth_headers):
    p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                        json={"git_url": "https://github.com/acme/c.git", "git_ref": "main"})).json()
    run = await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})
    assert run.status_code == 202 and run.json()["status"] == "queued"
    run_id = run.json()["id"]
    c1 = await api.post(f"/api/v1/projects/{p['id']}/runs/{run_id}/cancel", headers=auth_headers)
    assert c1.status_code == 200 and c1.json()["status"] == "cancelled"
    c2 = await api.post(f"/api/v1/projects/{p['id']}/runs/{run_id}/cancel", headers=auth_headers)
    assert c2.status_code == 409


def test_cancel_run_requires_auth(client):
    resp = client.post(
        "/api/v1/projects/00000000-0000-0000-0000-000000000099/runs/"
        "00000000-0000-0000-0000-000000000098/cancel",
    )
    assert resp.status_code == 401


def test_validate_requires_internal_token(client):
    resp = client.post("/api/v1/validate", json={
        "path": "models/staging/stg_orders.sql",
        "sql": "select 1 as id",
        "engine": "trino",
    })
    assert resp.status_code == 401


def test_internal_list_all_projects_requires_the_service_token(client):
    assert client.get("/api/v1/internal/projects").status_code == 401


async def test_cancel_of_a_running_k8s_run_reports_cancelling_not_cancelled(api, auth_headers, run_db, monkeypatch):
    """DBT-005 over the real route: the request is acknowledged as `cancelling` while the Job still exists."""
    from datetime import timedelta

    from sqlalchemy import update

    from app.core.config import get_settings
    from app.models import Run
    from tests.test_run_cancel import ScriptedIso
    from tests.test_run_reconciler import _make_run

    monkeypatch.setattr(get_settings(), "runner_mode", "k8s_job")
    iso = ScriptedIso("terminating", "gone")
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: iso)
    run_id, project_id = await _make_run(run_db, status="running", lease_owner="o", lease_delta=timedelta(minutes=5),
                                         k8s_job_name="j", released=True)
    from app.models import Project
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(k8s_job_name=f"dbtrun-{run_id}-a1"))
        await db.execute(update(Project).where(Project.id == project_id).values(org_id=get_settings().org_id))
        await db.commit()

    c1 = await api.post(f"/api/v1/projects/{project_id}/runs/{run_id}/cancel", headers=auth_headers)
    assert c1.status_code == 200 and c1.json()["status"] == "cancelling" and c1.json()["completed_at"] is None
    got = await api.get(f"/api/v1/projects/{project_id}/runs/{run_id}", headers=auth_headers)
    assert got.json()["status"] == "cancelling"
    listed = await api.get(f"/api/v1/projects/{project_id}/runs", headers=auth_headers)
    assert [r["status"] for r in listed.json()] == ["cancelling"]
    c2 = await api.post(f"/api/v1/projects/{project_id}/runs/{run_id}/cancel", headers=auth_headers)   # nudge: now gone
    assert c2.status_code == 200 and c2.json()["status"] == "cancelled"
    c3 = await api.post(f"/api/v1/projects/{project_id}/runs/{run_id}/cancel", headers=auth_headers)
    assert c3.status_code == 409


# W-022: an update that collides with another project is a 409 the caller can act on, never an internal error.
async def test_moving_a_project_onto_a_ref_another_project_has_is_a_409(api, auth_headers):
    url = "https://github.com/acme/refs.git"
    main = (await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": url, "git_ref": "main"})).json()
    other = (await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": url, "git_ref": "release"})).json()
    r = await api.patch(f"/api/v1/projects/{other['id']}", headers=auth_headers, json={"git_ref": "main"})
    assert r.status_code == 409
    assert _error_code(r) == "PROJECT_ALREADY_REGISTERED"        # DBT-013: a stable, machine-readable code
    # the project is unchanged, and a free ref still works
    assert (await api.get(f"/api/v1/projects/{other['id']}", headers=auth_headers)).json()["git_ref"] == "release"
    ok = await api.patch(f"/api/v1/projects/{other['id']}", headers=auth_headers, json={"git_ref": "v2"})
    assert ok.status_code == 200 and ok.json()["git_ref"] == "v2"
    assert main["id"] != other["id"]


# DBT-013: a duplicate update is a stable 409 with a machine-readable code and the conflicting identity, never a 500 —
# and the project (and the session) are still usable afterwards.
async def test_duplicate_update_returns_a_machine_readable_409(api, auth_headers):
    url = "https://github.com/acme/dupupdate.git"
    await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": url, "git_ref": "main"})
    other = (await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": url, "git_ref": "dev"})).json()
    r = await api.patch(f"/api/v1/projects/{other['id']}", headers=auth_headers, json={"git_ref": "main"})
    assert r.status_code == 409
    err = r.json().get("error") or r.json().get("detail")
    assert err["code"] == "PROJECT_ALREADY_REGISTERED"
    assert err["details"]["git_url"] == url and err["details"]["git_ref"] == "main"
    again = await api.patch(f"/api/v1/projects/{other['id']}", headers=auth_headers, json={"git_ref": "main"})
    assert again.status_code == 409                                       # repeatable, not a one-off 500
