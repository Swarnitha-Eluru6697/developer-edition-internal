"""FL-011: deploy and stop have ONE owner — the leader's durable reconciler loop.

deploy()/stop() used to start execution themselves with an in-process asyncio task (holding the deployment's lease) while the reconciler could
also take the same work over later: two trigger paths, and the latency of a deploy depended on which request process happened to receive it.
Now they persist the desired state (job 'deploying'/'pausing' + an UNOWNED deployment row) and announce it with a NOTIFY delivered at commit;
only the leader's loop claims and drives it.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.config import Settings
from app.models import FlinkDeployment, FlinkJob
from app.schemas import DeployRequest, StopRequest
from app.services import deployment_reconciler as dr
from app.services.job_service import WORK_CHANNEL, JobService
from tests.fakes import FakeFlinkClient, leader_tick, patch_flink_client


@pytest.fixture
def flink_db(pg_db):
    return pg_db


async def _validated_job(flink_db) -> uuid.UUID:
    async with flink_db() as db:
        job = FlinkJob(org_id="org1", name=f"j-{uuid.uuid4().hex[:8]}", sql="INSERT INTO t SELECT 1;", status="validated")
        db.add(job)
        await db.commit()
        return job.id


async def _deploy(flink_db, job_id) -> FlinkDeployment:
    async with flink_db() as db:
        dep = await JobService(db).deploy("org1", job_id, "u", DeployRequest())
        await db.commit()
        return dep


async def _status(flink_db, job_id) -> str:
    async with flink_db() as db:
        return (await db.get(FlinkJob, job_id)).status


async def test_deploy_persists_unowned_work_and_executes_nothing(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _validated_job(flink_db)
    dep = await _deploy(flink_db, job_id)
    await asyncio.sleep(0.4)                                        # a stray in-process task would have run by now
    assert fake.execute_statements_calls == 0
    assert not [t for t in asyncio.all_tasks() if "_execute_deploy" in repr(t.get_coro()) or "dispatch_deploy" in repr(t.get_coro())]
    async with flink_db() as db:
        row = await db.get(FlinkDeployment, dep.id)
        assert row.status == "submitting" and row.owner_id is None and row.lease_expires_at is None
    assert await _status(flink_db, job_id) == "deploying"


async def test_the_leader_loop_picks_the_work_up_and_runs_it_exactly_once(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _validated_job(flink_db)
    await _deploy(flink_db, job_id)
    await asyncio.gather(leader_tick(), leader_tick(), leader_tick())      # overlapping sweeps must not start it twice
    for _ in range(100):
        if await _status(flink_db, job_id) == "running":
            break
        await leader_tick()
        await asyncio.sleep(0.05)
    assert await _status(flink_db, job_id) == "running"
    assert fake.execute_statements_calls == 1


async def test_stop_persists_unowned_work_and_the_leader_loop_runs_it(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    job_id = await _validated_job(flink_db)
    await _deploy(flink_db, job_id)
    for _ in range(100):
        if await _status(flink_db, job_id) == "running":
            break
        await leader_tick()
        await asyncio.sleep(0.05)
    async with flink_db() as db:
        dep = await JobService(db).stop("org1", job_id, StopRequest())
        await db.commit()
        stop_id = dep.id
    await asyncio.sleep(0.3)
    async with flink_db() as db:
        row = await db.get(FlinkDeployment, stop_id)
        assert row.owner_id is None and row.lease_expires_at is None
    assert await _status(flink_db, job_id) == "pausing"                # nothing stopped it in the request process
    for _ in range(100):
        if await _status(flink_db, job_id) == "paused":
            break
        await leader_tick()
        await asyncio.sleep(0.05)
    assert await _status(flink_db, job_id) == "paused"


async def test_deploy_announces_the_work_at_commit(flink_db, monkeypatch):
    import asyncpg

    from app.db import session as session_mod

    patch_flink_client(monkeypatch, FakeFlinkClient())
    dsn = session_mod.engine.url.render_as_string(hide_password=False).replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    heard: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(WORK_CHANNEL, lambda _c, _pid, _ch, payload: heard.put_nowait(payload))
    try:
        job_id = await _validated_job(flink_db)
        dep = await _deploy(flink_db, job_id)
        assert await asyncio.wait_for(heard.get(), timeout=5) == str(dep.id)
    finally:
        await conn.close()


async def test_the_listener_wakes_the_loop(flink_db):
    from sqlalchemy import text
    dr._wake = None
    task = asyncio.create_task(dr._listen_for_work())
    try:
        await asyncio.sleep(1.0)
        async with flink_db() as db:
            await db.execute(text("SELECT pg_notify(:c, :p)"), {"c": WORK_CHANNEL, "p": str(uuid.uuid4())})
            await db.commit()
        await asyncio.wait_for(dr._wake_event().wait(), timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        dr._wake = None


async def test_the_loop_starts_a_deploy_without_waiting_for_the_full_sweep(flink_db, monkeypatch):
    """Full sweep every 10 minutes, fast poll every second: a deploy persisted after the first tick still starts within seconds."""
    settings = Settings(deployment_reconciler_poll_interval_seconds=600, deployment_dispatch_poll_seconds=0.5)
    monkeypatch.setattr(dr, "get_settings", lambda: settings)
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    dr._wake = None
    loop_task = asyncio.create_task(dr.reconciler_loop())
    try:
        await asyncio.sleep(1.0)                                   # the first (full) sweep has run: nothing was waiting
        job_id = await _validated_job(flink_db)
        await _deploy(flink_db, job_id)                            # persisted AFTER it, so only the fast path can start it
        for _ in range(150):
            await asyncio.sleep(0.1)
            if await _status(flink_db, job_id) == "running":
                break
        assert await _status(flink_db, job_id) == "running"
        assert fake.execute_statements_calls == 1
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        dr._wake = None


async def test_a_slow_deploy_does_not_hold_up_another_jobs_deploy(flink_db, monkeypatch):
    fake = FakeFlinkClient()
    patch_flink_client(monkeypatch, fake)
    gate = asyncio.Event()
    orig = fake.execute_statements
    first = {"seen": False}

    async def slow_first(session_id, sql, **kw):
        if not first["seen"]:
            first["seen"] = True
            await gate.wait()
        return await orig(session_id, sql, **kw)
    monkeypatch.setattr(fake, "execute_statements", slow_first)
    a, b = await _validated_job(flink_db), await _validated_job(flink_db)
    await _deploy(flink_db, a)
    await _deploy(flink_db, b)
    await leader_tick()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if "running" in (await _status(flink_db, a), await _status(flink_db, b)):
            break
    assert sorted([await _status(flink_db, a), await _status(flink_db, b)]) == ["deploying", "running"]
    gate.set()
    await dr._drain_in_flight()


async def test_stop_announces_the_work_at_commit(flink_db, monkeypatch):
    import asyncpg
    from app.db import session as session_mod

    patch_flink_client(monkeypatch, FakeFlinkClient())
    job_id = await _validated_job(flink_db)
    await _deploy(flink_db, job_id)
    for _ in range(100):
        if await _status(flink_db, job_id) == "running":
            break
        await leader_tick()
        await asyncio.sleep(0.05)
    dsn = session_mod.engine.url.render_as_string(hide_password=False).replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    heard: asyncio.Queue = asyncio.Queue()
    await conn.add_listener(WORK_CHANNEL, lambda _c, _pid, _ch, payload: heard.put_nowait(payload))
    try:
        async with flink_db() as db:
            dep = await JobService(db).stop("org1", job_id, StopRequest())
            await db.commit()
        assert await asyncio.wait_for(heard.get(), timeout=5) == str(dep.id)
    finally:
        await conn.close()
