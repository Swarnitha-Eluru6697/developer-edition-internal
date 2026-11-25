"""DBT-004: every internal listing / query boundary applies THIS data plane's ORG_ID.

The user-facing routes filter by org, but internal paths (Dagster's project listing, the scheduler, the reconciler's sweeps, the cancel
sweep, dispatch) selected rows with no org predicate, so a row of another org that a data or migration mistake put in this database
would have been listed, scheduled, dispatched and executed. Each test seeds a foreign-org row beside an own-org one.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.models import Project, Run
from app.services import run_cancel, run_reconciler
from app.services.project_service import ProjectService
from app.services.run_service import dispatch_run

OTHER = "some-other-org"


@pytest.fixture(autouse=True)
def _reset():
    run_reconciler._IN_FLIGHT.clear()
    run_reconciler._dispatch_semaphore = None
    yield
    run_reconciler._IN_FLIGHT.clear()


async def _project(db, org, *, scheduled=False, name=None):
    p = Project(org_id=org, name=name or f"p-{uuid.uuid4().hex[:6]}", git_url=f"https://github.com/acme/{uuid.uuid4().hex[:8]}.git",
                git_ref="main", engine="pg_lake", schedule_enabled=scheduled, schedule_cron="0 * * * *" if scheduled else None)
    db.add(p)
    await db.flush()
    return p


async def test_list_all_returns_only_this_orgs_projects(run_db):
    async with run_db() as db:
        mine, theirs = await _project(db, get_settings().org_id), await _project(db, OTHER)
        await db.commit()
        ids = {p.id for p in await ProjectService(db).list_all()}
    assert mine.id in ids and theirs.id not in ids


async def test_list_scheduled_returns_only_this_orgs_projects(run_db):
    async with run_db() as db:
        mine, theirs = await _project(db, get_settings().org_id, scheduled=True), await _project(db, OTHER, scheduled=True)
        await db.commit()
        ids = {p.id for p in await ProjectService(db).list_scheduled()}
    assert mine.id in ids and theirs.id not in ids


async def test_the_queued_sweep_never_dispatches_another_orgs_run(run_db, monkeypatch):
    dispatched = []

    async def spy(run_id):
        dispatched.append(run_id)

    monkeypatch.setattr(run_reconciler, "dispatch_run", spy)
    async with run_db() as db:
        mine, theirs = await _project(db, get_settings().org_id), await _project(db, OTHER)
        r1 = Run(project_id=mine.id, trigger="manual", status="queued", attempt=0)
        r2 = Run(project_id=theirs.id, trigger="manual", status="queued", attempt=0)
        db.add_all([r1, r2])
        await db.commit()
    await run_reconciler._reconcile_queued()
    await asyncio.gather(*list(run_reconciler._IN_FLIGHT.values()), return_exceptions=True)
    assert dispatched == [r1.id]


async def test_dispatch_refuses_a_run_of_another_org(run_db, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("another org's project must never be executed")

    monkeypatch.setattr("app.services.run_service.get_git_service", boom)
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", boom)
    async with run_db() as db:
        theirs = await _project(db, OTHER)
        run = Run(project_id=theirs.id, trigger="manual", status="queued", attempt=0)
        db.add(run)
        await db.commit()
    await dispatch_run(run.id)
    async with run_db() as db:
        assert (await db.get(Run, run.id)).status == "queued"          # untouched: not claimed, not failed


async def test_the_cancel_sweep_ignores_another_orgs_runs(run_db):
    class Isolation:
        def __init__(self):
            self.cancelled = []

        async def cancel_job(self, **kw):
            self.cancelled.append(kw)
            return True

        async def cleanup_job(self, **kw):
            self.cancelled.append(kw)
            return True

    async with run_db() as db:
        theirs = await _project(db, OTHER)
        run = Run(project_id=theirs.id, trigger="manual", status="cancelling", attempt=1, cancel_requested_at=datetime.now(UTC))
        db.add(run)
        await db.commit()
    iso = Isolation()
    await run_cancel.sweep(iso)
    assert iso.cancelled == []
    async with run_db() as db:
        assert (await db.get(Run, run.id)).status == "cancelling"
