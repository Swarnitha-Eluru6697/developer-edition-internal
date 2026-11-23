"""DBT-011: a run has ONE owner of dispatch — the leader's durable reconciler loop.

`RunService.submit` used to start execution with an in-process `asyncio.create_task(dispatch_run(...))` AND the reconciler also
claimed queued runs, so two paths raced for the same run and an API process owned work it could lose on exit. Now submit only
persists the queued row and announces it (NOTIFY, delivered at commit); the reconciler claims it after the commit.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.main import app
from app.models import Project, Run
from app.services import run_reconciler
from app.services.run_service import RUN_QUEUED_CHANNEL
from tests.conftest import FAKE_COMMIT_SHA


@pytest.fixture
async def api(run_db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset():
    run_reconciler._IN_FLIGHT.clear()
    run_reconciler._dispatch_semaphore = None
    run_reconciler._wake = None
    yield
    run_reconciler._IN_FLIGHT.clear()
    run_reconciler._wake = None


async def _register(api, headers) -> dict:
    r = await api.post("/api/v1/projects/register", headers=headers,
                       json={"git_url": f"https://github.com/acme/{uuid.uuid4().hex[:8]}.git", "git_ref": "main"})
    assert r.status_code == 201, r.text
    return r.json()


async def test_submit_persists_a_queued_run_and_starts_no_execution(api, auth_headers, monkeypatch):
    started = []

    async def spy(run_id):
        started.append(run_id)

    monkeypatch.setattr("app.services.run_service.dispatch_run", spy)
    p = await _register(api, auth_headers)
    r = await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    await asyncio.sleep(0.3)                       # a stray create_task would have run by now
    assert started == []
    assert not any("dispatch_run" in repr(t.get_coro()) for t in asyncio.all_tasks())


async def test_submit_announces_the_run_at_commit(api, auth_headers, run_db):
    """The NOTIFY is part of submit's transaction: a listener hears it, with the run id, only once the row is committed."""
    import asyncpg

    from app.db import session as session_mod

    dsn = session_mod.engine.url.render_as_string(hide_password=False).replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    heard: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(RUN_QUEUED_CHANNEL, lambda _c, _pid, _ch, payload: heard.put_nowait(payload))
    try:
        p = await _register(api, auth_headers)
        run = (await api.post(f"/api/v1/projects/{p['id']}/run", headers=auth_headers, json={})).json()
        assert await asyncio.wait_for(heard.get(), timeout=5) == run["id"]
        async with run_db() as db:                  # ... and the row is visible to a fresh session by then
            assert (await db.get(Run, uuid.UUID(run["id"]))).status == "queued"
    finally:
        await conn.close()


async def test_the_listener_wakes_the_reconciler_when_a_run_is_announced(run_db):
    task = asyncio.create_task(run_reconciler._listen_for_queued_runs())
    try:
        await asyncio.sleep(1.0)                    # let it connect and LISTEN
        async with run_db() as db:
            await db.execute(text("SELECT pg_notify(:c, :p)"), {"c": RUN_QUEUED_CHANNEL, "p": str(uuid.uuid4())})
            await db.commit()
        await asyncio.wait_for(run_reconciler._wake_event().wait(), timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_leader_loop_dispatches_a_queued_run_without_waiting_for_the_full_sweep(run_db, monkeypatch, tmp_path):
    """Full reconcile every 10 minutes, queue poll every second: a run submitted after the first tick still starts within seconds."""
    settings = Settings(run_reconciler_poll_interval_seconds=600, run_queue_poll_seconds=1)
    monkeypatch.setattr(run_reconciler, "get_settings", lambda: settings)
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    (clone_dir / "dbt_project.yml").write_text("name: x\n")
    executed = []

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
            executed.append(kwargs["run_id"])
            return {"success": True, "errors": [], "nodes": []}

    monkeypatch.setattr("app.services.run_service.get_git_service", lambda: FakeGit())
    monkeypatch.setattr("app.services.run_service.get_runner_isolation", lambda: FakeIsolation())

    async with run_db() as db:
        project = Project(org_id=get_settings().org_id, name="a", git_url="https://github.com/acme/a-loop.git", git_ref="main", engine="pg_lake")
        db.add(project)
        await db.flush()
        run = Run(project_id=project.id, trigger="manual", status="queued", attempt=0)
        db.add(run)
        await db.commit()
        run_id = run.id

    loop_task = asyncio.create_task(run_reconciler.reconciler_loop())
    try:
        for _ in range(100):
            await asyncio.sleep(0.1)
            async with run_db() as db:
                if (await db.get(Run, run_id)).status == "succeeded":
                    break
        async with run_db() as db:
            assert (await db.get(Run, run_id)).status == "succeeded"
        assert executed == [run_id]                 # exactly once: one owner
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
