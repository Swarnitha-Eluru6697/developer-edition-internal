"""Tests for RUN-DBT-004: the scheduler could fire the same cron
occurrence twice because "is this due" and "have I already fired it"
both lived in a process-local dict, so two replicas each thought they
were the only one deciding.

The pure cron-math is tested without a DB; everything else uses a real
Postgres database (the `run_db` fixture in conftest.py) because the
actual fix — a Postgres advisory lock plus a UNIQUE(project_id,
scheduled_fire_time) constraint — cannot be verified meaningfully any
other way. The concurrency test below is the one that actually proves
two replicas can no longer double-fire: it runs two real, independent
_maybe_fire() calls for the same project at the same instant and checks
that only one Run comes out the other end.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.models import Project, Run, ScheduledOccurrence
from tests.conftest import FAKE_COMMIT_SHA
from app.services.scheduler import _compute_next_fire, _last_fire_time, _latest_due_fire, _maybe_fire


def test_compute_next_fire_returns_none_for_invalid_cron():
    assert _compute_next_fire("not a cron expression", datetime.now(timezone.utc)) is None


def test_compute_next_fire_advances_from_last():
    last = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    next_fire = _compute_next_fire("0 * * * *", last)  # top of every hour
    assert next_fire == datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_latest_due_fire_coalesces_missed_occurrences():
    first = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 0, 5, 30, tzinfo=timezone.utc)
    assert _latest_due_fire("* * * * *", first, now) == datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert _latest_due_fire("0 * * * *", first, now) == first


class _FakeGitService:
    def __init__(self, clone_dir):
        self._clone_dir = clone_dir

    def clone_for_run(self, *, git_url, git_ref, run_id, sha=None, token=None):
        return self._clone_dir

    def head_sha(self, path):
        return FAKE_COMMIT_SHA

    def tree_sha(self, clone_dir, project_dir):
        return "7" * 40

    def cleanup(self, run_id):
        pass


class _FakeIsolation:
    async def execute_run(self, **kwargs):
        return {"success": True, "errors": [], "nodes": []}


def _patch_dispatch(monkeypatch, tmp_path):
    """Fakes out the actual dbt execution the background dispatch task
    (fired by RunService.submit(), see RUN-DBT-003) would otherwise try
    to do — irrelevant to this finding, which is only about whether a
    Run gets created once or twice."""
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir(exist_ok=True)
    (clone_dir / "dbt_project.yml").write_text("name: x\n")
    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: _FakeGitService(clone_dir))
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: _FakeIsolation())


async def _make_scheduled_project(run_db, *, cron: str, updated_at: datetime) -> uuid.UUID:
    async with run_db() as db:
        project = Project(
            org_id=get_settings().org_id, name="analytics", git_url="https://github.com/acme/analytics.git",
            git_ref="main", engine="pg_lake", schedule_cron=cron, schedule_enabled=True,
            updated_at=updated_at,
        )
        db.add(project)
        await db.commit()
        return project.id


async def test_maybe_fire_creates_occurrence_and_run_when_due(run_db, monkeypatch, tmp_path):
    _patch_dispatch(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    project_id = await _make_scheduled_project(run_db, cron="* * * * *", updated_at=now - timedelta(minutes=5))

    async with run_db() as db:
        project = await db.get(Project, project_id)
        await _maybe_fire(project, now)
    await asyncio.sleep(0.1)  # let the background dispatch task settle

    async with run_db() as db:
        occurrences = (await db.execute(
            select(ScheduledOccurrence).where(ScheduledOccurrence.project_id == project_id)
        )).scalars().all()
        runs = (await db.execute(select(Run).where(Run.project_id == project_id))).scalars().all()

    assert len(occurrences) == 1
    assert len(runs) == 1
    assert runs[0].trigger == "scheduled"
    assert occurrences[0].run_id == runs[0].id


async def test_maybe_fire_is_a_noop_when_not_yet_due(run_db, monkeypatch):
    def _must_not_run():
        raise AssertionError("a not-yet-due project must never call get_git_service")
    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: _must_not_run())

    now = datetime.now(timezone.utc)
    # updated_at == now, cron fires once an hour -> next fire is up to an
    # hour away, never <= now.
    project_id = await _make_scheduled_project(run_db, cron="0 * * * *", updated_at=now)

    async with run_db() as db:
        project = await db.get(Project, project_id)
        await _maybe_fire(project, now)

    async with run_db() as db:
        runs = (await db.execute(select(Run).where(Run.project_id == project_id))).scalars().all()
    assert runs == []


async def test_maybe_fire_handles_invalid_cron_without_raising(run_db):
    now = datetime.now(timezone.utc)
    project_id = await _make_scheduled_project(run_db, cron="garbage cron", updated_at=now - timedelta(days=1))

    async with run_db() as db:
        project = await db.get(Project, project_id)
        await _maybe_fire(project, now)  # must not raise

    async with run_db() as db:
        runs = (await db.execute(select(Run).where(Run.project_id == project_id))).scalars().all()
    assert runs == []


async def test_last_fire_time_uses_latest_occurrence_not_project_updated_at(run_db):
    project_id = await _make_scheduled_project(
        run_db, cron="* * * * *", updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    later = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with run_db() as db:
        db.add(ScheduledOccurrence(project_id=project_id, scheduled_fire_time=later))
        await db.commit()

        project = await db.get(Project, project_id)
        watermark = await _last_fire_time(db, project)
    assert watermark == later


async def test_concurrent_replicas_fire_the_same_occurrence_only_once(run_db, monkeypatch, tmp_path):
    """The actual RUN-DBT-004 regression test: two scheduler replicas
    (simulated here as two independent _maybe_fire() calls running at the
    same time, each with its own DB session/connection — exactly how two
    pods would behave) both see the same due occurrence. Only one may
    turn it into a Run."""
    _patch_dispatch(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    project_id = await _make_scheduled_project(run_db, cron="* * * * *", updated_at=now - timedelta(minutes=5))

    async def _one_replica_tick():
        async with run_db() as db:
            project = await db.get(Project, project_id)
        await _maybe_fire(project, now)

    await asyncio.gather(_one_replica_tick(), _one_replica_tick())
    await asyncio.sleep(0.1)

    async with run_db() as db:
        occurrences = (await db.execute(
            select(ScheduledOccurrence).where(ScheduledOccurrence.project_id == project_id)
        )).scalars().all()
        runs = (await db.execute(select(Run).where(Run.project_id == project_id))).scalars().all()

    assert len(occurrences) == 1, "two replicas recorded the same occurrence twice"
    assert len(runs) == 1, "two replicas fired the same scheduled occurrence twice"
