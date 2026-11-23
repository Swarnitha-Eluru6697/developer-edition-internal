"""Run ownership leases (RUN-DBT-003).

Every non-terminal run that is being driven forward has exactly one owner:
the process whose INSTANCE_ID is in runs.lease_owner. The owner renews
runs.lease_expires_at every run_lease_seconds/3 while it is alive (see
RunLease). Every state-changing step of a run — recording its k8s Job,
releasing that Job to start, finalizing — is a compare-and-set guarded by
(status='running', lease_owner=<me>, attempt=<my attempt>), so:

  * a replica that merely *sees* a 'running' row (e.g. one that is still
    cloning, with no Job yet) can never redispatch or fail it while the
    owner's lease is live — take_over_expired() only matches rows whose
    lease has EXPIRED;
  * a cancelled run (status flipped by RunService.cancel()'s CAS) makes
    every subsequent guarded step of the owner fail, so it stops;
  * a stale owner that lost its lease to a successor cannot finalize or
    touch the successor's resources (attempt/owner no longer match).

All timestamps compared here are the DATABASE's now(), never a replica's
local clock, so clock skew between pods cannot expire a live lease early.
"""
from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

import structlog
from sqlalchemy import func, or_, select, update

from app.core.config import get_settings
from app.models import Run

log = structlog.get_logger()

# Unique per process (pod AND uvicorn worker).
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _session():
    from app.db import session as db_session  # resolved at call time (tests patch it)
    return db_session.AsyncSessionLocal()


def _lease_interval() -> timedelta:
    return timedelta(seconds=get_settings().run_lease_seconds)


def _owned(run_id: uuid.UUID, attempt: int, owner: str):
    return (Run.id == run_id, Run.status == "running", Run.lease_owner == owner, Run.attempt == attempt)


async def claim_queued(run_id: uuid.UUID, owner: str = INSTANCE_ID) -> int | None:
    """queued -> running, taking the lease and starting a new attempt.
    Returns the attempt number, or None if someone else got there first
    (or the run was cancelled)."""
    async with _session() as db:
        result = await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "queued")
            .values(
                status="running",
                started_at=func.coalesce(Run.started_at, func.now()),
                attempt=Run.attempt + 1,
                lease_owner=owner,
                lease_expires_at=func.now() + _lease_interval(),
                heartbeat_at=func.now(),
                k8s_job_name=None,
                job_released_at=None,
            )
            .returning(Run.attempt)
        )
        attempt = result.scalar_one_or_none()
        await db.commit()
        return attempt


@dataclass
class TakenOver:
    run_id: uuid.UUID
    project_id: uuid.UUID
    attempt: int
    k8s_job_name: str | None
    job_released_at: datetime | None
    result_ref: str | None


async def take_over_expired(run_id: uuid.UUID, owner: str = INSTANCE_ID) -> TakenOver | None:
    """Take the lease of a 'running' run whose owner stopped heartbeating.
    A live lease is NEVER taken — that is the whole point."""
    async with _session() as db:
        result = await db.execute(
            update(Run)
            .where(
                Run.id == run_id, Run.status == "running",
                or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < func.now()),
            )
            .values(lease_owner=owner, lease_expires_at=func.now() + _lease_interval(),
                    heartbeat_at=func.now())
            .returning(Run.project_id, Run.attempt, Run.k8s_job_name, Run.job_released_at,
                       Run.result_ref)
        )
        row = result.one_or_none()
        await db.commit()
    if row is None:
        return None
    return TakenOver(run_id=run_id, project_id=row[0], attempt=row[1], k8s_job_name=row[2],
                     job_released_at=row[3], result_ref=row[4])


async def renew(run_id: uuid.UUID, attempt: int, owner: str = INSTANCE_ID) -> bool:
    async with _session() as db:
        result = await db.execute(
            update(Run).where(*_owned(run_id, attempt, owner))
            .values(lease_expires_at=func.now() + _lease_interval(), heartbeat_at=func.now())
        )
        await db.commit()
        return result.rowcount == 1


async def still_owner(run_id: uuid.UUID, attempt: int, owner: str = INSTANCE_ID) -> bool:
    async with _session() as db:
        row = (await db.execute(select(Run.id).where(*_owned(run_id, attempt, owner)))).first()
        return row is not None


async def record_job_name(run_id: uuid.UUID, attempt: int, job_name: str,
                          owner: str = INSTANCE_ID) -> bool:
    """Persist the Job name BEFORE creating the Job, so a concurrent cancel
    can always find (and delete) it. Fails if cancelled / not the owner."""
    async with _session() as db:
        result = await db.execute(
            update(Run).where(*_owned(run_id, attempt, owner)).values(k8s_job_name=job_name)
        )
        await db.commit()
        return result.rowcount == 1


async def release_job(run_id: uuid.UUID, attempt: int, release: Callable[[], Awaitable[None]],
                      owner: str = INSTANCE_ID) -> bool:
    """Let the suspended Job start, atomically with re-checking that the
    run is still ours and not cancelled. The row is locked FOR UPDATE while
    `release` runs, so RunService.cancel()'s CAS either commits first (we
    see 'cancelled' and never release) or waits until after the release is
    recorded (and then deletes the started Job itself)."""
    async with _session() as db:
        async with db.begin():
            row = (await db.execute(
                select(Run.id).where(*_owned(run_id, attempt, owner)).with_for_update()
            )).first()
            if row is None:
                return False
            await release()
            await db.execute(update(Run).where(Run.id == run_id).values(job_released_at=func.now()))
    return True


async def requeue(run_id: uuid.UUID, attempt: int, owner: str = INSTANCE_ID) -> bool:
    """Put a run that provably never started executing back to 'queued'."""
    async with _session() as db:
        result = await db.execute(
            update(Run).where(*_owned(run_id, attempt, owner))
            .values(status="queued", lease_owner=None, lease_expires_at=None,
                    heartbeat_at=None, k8s_job_name=None, job_released_at=None)
        )
        await db.commit()
        return result.rowcount == 1


class RunLease:
    """Holds the lease on one claimed run for the duration of its
    execution, renewing it in the background. `lost` is set the moment a
    renewal fails — the run was cancelled, or (after a long stall) another
    replica legitimately took it over — so the executor can stop."""

    def __init__(self, run_id: uuid.UUID, attempt: int, owner: str = INSTANCE_ID):
        self.run_id = run_id
        self.attempt = attempt
        self.owner = owner
        self.lost = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _heartbeat(self) -> None:
        interval = max(get_settings().run_lease_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await renew(self.run_id, self.attempt, self.owner)
            except Exception:  # noqa: BLE001 — a DB blip is not a lost lease yet
                log.warning("run_lease_renew_error", run_id=str(self.run_id), exc_info=True)
                continue
            if not ok:
                log.warning("run_lease_lost", run_id=str(self.run_id), attempt=self.attempt)
                self.lost.set()
                return

    async def __aenter__(self) -> "RunLease":
        self._task = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # Convenience pass-throughs bound to this lease
    async def still_owner(self) -> bool:
        return await still_owner(self.run_id, self.attempt, self.owner)

    async def record_job_name(self, job_name: str) -> bool:
        return await record_job_name(self.run_id, self.attempt, job_name, self.owner)

    async def release_job(self, release: Callable[[], Awaitable[None]]) -> bool:
        return await release_job(self.run_id, self.attempt, release, self.owner)
