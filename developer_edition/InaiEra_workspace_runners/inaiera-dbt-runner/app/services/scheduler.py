"""Cron scheduler (RUN-DBT-004).

A background task per process polls every scheduled, enabled project and
fires a run when its cron expression is due. Helm runs multiple replicas
with autoscaling, so this cannot rely on anything held only in this
process's memory — two replicas will independently decide the same
occurrence is due at close to the same moment. Two things make that safe:

  * A per-project Postgres advisory lock (pg_try_advisory_xact_lock),
    scoped to the transaction that evaluates and (if due) fires that
    project, so only one replica normally even attempts a given
    project's occurrence at a time.
  * ScheduledOccurrence's UNIQUE(project_id, scheduled_fire_time)
    constraint (see app/models/__init__.py) as the actual correctness
    guarantee: even if the lock's hashed key collides across two
    different projects, or two replicas somehow both got the lock in
    different ticks, only one INSERT for the same logical occurrence can
    ever succeed — the loser's IntegrityError is treated as "already
    fired" and it moves on without dispatching a second run.

The "last fired" watermark itself is also no longer process-local: it is
the most recent ScheduledOccurrence row for that project (falling back to
Project.updated_at before any occurrence has ever fired), so every
replica computes the exact same next-fire time from the exact same
inputs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models import Project, ScheduledOccurrence
from app.schemas.project import RunTriggerRequest
from app.services.project_service import ProjectService
from app.services.run_service import RunService

log = structlog.get_logger()


async def scheduler_loop() -> None:
    settings = get_settings()
    log.info("scheduler_starting", poll_interval=settings.scheduler_poll_interval_seconds)
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("scheduler_tick_failed")
        await asyncio.sleep(settings.scheduler_poll_interval_seconds)


async def _tick() -> None:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # list_scheduled() is limited to this data plane's ORG_ID (DBT-004).
        projects = await ProjectService(db).list_scheduled()

    for project in projects:
        try:
            await _maybe_fire(project, now)
        except Exception:
            log.exception("scheduler_project_tick_failed", project_id=str(project.id))


def _compute_next_fire(cron: str, last: datetime) -> datetime | None:
    """Next fire time strictly after `last`, or None if the cron
    expression can't be parsed. Pure — no DB, no clock — so it is the
    same for every replica given the same (cron, last)."""
    try:
        return croniter(cron, last).get_next(datetime)
    except (ValueError, KeyError):
        return None


def _latest_due_fire(cron: str, first_due: datetime, now: datetime, *, max_steps: int = 100_000) -> datetime:
    """Coalesce a backlog of missed occurrences into the most recent one
    that is due. After downtime (or with several replicas) every replica
    therefore picks the SAME logical occurrence — which the UNIQUE
    constraint then dedupes — instead of each firing a different missed
    one, and the scheduler never replays a storm of stale runs."""
    it = croniter(cron, first_due)
    latest = first_due
    for _ in range(max_steps):
        cand = it.get_next(datetime)
        if cand > now:
            break
        latest = cand
    return latest


async def _last_fire_time(db: AsyncSession, project: Project) -> datetime:
    """The watermark every replica computes the next occurrence from. A
    DB read, not a process-local dict — that's the actual fix here: it is
    identical no matter which replica asks."""
    result = await db.execute(
        select(func.max(ScheduledOccurrence.scheduled_fire_time))
        .where(ScheduledOccurrence.project_id == project.id)
    )
    return result.scalar() or project.updated_at


async def _maybe_fire(project: Project, now: datetime) -> None:
    async with AsyncSessionLocal() as db:
        # Advisory lock is transaction-scoped: held for exactly as long as
        # this session's transaction is open, released automatically on
        # commit/rollback below — nothing to remember to unlock. hashtext()
        # keeps this to one bigint argument; a hash collision between two
        # different projects only costs a missed tick for one of them this
        # cycle (the next tick tries again), never a wrong dispatch — that
        # guarantee comes from the UNIQUE constraint below, not this lock.
        got_lock = (await db.execute(
            select(func.pg_try_advisory_xact_lock(func.hashtext(str(project.id))))
        )).scalar()
        if not got_lock:
            log.debug("scheduler_project_locked_elsewhere", project_id=str(project.id))
            return

        last = await _last_fire_time(db, project)
        next_fire = _compute_next_fire(project.schedule_cron, last)
        if next_fire is None:
            log.warning("invalid_cron", project_id=str(project.id), cron=project.schedule_cron)
            return
        if next_fire > now:
            return
        next_fire = _latest_due_fire(project.schedule_cron, next_fire, now)

        occurrence = ScheduledOccurrence(project_id=project.id, scheduled_fire_time=next_fire)
        db.add(occurrence)
        try:
            await db.flush()
        except IntegrityError:
            # Another replica already recorded this exact occurrence — the
            # backstop the advisory lock is meant to make rare, not the
            # only thing standing between here and a duplicate run.
            await db.rollback()
            log.info("scheduler_occurrence_already_recorded", project_id=str(project.id),
                      scheduled_fire_time=next_fire.isoformat())
            return

        log.info("scheduler_triggering", project_id=str(project.id), name=project.name,
                  scheduled_fire_time=next_fire.isoformat())
        run = await RunService(db).submit(
            project=project, trigger="scheduled", triggered_by_user_id=None,
            request=RunTriggerRequest(select=None),
        )
        occurrence.run_id = run.id
        await db.commit()
