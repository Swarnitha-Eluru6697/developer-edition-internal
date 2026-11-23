"""DG-006: a Dagster-triggered occurrence is claimed in the Runner's ScheduledOccurrence table, atomically with the run.

Before: Dagster's sensor said the Runner's UNIQUE(project, scheduled_fire_time) was a second line of defence, but it only
put `scheduled_fire_time` in the Dagster run config: the trigger call never sent it, the Runner never recorded the
occurrence, and the ONLY dedupe was Dagster's own run_key. A replay that gets past it (run history pruned, a sensor
cursor reset, a re-created location, a second orchestrator) started the same transformation again.

Now the internal trigger accepts `scheduled_fire_time` (UTC instant, project + instant is the key). In the SAME
transaction it inserts the occurrence, freezes and creates the run and links them; a second request for the same
occurrence gets the run the first one created (no second run); a failed submission consumes nothing. Real migrated
Postgres; the Git host and the executor are faked.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import select, text

from app.core.config import get_settings
from app.main import app
from app.models import Project, Run, ScheduledOccurrence
from app.schemas.project import RunTriggerRequest
from app.services import run_manifest
from app.services.run_service import RunService
from app.services.scheduler import _maybe_fire

SHA = "1" * 40
URL = "https://github.com/acme/analytics.git"
T0 = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)


@pytest.fixture
def remote(monkeypatch):
    state = {"fail": None, "resolutions": 0}

    async def resolve(git_url, git_ref, token=None):
        state["resolutions"] += 1
        if state["fail"]:
            raise state["fail"]
        return SHA

    monkeypatch.setattr("app.services.run_manifest.resolve_commit", resolve)
    return state


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(run_id):
        return None
    monkeypatch.setattr("app.services.run_service.dispatch_run", _noop)


async def _project(run_db, **kw) -> uuid.UUID:
    async with run_db() as db:
        p = Project(org_id=get_settings().org_id, name=kw.pop("name", "analytics"), git_url=URL, git_ref=kw.pop("git_ref", "main"),
                    engine="pg_lake", **kw)
        db.add(p)
        await db.commit()
        return p.id


async def _fire(run_db, pid, when=T0, *, select_=None, trigger="scheduled", key=None) -> uuid.UUID:
    async with run_db() as db:
        project = await db.get(Project, pid)
        run = await RunService(db).submit(
            project=project, trigger=trigger, triggered_by_user_id=None, subject_id="dagster",
            request=RunTriggerRequest(select=select_, scheduled_fire_time=when), idempotency_key=key)
        await db.commit()
        return run.id


async def _all(run_db, model):
    async with run_db() as db:
        return (await db.execute(select(model))).scalars().all()


# ── the occurrence is claimed with the run ───────────────────────────────
async def test_the_occurrence_is_recorded_and_linked_to_its_run(run_db, remote):
    pid = await _project(run_db)
    run_id = await _fire(run_db, pid)
    (occ,) = await _all(run_db, ScheduledOccurrence)
    assert (occ.project_id, occ.scheduled_fire_time, occ.run_id) == (pid, T0, run_id)
    (run,) = await _all(run_db, Run)
    assert run.trigger == "scheduled" and run.id == run_id


async def test_a_replayed_occurrence_returns_the_first_run_and_starts_nothing(run_db, remote):
    """THE DG-006 reproduction: the same (project, instant) arriving again — e.g. from a second Dagster run."""
    pid = await _project(run_db)
    first = await _fire(run_db, pid)
    second = await _fire(run_db, pid)
    assert second == first
    assert len(await _all(run_db, Run)) == 1 and len(await _all(run_db, ScheduledOccurrence)) == 1


async def test_another_instant_or_another_project_is_a_separate_occurrence(run_db, remote):
    a, b = await _project(run_db, name="a"), await _project(run_db, name="b", git_ref="other")
    ids = {await _fire(run_db, a), await _fire(run_db, a, T0 + timedelta(minutes=2)), await _fire(run_db, b)}
    assert len(ids) == 3 and len(await _all(run_db, ScheduledOccurrence)) == 3


async def test_the_same_instant_in_another_utc_offset_is_the_same_occurrence(run_db, remote):
    pid = await _project(run_db)
    first = await _fire(run_db, pid, T0)
    again = await _fire(run_db, pid, T0.astimezone(timezone(timedelta(hours=5, minutes=30))))
    assert again == first and len(await _all(run_db, ScheduledOccurrence)) == 1


async def test_concurrent_requests_for_one_occurrence_create_one_run(run_db, remote):
    pid = await _project(run_db)
    ids = await asyncio.gather(*[_fire(run_db, pid) for _ in range(6)])
    assert len(set(ids)) == 1
    assert len(await _all(run_db, Run)) == 1 and len(await _all(run_db, ScheduledOccurrence)) == 1


async def test_a_failed_submission_does_not_consume_the_occurrence(run_db, remote):
    pid = await _project(run_db)
    remote["fail"] = run_manifest.GitRefError("GIT_UNREACHABLE", "git host down")
    with pytest.raises(HTTPException):
        await _fire(run_db, pid)
    assert await _all(run_db, ScheduledOccurrence) == [] and await _all(run_db, Run) == []
    remote["fail"] = None
    run_id = await _fire(run_db, pid)                                   # the retry of the same occurrence works
    (occ,) = await _all(run_db, ScheduledOccurrence)
    assert occ.run_id == run_id


async def test_replaying_an_occurrence_with_a_different_request_is_refused(run_db, remote):
    pid = await _project(run_db)
    await _fire(run_db, pid, select_="tag:a")
    with pytest.raises(HTTPException) as e:
        await _fire(run_db, pid, select_="tag:b")
    assert e.value.status_code == 422 and e.value.detail["code"] == "OCCURRENCE_REUSED"
    assert len(await _all(run_db, Run)) == 1


async def test_an_occurrence_whose_run_was_purged_is_not_fired_again(run_db, remote):
    pid = await _project(run_db)
    await _fire(run_db, pid)
    async with run_db() as db:                                          # retention removed the run: run_id -> NULL
        await db.execute(text("DELETE FROM runs"))
        await db.commit()
    with pytest.raises(HTTPException) as e:
        await _fire(run_db, pid)
    assert e.value.status_code == 409 and e.value.detail["code"] == "OCCURRENCE_ALREADY_FIRED"
    assert await _all(run_db, Run) == []


async def test_a_manual_or_api_trigger_cannot_claim_an_occurrence(run_db, remote):
    pid = await _project(run_db)
    with pytest.raises(HTTPException) as e:
        await _fire(run_db, pid, trigger="api")
    assert e.value.status_code == 422 and e.value.detail["code"] == "OCCURRENCE_NOT_SCHEDULED"
    assert await _all(run_db, ScheduledOccurrence) == []


async def test_an_occurrence_in_the_future_is_refused(run_db, remote):
    pid = await _project(run_db)
    with pytest.raises(HTTPException) as e:
        await _fire(run_db, pid, datetime.now(UTC) + timedelta(hours=1))
    assert e.value.status_code == 422 and e.value.detail["code"] == "OCCURRENCE_IN_THE_FUTURE"
    assert await _all(run_db, ScheduledOccurrence) == []


async def test_the_idempotency_key_of_a_retried_step_still_returns_its_run_first(run_db, remote):
    pid = await _project(run_db)
    first = await _fire(run_db, pid, key="dagster:run-1:submit_run_op")
    assert await _fire(run_db, pid, key="dagster:run-1:submit_run_op") == first
    assert len(await _all(run_db, ScheduledOccurrence)) == 1


async def test_a_run_without_an_occurrence_is_unchanged(run_db, remote):
    pid = await _project(run_db)
    async with run_db() as db:
        project = await db.get(Project, pid)
        for _ in range(2):
            await RunService(db).submit(project=project, trigger="manual", triggered_by_user_id="u1",
                                        request=RunTriggerRequest())
        await db.commit()
    assert len(await _all(run_db, Run)) == 2 and await _all(run_db, ScheduledOccurrence) == []


async def test_the_runners_own_scheduler_does_not_fire_an_occurrence_dagster_already_claimed(run_db, remote):
    """Both orchestrators share the watermark (the latest occurrence): running both cannot double-fire an instant."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    pid = await _project(run_db, schedule_cron="* * * * *", schedule_enabled=True, updated_at=now - timedelta(minutes=5))
    await _fire(run_db, pid, now)
    async with run_db() as db:
        await _maybe_fire(await db.get(Project, pid), now + timedelta(seconds=20))
    assert len(await _all(run_db, Run)) == 1


# ── the HTTP contract ────────────────────────────────────────────────────
SECRET = "dagster-scheduler-signing-secret"


def _token(scope="dbt.runs.trigger") -> str:
    now = int(time.time())
    return jwt.encode({"iss": "dagster", "sub": "dagster", "aud": "dbtrunner", "scope": scope,
                       "org_id": get_settings().org_id, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60},
                      SECRET, algorithm="HS256")


@pytest.fixture
async def api(run_db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "scheduler_jwt_secret", SECRET)
    monkeypatch.setattr(s, "scheduler_jwt_allowed_issuers", ["dagster"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _post(api, pid, body, **params):
    return await api.post(f"/api/v1/internal/projects/{pid}/run", params=params, json=body,
                          headers={"X-Internal-Service-Token": _token()})   # single use: a fresh token per call


async def test_the_internal_route_deduplicates_an_occurrence(api, run_db, remote):
    pid = await _project(run_db)
    body = {"scheduled_fire_time": "2026-10-14T06:00:00+00:00"}
    a, b = await _post(api, pid, body), await _post(api, pid, body)
    assert a.status_code == b.status_code == 202 and a.json()["id"] == b.json()["id"]
    assert len(await _all(run_db, Run)) == 1


async def test_the_internal_route_refuses_a_naive_time_and_a_non_scheduled_trigger(api, run_db, remote):
    pid = await _project(run_db)
    assert (await _post(api, pid, {"scheduled_fire_time": "2026-10-14T06:00:00"})).status_code == 422      # no zone
    r = await _post(api, pid, {"scheduled_fire_time": "2026-10-14T06:00:00Z"}, trigger="api")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "OCCURRENCE_NOT_SCHEDULED"
    assert await _all(run_db, Run) == []


async def test_the_user_route_does_not_accept_an_occurrence(api, run_db, remote, auth_headers):
    p = (await api.post("/api/v1/projects/register", headers=auth_headers, json={"git_url": URL, "git_ref": "main"})).json()
    r = await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers,
                       json={"scheduled_fire_time": "2026-10-14T06:00:00Z"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SCHEDULED_FIRE_TIME_NOT_ACCEPTED"
    assert await _all(run_db, Run) == []


def test_the_dagster_client_sends_the_field_the_runner_reads():
    """Cross-repo: the key the Dagster client puts in the trigger body is this request model's field."""
    import pathlib
    client = (pathlib.Path(__file__).resolve().parents[3] / "InaiEra-dbt-runner-dagster" / "dagster"
              / "inaiera_dbt_runner_dags" / "resources" / "dbt_runner_client.py")
    if not client.exists():
        pytest.skip("the Dagster repo is not checked out next to this one")
    assert "scheduled_fire_time" in RunTriggerRequest.model_fields
    assert 'body["scheduled_fire_time"]' in client.read_text()


# ── a run started by hand through Dagster is a MANUAL run, with who started it ────────────────────────────────────────────
async def _one(run_db) -> Run:
    (run,) = await _all(run_db, Run)
    return run


async def test_a_manual_run_through_the_internal_route_records_who_started_it(api, run_db, remote):
    pid = await _project(run_db)
    r = await _post(api, pid, {}, trigger="manual", triggered_by="4a5ef138-user")
    assert r.status_code == 202
    run = await _one(run_db)
    assert (run.trigger, run.triggered_by_user_id) == ("manual", "4a5ef138-user")


async def test_a_manual_run_without_a_name_is_still_manual(api, run_db, remote):
    pid = await _project(run_db)
    assert (await _post(api, pid, {}, trigger="manual")).status_code == 202
    run = await _one(run_db)
    assert (run.trigger, run.triggered_by_user_id) == ("manual", None)


async def test_only_a_manual_run_keeps_the_name_scheduled_and_api_runs_have_none(api, run_db, remote):
    pid = await _project(run_db)
    for trigger in ("scheduled", "api"):
        assert (await _post(api, pid, {}, trigger=trigger, triggered_by="someone")).status_code == 202
    assert sorted((r.trigger, r.triggered_by_user_id) for r in await _all(run_db, Run)) == [("api", None), ("scheduled", None)]


async def test_the_default_is_still_a_scheduled_run(api, run_db, remote):
    pid = await _project(run_db)
    assert (await _post(api, pid, {})).status_code == 202
    assert (await _one(run_db)).trigger == "scheduled"


async def test_a_manual_run_cannot_claim_a_cron_occurrence_and_the_name_has_a_limit(api, run_db, remote):
    pid = await _project(run_db)
    r = await _post(api, pid, {"scheduled_fire_time": "2026-10-14T06:00:00Z"}, trigger="manual")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "OCCURRENCE_NOT_SCHEDULED"
    assert (await _post(api, pid, {}, trigger="manual", triggered_by="x" * 256)).status_code == 422
    assert await _all(run_db, Run) == []


async def test_an_unknown_trigger_is_still_refused(api, run_db, remote):
    pid = await _project(run_db)
    assert (await _post(api, pid, {}, trigger="cron")).status_code == 422
