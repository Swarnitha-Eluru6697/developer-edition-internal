"""Leader election (RUN-DBT-003): the background loops run on exactly one
process across replicas and uvicorn workers, via a Postgres session-level
advisory lock held on a dedicated connection."""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from app.services import leader


async def test_only_one_elector_wins_and_failover_works(migrated_test_database):
    eng_a = create_async_engine(migrated_test_database)
    eng_b = create_async_engine(migrated_test_database)
    key = 4242_4242
    a, b = leader.LeaderElector(key=key, engine=eng_a), leader.LeaderElector(key=key, engine=eng_b)
    try:
        assert await a.try_acquire() is True
        assert await b.try_acquire() is False
        assert await a.still_leader() is True
        await a.release()
        assert await b.try_acquire() is True  # failover once the holder lets go
        assert await a.try_acquire() is False
    finally:
        await a.release()
        await b.release()
        await eng_a.dispose()
        await eng_b.dispose()


async def test_lock_is_released_when_the_leader_connection_dies(migrated_test_database):
    eng_a = create_async_engine(migrated_test_database)
    eng_b = create_async_engine(migrated_test_database)
    key = 4242_4243
    a, b = leader.LeaderElector(key=key, engine=eng_a), leader.LeaderElector(key=key, engine=eng_b)
    try:
        assert await a.try_acquire()
        await a._conn.invalidate()  # simulate the leader process/connection dying
        assert await a.still_leader() is False
        assert await b.try_acquire() is True
    finally:
        await a.release()
        await b.release()
        await eng_a.dispose()
        await eng_b.dispose()


async def test_run_as_leader_runs_loops_on_one_process_only(migrated_test_database, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "leader_election_retry_seconds", 0.05)
    eng_a = create_async_engine(migrated_test_database)
    eng_b = create_async_engine(migrated_test_database)
    key = 4242_4244
    ran: list[str] = []

    def loop_for(name):
        async def _loop():
            ran.append(name)
            await asyncio.sleep(3600)
        return _loop

    ta = asyncio.create_task(leader.run_as_leader([loop_for("a")], leader.LeaderElector(key=key, engine=eng_a)))
    await asyncio.sleep(0.2)
    tb = asyncio.create_task(leader.run_as_leader([loop_for("b")], leader.LeaderElector(key=key, engine=eng_b)))
    await asyncio.sleep(0.3)
    assert ran == ["a"]
    ta.cancel()
    await asyncio.gather(ta, return_exceptions=True)
    await asyncio.sleep(0.4)
    assert ran == ["a", "b"]  # took over after the leader stopped
    tb.cancel()
    await asyncio.gather(tb, return_exceptions=True)
    await eng_a.dispose()
    await eng_b.dispose()
