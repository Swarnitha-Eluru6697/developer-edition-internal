"""DBT-005: cancel is a desired-state transition, not an acknowledgement.

running -> cancelling -> cancelled. `cancelling` records that the user asked; `cancelled` is written only once
the cluster confirms this attempt's Kubernetes Job is gone (or already finished), so a run never reads
`cancelled` while its Job is still executing and possibly mutating the warehouse. The reconciler retries the
deletion until it is confirmed and alerts on a cancellation that does not complete.

Runs against the real migrated Postgres test database and a fake Kubernetes client that models Foreground
deletion (the Job lingers, terminating, until its pods are gone) and API failures.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.core import metrics
from app.core.config import get_settings
from app.core.errors import DbtRunnerError
from app.executors import isolation
from app.models import Run
from app.services import run_cancel, run_lease, run_reconciler
from app.services.run_service import RunService, _finalize_run
from tests.test_isolation import FakeK8s, _k8s_settings, _use, no_real_k8s  # noqa: F401  (autouse fixture: no real kube config)
from tests.test_run_reconciler import _make_run

JOB = "dbtrun-{}-a1"


class ScriptedIso:
    """What the cluster says, call by call: a state string, or an Exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def terminate_job(self, *, run_id, attempt, job_name):
        self.calls.append({"run_id": run_id, "attempt": attempt, "job_name": job_name})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture(autouse=True)
def k8s_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_mode", "k8s_job")
    run_reconciler._IN_FLIGHT.clear()
    run_cancel._STUCK_LAST_LOGGED.clear()
    metrics.reset()


async def _running_with_job(run_db, *, released=True, attempt=1):
    run_id, project_id = await _make_run(run_db, status="running", lease_owner="owner-1", lease_delta=timedelta(minutes=5),
                                         attempt=attempt, k8s_job_name="placeholder", released=released)
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(k8s_job_name=JOB.format(run_id)))
        await db.commit()
    return run_id, project_id


async def _cancel(run_db, run_id, project_id, iso):
    from app.models import Project
    async with run_db() as db:
        project = await db.get(Project, project_id)
        return await RunService(db).cancel(project, run_id, isolation=iso)


async def _row(run_db, run_id) -> Run:
    async with run_db() as db:
        return await db.get(Run, run_id)


async def _tick(iso):
    await run_cancel.sweep(iso)


# ── the core guarantee ───────────────────────────────────────────────────
async def test_cancel_of_a_running_run_is_not_cancelled_while_its_job_is_still_terminating(run_db):
    run_id, project_id = await _running_with_job(run_db)
    iso = ScriptedIso("terminating")

    run = await _cancel(run_db, run_id, project_id, iso)

    assert run.status == "cancelling"                       # acknowledged as a REQUEST, not as done
    row = await _row(run_db, run_id)
    assert row.status == "cancelling" and row.completed_at is None and row.cancel_requested_at is not None
    assert row.lease_owner is None                          # nothing renews it: the owner's guarded steps stop
    assert iso.calls == [{"run_id": run_id, "attempt": 1, "job_name": JOB.format(run_id)}]


async def test_the_reconciler_completes_the_cancellation_only_once_the_job_is_gone(run_db):
    run_id, project_id = await _running_with_job(run_db)
    iso = ScriptedIso("terminating", "active", "terminating", "terminating", "gone")
    await _cancel(run_db, run_id, project_id, iso)
    seen = []
    for _ in range(4):
        await _tick(iso)
        seen.append((await _row(run_db, run_id)).status)
    assert seen == ["cancelling", "cancelling", "cancelling", "cancelled"]
    row = await _row(run_db, run_id)
    assert row.completed_at is not None and row.duration_ms is not None and row.duration_ms >= 0


@pytest.mark.parametrize("state", ["gone", "terminal", "foreign"])
async def test_confirmed_states_complete_the_cancellation(run_db, state):
    """gone: deleted. terminal: it had already finished. foreign: the name is not our Job, so ours does not exist."""
    run_id, project_id = await _running_with_job(run_db)
    run = await _cancel(run_db, run_id, project_id, ScriptedIso(state))
    assert run.status == "cancelled" and run.completed_at is not None


@pytest.mark.parametrize("state", ["terminating", "active"])
async def test_unconfirmed_states_leave_it_cancelling(run_db, state):
    run_id, project_id = await _running_with_job(run_db)
    assert (await _cancel(run_db, run_id, project_id, ScriptedIso(state))).status == "cancelling"


# ── deletion failures are retried, never swallowed into "cancelled" ──────
async def test_a_failing_deletion_leaves_the_run_cancelling_and_is_retried_until_it_works(run_db):
    run_id, project_id = await _running_with_job(run_db)
    iso = ScriptedIso(RuntimeError("k8s API unavailable"), RuntimeError("k8s API unavailable"), "gone")

    run = await _cancel(run_db, run_id, project_id, iso)
    assert run.status == "cancelling"                               # the old code said "cancelled" here and only logged
    await _tick(iso)
    assert (await _row(run_db, run_id)).status == "cancelling"
    await _tick(iso)
    assert (await _row(run_db, run_id)).status == "cancelled"
    assert len(iso.calls) == 3
    assert metrics.get("run_cancel_retry") == 2


async def test_a_deletion_that_raises_does_not_break_the_cancel_request_itself(run_db):
    run_id, project_id = await _running_with_job(run_db)
    run = await _cancel(run_db, run_id, project_id, ScriptedIso(Exception("boom")))
    assert run.status == "cancelling"


# ── immediate cases: nothing can be running ──────────────────────────────
async def test_a_queued_run_is_cancelled_at_once(run_db):
    run_id, project_id = await _make_run(run_db, status="queued")
    iso = ScriptedIso("active")
    run = await _cancel(run_db, run_id, project_id, iso)
    assert run.status == "cancelled" and iso.calls == []


async def test_a_running_run_with_no_job_recorded_is_cancelled_at_once(run_db):
    """The Job name is recorded BEFORE the Job is created, so no name means no Job can exist."""
    run_id, project_id = await _make_run(run_db, status="running", lease_owner="o", lease_delta=timedelta(minutes=1))
    iso = ScriptedIso("active")
    run = await _cancel(run_db, run_id, project_id, iso)
    assert run.status == "cancelled" and iso.calls == []


async def test_subprocess_mode_has_no_job_to_confirm_so_it_cancels_at_once(run_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_mode", "subprocess")
    run_id, project_id = await _running_with_job(run_db)
    iso = ScriptedIso("active")
    assert (await _cancel(run_db, run_id, project_id, iso)).status == "cancelled" and iso.calls == []


# ── idempotency, conflicts, concurrency ──────────────────────────────────
async def test_cancelling_again_while_cancelling_is_idempotent_and_nudges_the_deletion(run_db):
    run_id, project_id = await _running_with_job(run_db)
    iso = ScriptedIso("terminating", "terminating", "gone")
    await _cancel(run_db, run_id, project_id, iso)
    first = (await _row(run_db, run_id)).cancel_requested_at
    again = await _cancel(run_db, run_id, project_id, iso)
    assert again.status == "cancelling"
    assert (await _row(run_db, run_id)).cancel_requested_at == first         # the request time is not reset
    assert (await _cancel(run_db, run_id, project_id, iso)).status == "cancelled"


async def test_cancelling_a_finished_run_conflicts_and_an_unknown_run_404s(run_db):
    for status in ("succeeded", "failed", "cancelled"):
        run_id, project_id = await _make_run(run_db, status=status)
        with pytest.raises(DbtRunnerError) as e:
            await _cancel(run_db, run_id, project_id, ScriptedIso("gone"))
        assert e.value.status_code == 409
    with pytest.raises(DbtRunnerError) as e:
        await _cancel(run_db, uuid.uuid4(), project_id, ScriptedIso("gone"))
    assert e.value.status_code == 404


async def test_many_concurrent_cancels_make_one_transition_and_all_succeed(run_db):
    run_id, project_id = await _running_with_job(run_db)
    iso = ScriptedIso("terminating")
    results = await asyncio.gather(*[_cancel(run_db, run_id, project_id, iso) for _ in range(5)], return_exceptions=True)
    assert all(isinstance(r, Run) and r.status == "cancelling" for r in results), results
    assert (await _row(run_db, run_id)).status == "cancelling"


async def test_an_inline_attempt_racing_the_reconciler_completes_exactly_once(run_db):
    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    gone = ScriptedIso("gone")
    await asyncio.gather(*[run_cancel.advance(run_id, gone) for _ in range(6)])
    row = await _row(run_db, run_id)
    assert row.status == "cancelled"
    first_completed = row.completed_at
    await run_cancel.advance(run_id, gone)                           # a late tick is a no-op
    assert (await _row(run_db, run_id)).completed_at == first_completed


async def test_a_stale_completion_never_changes_a_run_that_is_not_cancelling(run_db):
    """The final step is a compare-and-set on status='cancelling': a late or duplicate completion is a no-op
    and can never flip a finished run (or move an already-cancelled run's timestamps)."""
    done, _ = await _make_run(run_db, status="succeeded")
    assert await run_cancel._mark_cancelled(done) == "cancelled"        # reports "done" but changes nothing
    assert (await _row(run_db, done)).status == "succeeded"

    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("gone"))
    before = await _row(run_db, run_id)
    assert before.status == "cancelled"
    await asyncio.sleep(0.05)
    await run_cancel._mark_cancelled(run_id)
    after = await _row(run_db, run_id)
    assert (after.completed_at, after.duration_ms) == (before.completed_at, before.duration_ms)


# ── the owner and the reconciler leave a cancelling run alone ────────────
async def test_the_owner_can_no_longer_renew_record_or_finalize_a_cancelling_run(run_db):
    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    assert await run_lease.renew(run_id, 1, "owner-1") is False
    assert await run_lease.record_job_name(run_id, 1, "x", "owner-1") is False
    assert await run_lease.still_owner(run_id, 1, "owner-1") is False
    done = await _finalize_run(run_id, project_id, None, {"success": True, "errors": [], "nodes": []}, attempt=1, owner="owner-1")
    assert done is False and (await _row(run_db, run_id)).status == "cancelling"


async def test_a_run_that_finishes_while_being_cancelled_is_still_cancelled(run_db):
    """The Job completed before the delete landed: its result is ignored, the user's request wins."""
    run_id, project_id = await _running_with_job(run_db)
    run = await _cancel(run_db, run_id, project_id, ScriptedIso("terminal"))
    assert run.status == "cancelled"


async def test_the_reconciler_never_adopts_requeues_or_takes_over_a_cancelling_run(run_db):
    run_id, project_id = await _running_with_job(run_db)
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await db.commit()
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    assert await run_lease.take_over_expired(run_id) is None
    assert await run_reconciler._reconcile_running() == []
    assert await run_reconciler._reconcile_queued() == []
    assert (await _row(run_db, run_id)).status == "cancelling"


async def test_reconcile_once_drives_cancellations_forward(run_db, monkeypatch):
    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    monkeypatch.setattr(run_reconciler, "get_runner_isolation", lambda: ScriptedIso("gone"))
    await run_reconciler.reconcile_once()
    assert (await _row(run_db, run_id)).status == "cancelled"


async def test_the_sweep_handles_every_cancelling_run_and_ignores_the_rest(run_db):
    a, pa = await _running_with_job(run_db)
    b, pb = await _running_with_job(run_db)
    other, _ = await _make_run(run_db, status="running", lease_owner="o", lease_delta=timedelta(minutes=5), k8s_job_name="j")
    for rid, pid in ((a, pa), (b, pb)):
        await _cancel(run_db, rid, pid, ScriptedIso("terminating"))
    await _tick(ScriptedIso("gone"))
    assert [(await _row(run_db, r)).status for r in (a, b, other)] == ["cancelled", "cancelled", "running"]


async def test_one_failing_run_does_not_stop_the_others_in_the_sweep(run_db):
    bad, pb = await _running_with_job(run_db)
    good, pg = await _running_with_job(run_db)
    for rid, pid in ((bad, pb), (good, pg)):
        await _cancel(run_db, rid, pid, ScriptedIso("terminating"))

    class Picky:
        async def terminate_job(self, *, run_id, attempt, job_name):
            if run_id == bad:
                raise RuntimeError("boom")
            return "gone"
    await _tick(Picky())
    assert (await _row(run_db, bad)).status == "cancelling" and (await _row(run_db, good)).status == "cancelled"


# ── alert on a cancellation that does not complete ───────────────────────
async def _age(run_db, run_id, seconds):
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(
            cancel_requested_at=datetime.now(UTC) - timedelta(seconds=seconds)))
        await db.commit()


async def test_a_stuck_cancellation_raises_an_alert(run_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "run_cancel_stuck_seconds", 300)
    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    iso = ScriptedIso("terminating")

    await _tick(iso)
    assert metrics.get("run_cancel_stuck") == 0                      # young: not an alert

    await _age(run_db, run_id, 3600)
    await _tick(iso)
    assert metrics.get("run_cancel_stuck") == 1
    await _tick(iso)
    assert metrics.get("run_cancel_stuck") == 1                      # rate-limited: not one alert per 15 s tick


async def test_the_alert_repeats_after_the_quiet_period_and_clears_when_it_completes(run_db, monkeypatch):
    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    await _age(run_db, run_id, 3600)
    await _tick(ScriptedIso("terminating"))
    run_cancel._STUCK_LAST_LOGGED[run_id] -= run_cancel.STUCK_REALERT_SECONDS + 1
    await _tick(ScriptedIso("terminating"))
    assert metrics.get("run_cancel_stuck") == 2
    await _tick(ScriptedIso("gone"))
    assert (await _row(run_db, run_id)).status == "cancelled" and run_id not in run_cancel._STUCK_LAST_LOGGED


async def test_a_stuck_cancellation_is_visible_in_readyz(run_db, monkeypatch):
    import httpx

    from app.main import app
    run_id, project_id = await _running_with_job(run_db)
    await _cancel(run_db, run_id, project_id, ScriptedIso("terminating"))
    monkeypatch.setattr("app.main._jwks_ready", lambda: _true())
    monkeypatch.setattr("app.main._k8s_ready", lambda: _true())

    async def readyz():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/readyz")
            assert r.status_code == 200, r.text            # degraded is still ready: this pod can serve
            return r.json()

    body = await readyz()
    assert body["checks"]["cancellation"] == {"cancelling": 1, "stuck": 0}
    assert "run_cancellation_stuck" not in body["degraded"]
    await _age(run_db, run_id, 3600)
    body = await readyz()
    assert body["checks"]["cancellation"] == {"cancelling": 1, "stuck": 1}
    assert "run_cancellation_stuck" in body["degraded"] and body["status"] == "degraded"


async def _true():
    return True


# ── the real service end to end over the real isolation code ─────────────
async def test_cancel_deletes_the_job_in_fake_k8s_and_only_then_reads_cancelled(run_db, monkeypatch):
    """Real RunnerIsolation + a fake cluster whose Foreground deletion lingers for two reads."""
    from tests.test_run_cancel_k8s import LingeringK8s
    settings = _k8s_settings()
    fake = LingeringK8s(linger_reads=2)
    _use(monkeypatch, fake, settings)
    run_id, project_id = await _make_run(run_db, status="running", lease_owner="o", lease_delta=timedelta(minutes=5), attempt=1)
    job = isolation._build_job(run_id=run_id, attempt=1, engine="pg_lake", select=None, settings=settings)
    fake.create_namespaced_job("inaiera", job)
    async with run_db() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(k8s_job_name=job.metadata.name))
        await db.commit()
    iso = isolation.RunnerIsolation()

    run = await _cancel(run_db, run_id, project_id, iso)
    assert run.status == "cancelling" and fake.deleted_jobs == [job.metadata.name]
    assert (await _row(run_db, run_id)).status == "cancelling"      # the Job still exists (terminating)
    await _tick(iso)
    assert (await _row(run_db, run_id)).status == "cancelling"
    await _tick(iso)
    assert (await _row(run_db, run_id)).status == "cancelled"        # only after the cluster says it is gone
    assert job.metadata.name not in fake.jobs
