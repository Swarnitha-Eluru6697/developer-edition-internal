"""WS-EXEC-005/006: execution status changes are atomic, single-winner
transitions — so a row runs once across replicas and gets exactly one
terminal event that matches the database.

Runs the real conditional UPDATEs against an in-memory SQLite `executions`
table (the only table these code paths touch).
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Execution
from app.services import execution_router as er
from app.services.execution_router import ExecutionRouter, transition


@pytest.fixture
def sessionmaker(monkeypatch, tmp_path):
    # A file DB with NullPool: every session gets its own connection, like
    # Postgres. (":memory:" shares ONE connection across sessions, so
    # concurrent runs would interleave their transactions on it.)
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'exec.db'}", poolclass=NullPool,
        connect_args={"timeout": 30},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as c:
            await c.run_sync(lambda sc: Execution.__table__.create(sc))
    asyncio.run(_setup())
    import app.db.session as db_session
    monkeypatch.setattr(db_session, "AsyncSessionLocal", Session)
    yield Session
    asyncio.run(engine.dispose())


class _Redis:
    """Just the Redis surface the router uses: pub/sub publish, plain keys
    (the cancel wake-up hint) and a lock. `fail` names operations that raise,
    to model an outage; `flush()` models a restart that loses every key."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.keys: dict[str, str] = {}
        self.fail: set[str] = set()
        self.publish_calls = 0

    def _check(self, op):
        if op in self.fail:
            raise ConnectionError(f"redis down ({op})")

    def flush(self):
        self.keys.clear()

    async def publish(self, channel, payload):
        self.publish_calls += 1
        self._check("publish")
        if "publish_first" in self.fail and self.publish_calls == 1:      # the `execution.started` event only
            raise ConnectionError("redis down (first publish)")
        self.published.append((channel, payload))

    async def set(self, key, value, ex=None):
        self._check("set")
        self.keys[key] = value

    async def exists(self, *keys):
        self._check("exists")
        return sum(k in self.keys for k in keys)

    async def delete(self, *keys):
        self._check("delete")
        for k in keys:
            self.keys.pop(k, None)

    def lock(self, *_a, **_k):
        import contextlib

        @contextlib.asynccontextmanager
        async def _lock():
            yield
        return _lock()

    def pubsub(self):
        raise ConnectionError("no pubsub in tests")   # wake listener just backs off


@pytest.fixture
def redis(monkeypatch):
    r = _Redis()
    monkeypatch.setattr(er, "get_redis", lambda: r)
    return r


def _terminal_events(redis, execution_id):
    import json
    return [
        json.loads(p)["type"] for ch, p in redis.published
        if ch == er.channel_for(str(execution_id))
        and json.loads(p)["type"] in er.ExecutionRouter._TERMINAL_EVENT_FOR_STATUS.values()
    ]


async def _add(Session, **kw) -> uuid.UUID:
    ex = Execution(
        id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=uuid.uuid4(), workspace_id=uuid.uuid4(),
        language="sql", code_text="select 1", code_sha256="x", target=kw.pop("target", "lakehouse"),
        max_rows=10, timeout_seconds=30, status=kw.pop("status", "pending"),
        created_at=datetime.now(timezone.utc), **kw,
    )
    async with Session() as db:
        db.add(ex)
        await db.commit()
    return ex.id


async def _status(Session, eid):
    async with Session() as db:
        return (await db.get(Execution, eid)).status


# ── transition() ─────────────────────────────────────────────────────────

def test_claim_has_exactly_one_winner(sessionmaker):
    async def main():
        eid = await _add(sessionmaker)

        async def claim():
            async with sessionmaker() as db:
                return await transition(db, eid, "running", from_=("pending",),
                                        started_at=datetime.now(timezone.utc))
        results = await asyncio.gather(*(claim() for _ in range(5)))
        assert sum(r is not None for r in results) == 1
        assert await _status(sessionmaker, eid) == "running"
    asyncio.run(main())


def test_terminal_state_is_never_overwritten(sessionmaker):
    async def main():
        eid = await _add(sessionmaker, status="running")
        async with sessionmaker() as db:
            assert await transition(db, eid, "failed", error_code="INTERRUPTED") is not None
            # A late worker finishing the same row loses — no overwrite.
            assert await transition(db, eid, "completed", from_=("running",)) is None
        assert await _status(sessionmaker, eid) == "failed"
    asyncio.run(main())


def test_returned_row_is_fresh_not_identity_map_copy(sessionmaker):
    async def main():
        eid = await _add(sessionmaker)
        async with sessionmaker() as db:
            stale = await db.get(Execution, eid)          # cached as 'pending'
            row = await transition(db, eid, "running", started_at=datetime.now(timezone.utc))
            assert row is stale and row.status == "running"
    asyncio.run(main())


@pytest.mark.parametrize("bad", [("completed",), ("cancelled",), ("bogus",)])
def test_invalid_transitions_are_rejected(sessionmaker, bad):
    async def main():
        eid = await _add(sessionmaker)
        async with sessionmaker() as db:
            with pytest.raises(ValueError):
                await transition(db, eid, "running", from_=bad)
    asyncio.run(main())


def test_no_transition_out_of_a_terminal_state(sessionmaker):
    async def main():
        eid = await _add(sessionmaker, status="completed")
        async with sessionmaker() as db:
            for to in ("running", "failed", "cancelled", "timeout"):
                assert await transition(db, eid, to) is None
        assert await _status(sessionmaker, eid) == "completed"
    asyncio.run(main())


# ── ExecutionRouter end to end ──────────────────────────────────────────

class _Chunk:
    def __init__(self, output_type, payload, row_count=0, is_final=False):
        self.output_type, self.payload, self.row_count, self.is_final = output_type, payload, row_count, is_final


class _Sql:
    """Fake lakehouse executor. `block` makes a run wait until cancelled."""

    def __init__(self, chunks, block=False):
        self.chunks, self.block = chunks, block
        self.runs = 0
        self.cancels: list[str] = []

    async def execute(self, **_kw):
        self.runs += 1
        for c in self.chunks:
            yield c
        if self.block:
            await asyncio.Event().wait()

    async def cancel(self, *, execution_id, user_id="", target=None):
        self.cancels.append(execution_id)


def _router(chunks, block=False):
    router = ExecutionRouter()
    router._sql = _Sql(chunks, block)
    return router


def _create_outputs_table(sessionmaker):
    from app.models import ExecutionOutput

    async def _c():
        async with sessionmaker() as db:
            conn = await db.connection()
            await conn.run_sync(lambda sc: ExecutionOutput.__table__.create(sc, checkfirst=True))
            await db.commit()
    asyncio.run(_c())


_DONE = [_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1), _Chunk("done", {}, is_final=True)]


def test_two_workers_run_a_row_once_with_one_terminal_event(sessionmaker, redis):
    _create_outputs_table(sessionmaker)
    router = _router(_DONE)

    async def main():
        eid = await _add(sessionmaker)
        await asyncio.gather(router._run(eid), router._run(eid))   # two workers, same row
        assert router._sql.runs == 1
        assert await _status(sessionmaker, eid) == "completed"
        assert _terminal_events(redis, eid) == ["execution.completed"]
        assert redis.keys == {}                                    # lease released
    asyncio.run(main())


def test_row_count_is_not_double_counted(sessionmaker, redis):
    _create_outputs_table(sessionmaker)
    router = _router(_DONE)

    async def main():
        eid = await _add(sessionmaker)
        await router._run(eid)
        async with sessionmaker() as db:
            assert (await db.get(Execution, eid)).row_count == 1
    asyncio.run(main())


def test_error_chunk_ends_failed_with_matching_event(sessionmaker, redis):
    _create_outputs_table(sessionmaker)
    router = _router([_Chunk("error", {"error_code": "QUERY_ERROR", "error_message": "boom"}, is_final=True)])

    async def main():
        eid = await _add(sessionmaker)
        await router._run(eid)
        assert await _status(sessionmaker, eid) == "failed"
        assert _terminal_events(redis, eid) == ["execution.failed"]
    asyncio.run(main())


def test_cancel_pending_then_worker_skips_it(sessionmaker, redis):
    _create_outputs_table(sessionmaker)
    router = _router(_DONE)

    async def main():
        eid = await _add(sessionmaker)
        assert await router.cancel(eid, "sql") == "cancelled"
        await router._run(eid)
        assert router._sql.runs == 0
        assert await _status(sessionmaker, eid) == "cancelled"
        assert _terminal_events(redis, eid) == ["execution.cancelled"]
    asyncio.run(main())


def test_cancel_running_stops_it_and_ends_cancelled(sessionmaker, redis):
    _create_outputs_table(sessionmaker)
    router = _router([_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1)], block=True)

    async def main():
        eid = await _add(sessionmaker)
        run = asyncio.create_task(router._run(eid))
        for _ in range(50):                       # wait until it's running
            await asyncio.sleep(0.05)
            if await _status(sessionmaker, eid) == "running":
                break
        assert await router.cancel(eid, "sql") == "cancelling"
        await asyncio.wait_for(run, timeout=10)   # the watcher honours it
        assert router._sql.cancels == [str(eid)]  # engine was told to stop
        assert await _status(sessionmaker, eid) == "cancelled"
        assert _terminal_events(redis, eid) == ["execution.cancelled"]
        assert await router.cancel(eid, "sql") == "finished"
    asyncio.run(main())


def test_dispatch_loop_runs_pending_rows_with_bounded_concurrency(sessionmaker, redis, monkeypatch):
    _create_outputs_table(sessionmaker)
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "execution_worker_concurrency", 2)
    monkeypatch.setattr(get_settings(), "execution_poll_interval_seconds", 0.05)
    router = _router(_DONE)
    peak = {"now": 0, "max": 0}
    real_run = router._run

    async def tracking_run(eid):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        try:
            await asyncio.sleep(0.05)
            await real_run(eid)
        finally:
            peak["now"] -= 1
    router._run = tracking_run

    async def main():
        ids = [await _add(sessionmaker) for _ in range(5)]
        await router.start()
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if all([await _status(sessionmaker, i) == "completed" for i in ids]):
                    break
            assert [await _status(sessionmaker, i) for i in ids] == ["completed"] * 5
            assert peak["max"] <= 2
            assert router.healthy()
        finally:
            await router.stop()
        assert not router.healthy()
    asyncio.run(main())


def test_reconcile_recovers_rows_whose_lease_lapsed(sessionmaker, redis):
    """The lease is the row's own `lease_expires_at` (W-018): Redis is not asked at all."""
    async def main():
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=10)
        lapsed = {"lease_owner": "w1", "lease_expires_at": now - timedelta(seconds=5)}
        dead = await _add(sessionmaker, status="running", started_at=old, **lapsed)
        alive = await _add(sessionmaker, status="running", started_at=old,
                           lease_owner="w2", lease_expires_at=now + timedelta(seconds=20))
        just_claimed = await _add(sessionmaker, status="running", started_at=now,
                                  lease_owner="w3", lease_expires_at=now + timedelta(seconds=30))
        cancelled = await _add(sessionmaker, status="running", started_at=old, cancel_requested_at=old, **lapsed)
        router = _router([])
        await router._reconcile()
        await router._reconcile()          # a second worker reconciling again
        assert await _status(sessionmaker, dead) == "failed"
        assert await _status(sessionmaker, alive) == "running"
        assert await _status(sessionmaker, just_claimed) == "running"
        assert await _status(sessionmaker, cancelled) == "cancelled"
        assert _terminal_events(redis, dead) == ["execution.failed"]
        assert _terminal_events(redis, cancelled) == ["execution.cancelled"]
    asyncio.run(main())


def test_connection_run_is_reauthorized_at_run_time(sessionmaker, redis, monkeypatch):
    _create_outputs_table(sessionmaker)
    router = _router([])

    async def denied(_db, _execution):
        return None, ("FORBIDDEN", "Not authorized to use connection 'sales': grant revoked")
    monkeypatch.setattr(router, "_authorize_connection", denied)
    ran = []

    async def _never(**_kw):
        ran.append(1)
        yield None
    monkeypatch.setattr(router._conn_sql, "execute", _never)

    async def main():
        eid = await _add(sessionmaker, target=f"connection:{uuid.uuid4()}")
        await router._run(eid)
        async with sessionmaker() as db:
            row = await db.get(Execution, eid)
        assert row.status == "failed" and row.error_code == "FORBIDDEN"
        assert ran == []
    asyncio.run(main())


# ── W-017: a cancel request survives Redis ──────────────────────────────

async def _row(Session, eid) -> Execution:
    async with Session() as db:
        return await db.get(Execution, eid, populate_existing=True)


async def _wait_running(Session, eid):
    for _ in range(100):
        await asyncio.sleep(0.05)
        if await _status(Session, eid) == "running":
            return
    raise AssertionError("never started")


def test_a_cancel_request_is_stored_on_the_row(sessionmaker, redis):
    async def main():
        eid = await _add(sessionmaker, status="running", started_at=datetime.now(timezone.utc))
        assert (await _row(sessionmaker, eid)).cancel_requested_at is None
        assert await _router([]).cancel(eid, "sql") == "cancelling"
        assert (await _row(sessionmaker, eid)).cancel_requested_at is not None
    asyncio.run(main())


def test_a_cancel_survives_a_redis_flush_and_the_run_ends_cancelled_not_interrupted(sessionmaker, redis):
    """THE W-017 reproduction: cancel, Redis loses its keys, the worker dies; the user's cancel must still count."""
    async def main():
        now = datetime.now(timezone.utc)
        eid = await _add(sessionmaker, status="running", started_at=now - timedelta(minutes=5),
                         lease_owner="dead", lease_expires_at=now - timedelta(minutes=1))
        router = _router([])
        assert await router.cancel(eid, "sql") == "cancelling"
        redis.flush()                                          # restart / FLUSHALL / eviction
        await router._reconcile()
        row = await _row(sessionmaker, eid)
        assert (row.status, row.error_code) == ("cancelled", None)
        assert _terminal_events(redis, eid) == ["execution.cancelled"]
    asyncio.run(main())


def test_a_cancel_is_accepted_while_redis_is_down(sessionmaker, redis):
    async def main():
        redis.fail = {"set", "exists", "delete"}
        eid = await _add(sessionmaker, status="running", started_at=datetime.now(timezone.utc))
        assert await _router([]).cancel(eid, "sql") == "cancelling"
        assert (await _row(sessionmaker, eid)).cancel_requested_at is not None
    asyncio.run(main())


def test_a_running_worker_honours_a_cancel_recorded_only_in_the_database(sessionmaker, redis, monkeypatch):
    """Redis never heard of this cancel (down / flushed): the worker still finds it on the row."""
    _create_outputs_table(sessionmaker)
    monkeypatch.setattr(er.get_settings(), "execution_lease_seconds", 3)      # worker checks the row about every second
    router = _router([_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1)], block=True)

    async def main():
        eid = await _add(sessionmaker)
        run = asyncio.create_task(router._run(eid))
        await _wait_running(sessionmaker, eid)
        redis.fail = {"set", "exists", "delete"}
        assert await router.cancel(eid, "sql") == "cancelling"
        await asyncio.wait_for(run, timeout=10)
        assert router._sql.cancels == [str(eid)]
        assert await _status(sessionmaker, eid) == "cancelled"
        assert _terminal_events(redis, eid) == ["execution.cancelled"]
    asyncio.run(main())


def test_a_cancel_is_honoured_even_when_the_redis_hint_is_flushed(sessionmaker, redis, monkeypatch):
    _create_outputs_table(sessionmaker)
    monkeypatch.setattr(er.get_settings(), "execution_lease_seconds", 3)
    router = _router([_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1)], block=True)

    async def main():
        eid = await _add(sessionmaker)
        run = asyncio.create_task(router._run(eid))
        await _wait_running(sessionmaker, eid)
        await router.cancel(eid, "sql")
        redis.flush()                                           # even the wake-up is gone
        await asyncio.wait_for(run, timeout=10)
        assert await _status(sessionmaker, eid) == "cancelled"
    asyncio.run(main())


def test_cancelling_twice_keeps_the_first_request_time(sessionmaker, redis):
    async def main():
        eid = await _add(sessionmaker, status="running", started_at=datetime.now(timezone.utc))
        router = _router([])
        await router.cancel(eid, "sql")
        first = (await _row(sessionmaker, eid)).cancel_requested_at
        await asyncio.sleep(0.05)
        assert await router.cancel(eid, "sql") == "cancelling"
        assert (await _row(sessionmaker, eid)).cancel_requested_at == first
    asyncio.run(main())


def test_cancelling_a_finished_or_unknown_execution_records_nothing(sessionmaker, redis):
    async def main():
        done = await _add(sessionmaker, status="completed")
        router = _router([])
        assert await router.cancel(done, "sql") == "finished"
        assert await router.cancel(uuid.uuid4(), "sql") == "finished"
        assert (await _row(sessionmaker, done)).cancel_requested_at is None
    asyncio.run(main())


def test_a_pending_cancel_needs_no_redis_at_all(sessionmaker, redis):
    async def main():
        redis.fail = {"set", "exists", "delete"}
        eid = await _add(sessionmaker)
        assert await _router([]).cancel(eid, "sql") == "cancelled"
        assert (await _row(sessionmaker, eid)).cancel_requested_at is None     # never ran: nothing to remember
    asyncio.run(main())


# ── W-018: a running row always has a durable owner ─────────────────────

def test_the_claim_sets_the_lease_in_the_same_statement(sessionmaker, redis):
    """Seen from inside the run: the row is running AND already owned, with Redis unusable for leases."""
    _create_outputs_table(sessionmaker)
    router = _router(_DONE)
    seen = {}

    async def execute(**_kw):
        seen["row"] = await _row(sessionmaker, seen["eid"])
        for c in _DONE:
            yield c
    router._sql.execute = execute

    async def main():
        redis.fail = {"set", "exists", "delete"}
        eid = seen["eid"] = await _add(sessionmaker)
        await router._run(eid)
        row = seen["row"]
        assert row.status == "running"
        assert row.lease_owner and row.lease_owner == router.owner
        assert er._as_utc(row.lease_expires_at) > datetime.now(timezone.utc)
        assert await _status(sessionmaker, eid) == "completed"
    asyncio.run(main())


def test_a_redis_outage_after_the_claim_cannot_strand_the_row(sessionmaker, redis):
    """THE W-018 reproduction: Redis fails right after the claim. It used to leave a running row with no lease and no run."""
    _create_outputs_table(sessionmaker)
    router = _router(_DONE)

    async def main():
        redis.fail = {"set", "exists", "delete"}
        eid = await _add(sessionmaker)
        await router._run(eid)
        assert router._sql.runs == 1
        assert await _status(sessionmaker, eid) == "completed"
        assert _terminal_events(redis, eid) == ["execution.completed"]
    asyncio.run(main())


def test_a_failed_started_event_does_not_strand_a_claimed_row(sessionmaker, redis):
    _create_outputs_table(sessionmaker)
    router = _router(_DONE)

    async def main():
        redis.fail = {"publish_first"}
        eid = await _add(sessionmaker)
        await router._run(eid)
        assert router._sql.runs == 1
        assert await _status(sessionmaker, eid) == "completed"
    asyncio.run(main())


def test_the_worker_extends_its_lease_in_the_database_while_it_runs(sessionmaker, redis, monkeypatch):
    _create_outputs_table(sessionmaker)
    monkeypatch.setattr(er.get_settings(), "execution_lease_seconds", 3)
    router = _router([_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1)], block=True)

    async def main():
        eid = await _add(sessionmaker)
        run = asyncio.create_task(router._run(eid))
        await _wait_running(sessionmaker, eid)
        first = er._as_utc((await _row(sessionmaker, eid)).lease_expires_at)
        await asyncio.sleep(2.5)
        later = er._as_utc((await _row(sessionmaker, eid)).lease_expires_at)
        assert later > first + timedelta(seconds=1)
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run
    asyncio.run(main())


def test_a_dead_worker_is_recovered_from_the_database_alone(sessionmaker, redis, monkeypatch):
    """The worker is killed mid-run (no cleanup); the next reconcile, with Redis unreachable, recovers it."""
    _create_outputs_table(sessionmaker)
    monkeypatch.setattr(er.get_settings(), "execution_lease_seconds", 1)
    router = _router([_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1)], block=True)

    async def main():
        eid = await _add(sessionmaker)
        run = asyncio.create_task(router._run(eid))
        await _wait_running(sessionmaker, eid)
        run.cancel()                                          # the worker process dies
        with contextlib.suppress(asyncio.CancelledError):
            await run
        redis.fail = {"exists", "set", "delete"}
        await router._reconcile()
        assert await _status(sessionmaker, eid) == "running"   # lease still valid: not recovered early
        await asyncio.sleep(1.2)
        await router._reconcile()
        row = await _row(sessionmaker, eid)
        assert (row.status, row.error_code) == ("failed", "INTERRUPTED")
    asyncio.run(main())


def test_a_row_with_no_lease_columns_falls_back_to_the_budget_rule(sessionmaker, redis):
    """Claimed by an older worker (no lease in the row): only recovered once it overran its own timeout + grace."""
    async def main():
        now = datetime.now(timezone.utc)
        within = await _add(sessionmaker, status="running", started_at=now - timedelta(seconds=40))      # timeout 30 + grace 60
        overran = await _add(sessionmaker, status="running", started_at=now - timedelta(seconds=400))
        await _router([])._reconcile()
        assert await _status(sessionmaker, within) == "running"
        assert await _status(sessionmaker, overran) == "failed"
    asyncio.run(main())


def test_a_worker_that_lost_its_row_stops_running_it(sessionmaker, redis, monkeypatch):
    """The reconciler gave up on this run (e.g. a long DB outage). The worker must stop, not carry on in the dark."""
    _create_outputs_table(sessionmaker)
    monkeypatch.setattr(er.get_settings(), "execution_lease_seconds", 3)
    router = _router([_Chunk("rows", {"columns": [], "rows": [[1]]}, row_count=1)], block=True)

    async def main():
        eid = await _add(sessionmaker)
        run = asyncio.create_task(router._run(eid))
        await _wait_running(sessionmaker, eid)
        async with sessionmaker() as db:
            assert await transition(db, eid, "failed", from_=("running",), error_code="INTERRUPTED",
                                    completed_at=datetime.now(timezone.utc))
        await asyncio.wait_for(run, timeout=10)
        assert router._sql.cancels == [str(eid)]
        assert await _status(sessionmaker, eid) == "failed"
    asyncio.run(main())


def test_finishing_a_run_is_not_held_up_by_its_lease_heartbeat(sessionmaker, redis):
    """The watcher is cancelled when the run ends, possibly mid-heartbeat. That write must not be left holding the row
    (or, on SQLite, the whole file) against the run's own final transition: runs that end at once must not stall."""
    _create_outputs_table(sessionmaker)

    async def main():
        for _ in range(8):
            router = _router(_DONE)
            eid = await _add(sessionmaker)
            await asyncio.wait_for(router._run(eid), timeout=5)
            assert await _status(sessionmaker, eid) == "completed"
    asyncio.run(main())
