"""Cross-replica coordination (RUN-FLINK-013 / RUN-FLINK-015).

LeaderElector — a Postgres SESSION-level advisory lock held on one
dedicated AUTOCOMMIT connection for as long as this replica is leader.
The background loops (deployment reconciler, metrics poller) only do work
while `await elector.ensure()` is True, so each runs on exactly one replica
at a time. If the leader's connection (or pod) dies, Postgres releases the
lock immediately and the next replica's ensure() acquires it — no stale
lease to wait out. (Previously the metrics poller took a transaction-scoped
lock per tick, so two replicas whose ticks didn't overlap BOTH collected.)

Deployment leases — per-row ownership (owner_id / lease_expires_at /
heartbeat_at on flink_deployments). The process driving a deploy/stop
holds the lease and heartbeats; anyone else (the reconciler) may act on a
deployment only after claiming an expired lease with a compare-and-set
UPDATE, so a resume can never race the original owner.
"""
from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import timedelta

import structlog
from sqlalchemy import or_, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.config import get_settings
from app.db import session as db_session
from app.models import FlinkDeployment

log = structlog.get_logger()

_INSTANCE_ID: str | None = None


def instance_id() -> str:
    global _INSTANCE_ID
    if _INSTANCE_ID is None:
        # Always suffixed per process: a restarted container in the same pod
        # must not inherit the dead process's leases (they have to expire).
        base = get_settings().instance_id or os.environ.get("HOSTNAME") or socket.gethostname()
        _INSTANCE_ID = f"{base}-{uuid.uuid4().hex[:8]}"
    return _INSTANCE_ID


class LeaderElector:
    """Hold `pg_try_advisory_lock(hashtext(name))` for the loop's lifetime."""

    def __init__(self, name: str, engine=None) -> None:
        self.name = name
        self._engine = engine
        self._conn: AsyncConnection | None = None

    @property
    def is_leader(self) -> bool:
        return self._conn is not None

    def _get_engine(self):
        return self._engine or db_session.engine

    async def ensure(self) -> bool:
        """True iff this elector holds the lock right now (acquiring it if
        free). Verifies a held lock's connection is still alive."""
        if self._conn is not None:
            try:
                # Session-level advisory locks live exactly as long as the
                # connection: if this round-trip succeeds we still hold it.
                held = (await self._conn.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND granted"
                ))).scalar()
                if held:
                    return True
                log.warning("leader_lock_lost", name=self.name)
            except Exception as e:  # noqa: BLE001
                log.warning("leader_connection_lost", name=self.name, error=str(e))
            await self._close()
        try:
            conn = await self._get_engine().connect()
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        except Exception as e:  # noqa: BLE001
            log.warning("leader_connect_failed", name=self.name, error=str(e))
            return False
        try:
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:n))"),
                                      {"n": self.name})).scalar()
        except Exception as e:  # noqa: BLE001
            log.warning("leader_lock_attempt_failed", name=self.name, error=str(e))
            got = False
        if not got:
            await conn.close()
            return False
        self._conn = conn
        log.info("leader_acquired", name=self.name, instance=instance_id())
        return True

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(hashtext(:n))"),
                                     {"n": self.name})
        except Exception:  # noqa: BLE001
            pass
        await self._close()

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass


# ── Deployment leases ────────────────────────────────────────────────────

def _lease_ttl() -> timedelta:
    return timedelta(seconds=get_settings().deployment_lease_ttl_seconds)


def lease_values() -> dict:
    """Column values that make THIS instance the lease holder from now."""
    from sqlalchemy import func
    return {"owner_id": instance_id(), "lease_expires_at": func.now() + _lease_ttl(),
            "heartbeat_at": func.now()}


async def claim_lease(db: AsyncSession, deployment_id: uuid.UUID) -> bool:
    """Compare-and-set: take the lease if it is free, expired, or already
    ours. Commits. Returns whether this instance now holds it."""
    from sqlalchemy import func
    me = instance_id()
    result = await db.execute(
        update(FlinkDeployment)
        .where(
            FlinkDeployment.id == deployment_id,
            or_(FlinkDeployment.owner_id.is_(None),
                FlinkDeployment.owner_id == me,
                FlinkDeployment.lease_expires_at.is_(None),
                FlinkDeployment.lease_expires_at < func.now()),
        )
        .values(**lease_values())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def renew_lease(deployment_id: uuid.UUID) -> bool:
    async with db_session.AsyncSessionLocal() as db:
        result = await db.execute(
            update(FlinkDeployment)
            .where(FlinkDeployment.id == deployment_id, FlinkDeployment.owner_id == instance_id())
            .values(**lease_values())
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


async def release_lease(deployment_id: uuid.UUID) -> None:
    try:
        async with db_session.AsyncSessionLocal() as db:
            await db.execute(
                update(FlinkDeployment)
                .where(FlinkDeployment.id == deployment_id,
                       FlinkDeployment.owner_id == instance_id())
                .values(owner_id=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("lease_release_failed", deployment_id=str(deployment_id), error=str(e))


class LeaseHeartbeat:
    """Renews a deployment lease every ttl/3 while a deploy/stop runs."""

    def __init__(self, deployment_id: uuid.UUID) -> None:
        self.deployment_id = deployment_id
        self.lost = False
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        interval = max(1.0, get_settings().deployment_lease_ttl_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                if not await renew_lease(self.deployment_id):
                    self.lost = True
                    log.error("deployment_lease_lost", deployment_id=str(self.deployment_id))
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("deployment_lease_renew_failed",
                            deployment_id=str(self.deployment_id), error=str(e))

    async def __aenter__(self) -> "LeaseHeartbeat":
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await release_lease(self.deployment_id)
