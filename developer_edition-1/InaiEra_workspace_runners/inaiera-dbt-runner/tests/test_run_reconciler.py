"""Tests for RUN-DBT-003's durable run controller (leases, takeover,
restart recovery, no duplicate execution).

Each test simulates a concrete failure mode against the real migrated test
database: a run left 'queued' with no task anywhere; a run whose owner is
alive (lease live) and must not be touched; a run whose owner died at each
stage (before a Job existed, with a suspended Job, with a running Job, after
the Job was TTL-GC'd); subprocess-mode orphans.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings, get_settings
from app.models import Project, Run
from app.services import run_lease, run_reconciler
from tests.conftest import FAKE_COMMIT_SHA


@pytest.fixture(autouse=True)
def _clear_in_flight():
    run_reconciler._IN_FLIGHT.clear()
    run_reconciler._dispatch_semaphore = None
    yield
    run_reconciler._IN_FLIGHT.clear()


async def _make_run(run_db, *, status: str, lease_owner=None, lease_delta=None, attempt=1,
                    k8s_job_name=None, released=False) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(timezone.utc)
    async with run_db() as db:
        project = Project(org_id=get_settings().org_id, name="analytics", git_url=f"https://github.com/acme/analytics-{uuid.uuid4().hex[:8]}.git",
                          git_ref="main", engine="pg_lake")
        db.add(project)
        await db.flush()
        run = Run(project_id=project.id, trigger="manual", status=status, attempt=attempt,
                  lease_owner=lease_owner,
                  lease_expires_at=(now + lease_delta) if lease_delta is not None else None,
                  k8s_job_name=k8s_job_name, job_released_at=now if released else None,
                  started_at=now if status == "running" else None)
        db.add(run)
        await db.commit()
        return run.id, project.id


async def _status(run_db, run_id) -> Run:
    async with run_db() as db:
        return await db.get(Run, run_id)


def _patch_dispatch(monkeypatch, tmp_path, *, result=None, gate: asyncio.Event | None = None):
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(exist_ok=True)
    (clone_dir / "dbt_project.yml").write_text("name: x\n")
    calls = []

    class FakeGit:
        def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
            return clone_dir

        def head_sha(self, path):
            return FAKE_COMMIT_SHA

        def tree_sha(self, clone_dir, project_dir):
            return "7" * 40

        def cleanup(self, run_id):
            pass

    class FakeIsolation:
        async def execute_run(self, **kwargs):
            calls.append(kwargs["run_id"])
            if gate is not None:
                await gate.wait()
            return dict(result or {"success": True, "errors": [], "nodes": []})

    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: FakeGit())
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: FakeIsolation())
    return calls


async def _drain():
    await asyncio.gather(*list(run_reconciler._IN_FLIGHT.values()), return_exceptions=True)


def _k8s_mode(monkeypatch):
    settings = Settings(runner_mode="k8s_job")
    monkeypatch.setattr(run_reconciler, "get_settings", lambda: settings)
    monkeypatch.setattr("app.services.run_service.get_settings", lambda: settings)
    return settings


class _ScriptedIsolation:
    """Reconciler-side isolation: adopt() answers are scripted per run."""

    def __init__(self, answers):
        self.answers = answers
        self.adopted = []
        self.cleaned = []

    async def adopt(self, *, lease, run_id, job_name, released):
        self.adopted.append((run_id, job_name, released, lease.attempt))
        answer = self.answers[run_id]
        return dict(answer) if isinstance(answer, dict) else await answer()

    async def cleanup_job(self, *, run_id, attempt, job_name):
        self.cleaned.append((run_id, attempt, job_name))
        return True


# ---------------------------------------------------------------------
# queued sweep
# ---------------------------------------------------------------------
async def test_queued_sweep_dispatches_orphaned_queued_run(run_db, monkeypatch, tmp_path):
    run_id, _ = await _make_run(run_db, status="queued", attempt=0)
    _patch_dispatch(monkeypatch, tmp_path)
    await run_reconciler._reconcile_queued()
    await _drain()
    run = await _status(run_db, run_id)
    assert run.status == "succeeded" and run.git_sha == FAKE_COMMIT_SHA


async def test_queued_sweep_is_non_blocking_and_bounded(run_db, monkeypatch, tmp_path):
    """The sweep returns immediately even though builds are still running,
    and never runs more than run_dispatch_concurrency at once."""
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "run_dispatch_concurrency", 2)
    run_ids = [(await _make_run(run_db, status="queued", attempt=0))[0] for _ in range(5)]
    gate = asyncio.Event()
    calls = _patch_dispatch(monkeypatch, tmp_path, gate=gate)

    await asyncio.wait_for(run_reconciler._reconcile_queued(), timeout=2)
    await asyncio.sleep(0.3)
    assert len(calls) == 2  # bounded
    # a second sweep while those are in flight does not double-dispatch
    await run_reconciler._reconcile_queued()
    await asyncio.sleep(0.1)
    assert len(calls) == 2

    gate.set()
    await _drain()
    statuses = [(await _status(run_db, rid)).status for rid in run_ids]
    assert statuses.count("succeeded") == 5
    assert sorted(set(calls)) == sorted(run_ids)  # each run executed exactly once
    assert len(calls) == 5


# ---------------------------------------------------------------------
# running sweep: live leases are never touched (clone-window race)
# ---------------------------------------------------------------------
async def test_live_lease_with_no_job_yet_is_left_alone(run_db, monkeypatch):
    """The race from the review: replica A is still cloning (no Job yet),
    replica B's reconciler must NOT see that as 'Job missing' and redispatch."""
    _k8s_mode(monkeypatch)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="replica-A",
                                lease_delta=timedelta(seconds=60))
    iso = _ScriptedIsolation({})
    monkeypatch.setattr(run_reconciler, "get_runner_isolation", lambda: iso)
    monkeypatch.setattr("app.services.run_service.get_git_service",
                        lambda: pytest.fail("must not redispatch a live-leased run"))

    await run_reconciler._reconcile_running()
    await _drain()

    run = await _status(run_db, run_id)
    assert run.status == "running" and run.lease_owner == "replica-A"
    assert iso.adopted == []


async def test_subprocess_mode_does_not_fail_runs_with_a_live_lease(run_db, monkeypatch):
    settings = Settings(runner_mode="subprocess")
    monkeypatch.setattr(run_reconciler, "get_settings", lambda: settings)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="other-worker",
                                lease_delta=timedelta(seconds=60))
    await run_reconciler._reconcile_running()
    assert (await _status(run_db, run_id)).status == "running"


async def test_subprocess_mode_fails_orphan_whose_lease_expired(run_db, monkeypatch):
    settings = Settings(runner_mode="subprocess")
    monkeypatch.setattr(run_reconciler, "get_settings", lambda: settings)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="dead-worker",
                                lease_delta=timedelta(seconds=-5))
    await run_reconciler._reconcile_running()
    run = await _status(run_db, run_id)
    assert run.status == "failed" and "orphaned" in run.error_message


# ---------------------------------------------------------------------
# running sweep: expired leases (owner died) in k8s_job mode
# ---------------------------------------------------------------------
async def test_expired_lease_without_job_is_requeued_and_redispatched(run_db, monkeypatch, tmp_path):
    _k8s_mode(monkeypatch)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="dead",
                                lease_delta=timedelta(seconds=-5), attempt=1)
    calls = _patch_dispatch(monkeypatch, tmp_path)
    await run_reconciler._reconcile_running()
    await _drain()
    run = await _status(run_db, run_id)
    assert run.status == "succeeded"
    assert run.attempt == 2  # a fresh attempt -> fresh Job name
    assert calls == [run_id]


async def test_expired_lease_with_running_job_is_adopted_and_finalized(run_db, monkeypatch):
    _k8s_mode(monkeypatch)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="dead",
                                lease_delta=timedelta(seconds=-5), attempt=3,
                                k8s_job_name="dbtrun-x-a3", released=True)
    result = {"success": True, "errors": [], "nodes": [{"node_id": "model.x.y", "status": "success"}],
              "git_sha": "fromjob", "_k8s_job": {"name": "dbtrun-x-a3", "attempt": 3}}
    iso = _ScriptedIsolation({run_id: result})
    monkeypatch.setattr(run_reconciler, "get_runner_isolation", lambda: iso)
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: iso)

    await run_reconciler._reconcile_running()
    await _drain()

    run = await _status(run_db, run_id)
    assert run.status == "succeeded" and run.git_sha == "fromjob"
    assert iso.adopted == [(run_id, "dbtrun-x-a3", True, 3)]
    assert iso.cleaned == [(run_id, 3, "dbtrun-x-a3")]  # deleted only after finalize


async def test_suspended_job_of_dead_owner_is_requeued_not_duplicated(run_db, monkeypatch, tmp_path):
    _k8s_mode(monkeypatch)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="dead",
                                lease_delta=timedelta(seconds=-5), attempt=1,
                                k8s_job_name="dbtrun-x-a1", released=False)
    iso = _ScriptedIsolation({run_id: {"_never_started": True, "success": False, "errors": [], "nodes": []}})
    monkeypatch.setattr(run_reconciler, "get_runner_isolation", lambda: iso)
    calls = _patch_dispatch(monkeypatch, tmp_path)

    await run_reconciler._reconcile_running()
    await _drain()

    assert iso.adopted[0][2] is False
    run = await _status(run_db, run_id)
    assert run.status == "succeeded" and run.attempt == 2
    assert calls == [run_id]


async def test_ttl_gced_job_is_never_rerun(run_db, monkeypatch):
    """After TTL GC the Job is gone; with no recorded artifact the run must
    fail with a clear reason — not execute the build a second time."""
    _k8s_mode(monkeypatch)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="dead",
                                lease_delta=timedelta(seconds=-5), attempt=1,
                                k8s_job_name="dbtrun-x-a1", released=True)
    from app.executors.isolation import RunnerIsolation

    real = RunnerIsolation()

    class Iso(_ScriptedIsolation):
        async def adopt(self, *, lease, run_id, job_name, released):
            self.adopted.append(run_id)
            return await real.result_for_missing_job(run_id=run_id, attempt=lease.attempt,
                                                     job_name=job_name, released=released)

    iso = Iso({})
    monkeypatch.setattr(run_reconciler, "get_runner_isolation", lambda: iso)
    monkeypatch.setattr("app.services.run_service.get_git_service",
                        lambda: pytest.fail("a GC'd Job must not trigger a re-run"))

    await run_reconciler._reconcile_running()
    await _drain()

    run = await _status(run_db, run_id)
    assert run.status == "failed"
    assert "Not re-running" in run.error_message
    assert run.attempt == 1


async def test_two_reconcilers_racing_take_over_only_once(run_db, monkeypatch):
    _k8s_mode(monkeypatch)
    run_id, _ = await _make_run(run_db, status="running", lease_owner="dead",
                                lease_delta=timedelta(seconds=-5), attempt=1,
                                k8s_job_name="dbtrun-x-a1", released=True)
    results = await asyncio.gather(run_lease.take_over_expired(run_id, owner="A"),
                                   run_lease.take_over_expired(run_id, owner="B"))
    assert sum(r is not None for r in results) == 1


async def test_stale_owner_cannot_finalize_after_takeover(run_db, monkeypatch):
    """Owner A stalls past its lease; B takes over. A's late finalize is
    rejected (owner mismatch), B's succeeds."""
    from app.services.run_service import _finalize_run

    run_id, project_id = await _make_run(run_db, status="queued", attempt=0)
    attempt = await run_lease.claim_queued(run_id, owner="A")
    async with run_db() as db:
        run = await db.get(Run, run_id)
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    assert await run_lease.take_over_expired(run_id, owner="B") is not None
    ok_a = await _finalize_run(run_id, project_id, None, {"success": True, "errors": [], "nodes": []},
                               attempt=attempt, owner="A")
    ok_b = await _finalize_run(run_id, project_id, None, {"success": False, "errors": ["x"], "nodes": []},
                               attempt=attempt, owner="B")
    assert (ok_a, ok_b) == (False, True)
    assert (await _status(run_db, run_id)).status == "failed"
