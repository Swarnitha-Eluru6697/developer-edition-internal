"""Leader election for the background loops (RUN-DBT-003).

The scheduler, run reconciler and metadata outbox loops must run ONCE per
data plane — not once per replica, and not once per uvicorn worker (the
Dockerfile runs `--workers 2`). Each process contends for one Postgres
session-level advisory lock on a DEDICATED connection and holds it for as
long as it runs the loops. Postgres releases the lock automatically if that
connection (or the process) dies, at which point another process's next
attempt acquires it.

The leader also re-verifies its connection on every check interval; if the
connection is gone the lock is gone too, so it stops its loops immediately
rather than running alongside a new leader.

Correctness never depends on this alone — every run transition is still a
lease-guarded CAS and the scheduler still has its UNIQUE occurrence
constraint — but it removes the duplicate polling work and log noise.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import structlog
from sqlalchemy import text

from app.core.config import get_settings

log = structlog.get_logger()

# Constant 63-bit key: ascii "dbtrunLD".
LEADER_LOCK_KEY = 0x646274_72756E4C44 & 0x7FFFFFFFFFFFFFFF


def _engine():
    from app.db import session as db_session
    return db_session.engine


class LeaderElector:
    def __init__(self, key: int = LEADER_LOCK_KEY, engine=None):
        self.key = key
        self._engine = engine
        self._conn = None
        self.is_leader = False

    @property
    def engine(self):
        return self._engine or _engine()

    async def try_acquire(self) -> bool:
        if self.is_leader:
            return True
        if self.engine.dialect.name != "postgresql":
            # SQLite/dev: single process, nothing to elect against.
            self.is_leader = True
            return True
        conn = await self.engine.connect()
        try:
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})).scalar()
            await conn.commit()
        except Exception:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._conn = conn
        self.is_leader = True
        log.info("leader_acquired", key=self.key)
        return True

    async def still_leader(self) -> bool:
        if not self.is_leader:
            return False
        if self._conn is None:
            return True  # non-postgres
        try:
            # Not just "is the connection alive": SQLAlchemy may have
            # transparently reconnected, and a new backend does NOT hold the
            # lock. Ask Postgres whether THIS backend holds it.
            held = (await self._conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND granted AND objsubid = 1 "
                "AND ((classid::bigint << 32) | objid::bigint) = :k)"), {"k": self.key})).scalar()
            await self._conn.commit()
        except Exception:  # noqa: BLE001
            held = False
        if not held:
            log.error("leader_lock_lost")
            await self._drop()
            return False
        return True

    async def release(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
                await self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
        await self._drop()

    async def _drop(self) -> None:
        conn, self._conn = self._conn, None
        self.is_leader = False
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass


async def run_as_leader(loops: list[Callable[[], Awaitable[None]]],
                        elector: LeaderElector | None = None) -> None:
    """Forever: contend for leadership; while leader, run `loops`; if
    leadership is lost, cancel them and contend again."""
    settings = get_settings()
    elector = elector or LeaderElector()
    while True:
        try:
            acquired = await elector.try_acquire()
        except Exception:  # noqa: BLE001
            log.exception("leader_acquire_failed")
            acquired = False
        if not acquired:
            await asyncio.sleep(settings.leader_election_retry_seconds)
            continue

        tasks = [asyncio.create_task(loop()) for loop in loops]
        try:
            while await elector.still_leader():
                await asyncio.sleep(settings.leader_election_retry_seconds)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if elector.is_leader:
                await elector.release()
        log.warning("leader_lost_recontending")
