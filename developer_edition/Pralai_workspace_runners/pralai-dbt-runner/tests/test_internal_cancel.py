"""DG-017: the orchestrator can cancel the dbt run it started, with the DBT-005 semantics.

Before: Dagster could start and poll a run but had no way to stop it, so terminating the Dagster run left the dbt
Kubernetes Job running and modifying data. The cancel route existed only for users (`/projects/{id}/runs/{id}/cancel`,
a user JWT). Now Dagster has its own, separately scoped one:

  POST /api/v1/internal/runs/{run_id}/cancel                       scope dbt.runs.cancel
  POST /api/v1/internal/projects/{project_id}/runs/cancel          scope dbt.runs.cancel, body {"idempotency_key": ...}
                                                                   (for a submission whose response Dagster never saw)

Both end in RunService.cancel, i.e. exactly the user route's DBT-005 behaviour: queued -> cancelled, running with a Job ->
cancelling (the Job's deletion is requested) -> cancelled only once the cluster confirms. Real migrated Postgres.
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from jose import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.main import app
from app.models import Project, Run
from app.services import run_cancel, run_reconciler

SECRET = "dagster-scheduler-signing-secret"
STATIC = "legacy-static-internal-token"


@pytest.fixture(autouse=True)
def identities(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])
    monkeypatch.setattr(s, "internal_service_token", STATIC)
    run_reconciler._IN_FLIGHT.clear()
    run_cancel._STUCK_LAST_LOGGED.clear()


def token(scope="dbt.runs.cancel", **over) -> str:
    now = int(time.time())
    claims = {"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": scope,
              "org_id": get_settings().org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _post(api, path, body=None, tok=None):
    return await api.post(f"/api/v1/internal{path}", json=body or {},
                          headers={"X-Internal-Service-Token": tok or token()})


async def _make(run_db, *, status="queued", key=None, org=None, k8s_job=None, name="analytics"):
    async with run_db() as db:
        p = Project(org_id=org or get_settings().org_id, name=name, engine="pg_lake", git_ref="main",
                    git_url=f"https://github.com/acme/{uuid.uuid4().hex[:8]}.git")
        db.add(p)
        await db.flush()
        now = datetime.now(UTC)
        r = Run(project_id=p.id, trigger="scheduled", status=status, idempotency_key=key, k8s_job_name=k8s_job,
                started_at=now if status == "running" else None,
                lease_owner="o1" if status == "running" else None,
                lease_expires_at=now + timedelta(minutes=5) if status == "running" else None)
        db.add(r)
        await db.commit()
        return p.id, r.id


async def _row(run_db, run_id) -> Run:
    async with run_db() as db:
        return await db.get(Run, run_id)


# ── cancel by run id ─────────────────────────────────────────────────────
async def test_a_queued_run_is_cancelled_at_once(api, run_db):
    _, rid = await _make(run_db)
    r = await _post(api, f"/runs/{rid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["id"] == str(rid)
    row = await _row(run_db, rid)
    assert row.status == "cancelled" and row.completed_at is not None and row.cancel_requested_at is not None


async def test_a_running_run_with_a_job_is_cancelling_until_the_cluster_confirms(api, run_db, monkeypatch):
    """The DBT-005 guarantee, through the orchestrator's route: 'cancelled' only after the Job is gone."""
    monkeypatch.setattr(get_settings(), "runner_mode", "k8s_job")
    _, rid = await _make(run_db, status="running", k8s_job="dbtrun-x-a1")

    class Iso:
        def __init__(self, *answers):
            self.answers = list(answers)
            self.calls = 0

        async def terminate_job(self, *, run_id, attempt, job_name):
            self.calls += 1
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    iso = Iso("terminating", "gone")
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: iso)
    r = await _post(api, f"/runs/{rid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelling"
    assert (await _row(run_db, rid)).status == "cancelling"
    await run_cancel.sweep(iso)
    row = await _row(run_db, rid)
    assert row.status == "cancelled" and row.completed_at is not None and iso.calls == 2


async def test_cancelling_twice_is_idempotent(api, run_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_mode", "k8s_job")
    _, rid = await _make(run_db, status="running", k8s_job="dbtrun-x-a1")

    class Iso:
        async def terminate_job(self, **_kw):
            return "terminating"
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: Iso())
    first = await _post(api, f"/runs/{rid}/cancel")
    second = await _post(api, f"/runs/{rid}/cancel")
    assert first.status_code == second.status_code == 200 and second.json()["status"] == "cancelling"


@pytest.mark.parametrize("final", ["succeeded", "failed", "cancelled"])
async def test_a_finished_run_conflicts_and_is_left_alone(api, run_db, final):
    _, rid = await _make(run_db, status=final)
    r = await _post(api, f"/runs/{rid}/cancel")
    assert r.status_code == 409
    assert (await _row(run_db, rid)).status == final


async def test_an_unknown_run_is_a_404(api, run_db):
    assert (await _post(api, f"/runs/{uuid.uuid4()}/cancel")).status_code == 404


async def test_a_run_of_another_org_cannot_be_cancelled(api, run_db):
    _, rid = await _make(run_db, org="some-other-org")
    assert (await _post(api, f"/runs/{rid}/cancel")).status_code in (403, 404)
    assert (await _row(run_db, rid)).status == "queued"


# ── cancel by idempotency key (the submission whose answer Dagster never saw) ──
async def test_a_run_is_found_and_cancelled_by_its_idempotency_key(api, run_db):
    pid, rid = await _make(run_db, key="dagster:abc:submit_run_op")
    r = await _post(api, f"/projects/{pid}/runs/cancel", {"idempotency_key": "dagster:abc:submit_run_op"})
    assert r.status_code == 200 and r.json()["id"] == str(rid) and r.json()["status"] == "cancelled"


async def test_an_unknown_key_is_a_404_and_cancels_nothing(api, run_db):
    pid, rid = await _make(run_db, key="dagster:abc:submit_run_op")
    r = await _post(api, f"/projects/{pid}/runs/cancel", {"idempotency_key": "dagster:other:submit_run_op"})
    assert r.status_code == 404
    assert (await _row(run_db, rid)).status == "queued"


async def test_a_key_belongs_to_one_project(api, run_db):
    _, rid = await _make(run_db, key="dagster:abc:submit_run_op", name="a")
    other, _ = await _make(run_db, key="dagster:zzz:submit_run_op", name="b")
    r = await _post(api, f"/projects/{other}/runs/cancel", {"idempotency_key": "dagster:abc:submit_run_op"})
    assert r.status_code == 404 and (await _row(run_db, rid)).status == "queued"


async def test_a_body_without_a_key_is_refused(api, run_db):
    pid, _ = await _make(run_db)
    assert (await _post(api, f"/projects/{pid}/runs/cancel", {})).status_code == 422


# ── authentication: its own scope, single use ────────────────────────────
@pytest.mark.parametrize("scope", ["dbt.runs.trigger", "dbt.runs.read", "dbt.projects.read"])
async def test_no_other_scheduler_scope_can_cancel(api, run_db, scope):
    _, rid = await _make(run_db)
    r = await _post(api, f"/runs/{rid}/cancel", tok=token(scope))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "INSUFFICIENT_SCOPE"
    assert (await _row(run_db, rid)).status == "queued"


async def test_the_cancel_scope_cannot_start_a_run_or_read_projects(api, run_db):
    pid, _ = await _make(run_db)
    assert (await _post(api, f"/projects/{pid}/run", tok=token("dbt.runs.cancel"))).status_code == 403
    assert (await api.get("/api/v1/internal/projects", headers={"X-Internal-Service-Token": token("dbt.runs.cancel")})
            ).status_code == 403


async def test_the_static_token_and_no_token_are_refused(api, run_db):
    _, rid = await _make(run_db)
    assert (await _post(api, f"/runs/{rid}/cancel", tok=STATIC)).status_code == 401
    r = await api.post(f"/api/v1/internal/runs/{rid}/cancel", json={})
    assert r.status_code == 401
    assert (await _row(run_db, rid)).status == "queued"


async def test_a_cancel_token_cannot_be_replayed(api, run_db):
    _, rid = await _make(run_db)
    t = token()
    assert (await _post(api, f"/runs/{rid}/cancel", tok=t)).status_code == 200
    again = await _post(api, f"/runs/{rid}/cancel", tok=t)
    assert again.status_code == 401 and again.json()["detail"]["code"] == "TOKEN_REPLAYED"


# ── the user route is unchanged ──────────────────────────────────────────
async def test_the_user_cancel_route_still_works(api, run_db, auth_headers):
    p = (await api.post("/api/v1/projects/register", headers=auth_headers,
                        json={"git_url": "https://github.com/acme/u.git", "git_ref": "main"})).json()
    async with run_db() as db:
        r = Run(project_id=uuid.UUID(p["id"]), trigger="manual", status="queued")
        db.add(r)
        await db.commit()
        rid = r.id
    out = await api.post(f"/api/v1/projects/{p['id']}/runs/{rid}/cancel", headers=auth_headers)
    assert out.status_code == 200 and out.json()["status"] == "cancelled"
    async with run_db() as db:
        assert (await db.execute(select(Run.status).where(Run.id == rid))).scalar() == "cancelled"
