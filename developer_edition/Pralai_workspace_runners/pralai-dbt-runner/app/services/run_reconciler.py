"""Durable run controller (RUN-DBT-003).

RunService.submit() only PERSISTS a queued run (and announces it with a NOTIFY delivered at commit); it never starts execution
in the request's process (DBT-011). This loop is the single owner of dispatch: it runs on the elected leader only (app/services/leader.py — once
across replicas AND uvicorn workers) and drives every non-terminal run
forward, including ones this process never dispatched.

Three sweeps per tick:

  * cancelling runs (DBT-005) — the user asked to cancel a run whose Kubernetes
    Job exists: keep requesting its deletion until the cluster confirms it is
    gone, only then mark the run 'cancelled'; alert if that never happens.
    See app/services/run_cancel.py.

  * queued runs — handed to dispatch_run() as BACKGROUND tasks, bounded by
    run_dispatch_concurrency per process; the sweep never waits for a run
    to finish (a slow clone or a long build cannot stall the loop).
    dispatch_run()'s guarded claim makes duplicates a no-op.

  * running runs — ONLY those whose lease has EXPIRED (the owner stopped
    heartbeating). A run whose owner is alive — e.g. still cloning, with no
    Job created yet — is never touched; that was the race where another
    replica saw "no Job (404)" and redispatched a run that was about to
    start. For an expired lease the reconciler first takes the lease over
    (CAS), then:
      - subprocess mode: the owning process is gone and so is its build;
        fail the run as orphaned.
      - k8s_job, no Job recorded: nothing was created -> requeue.
      - k8s_job, Job recorded: adopt it (in the background). A suspended
        Job never ran -> withdraw + requeue. An active/terminal Job is
        monitored and finalized. A MISSING Job (e.g. TTL-GC'd) is never
        re-run: its recorded result in the artifact store is used, or — if
        it was released and no result was recorded — the run fails with an
        explicit reason.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import structlog
from sqlalchemy import func, or_, select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.executors.isolation import get_runner_isolation
from app.models import Project, Run
from app.services import run_cancel, run_lease
from app.services.run_service import _complete_attempt, _finalize_run, dispatch_run

log = structlog.get_logger()

# run_id -> task, for work this process has in flight (dispatches started by
# the queued sweep and Job adoptions). Strong refs also stop GC of tasks.
_IN_FLIGHT: dict[uuid.UUID, asyncio.Task] = {}
_dispatch_semaphore: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _dispatch_semaphore
    if _dispatch_semaphore is None:
        _dispatch_semaphore = asyncio.Semaphore(get_settings().run_dispatch_concurrency)
    return _dispatch_semaphore


def _spawn(run_id: uuid.UUID, coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _IN_FLIGHT[run_id] = task
    task.add_done_callback(lambda t, rid=run_id: _IN_FLIGHT.pop(rid, None) if _IN_FLIGHT.get(rid) is t else None)
    return task


_wake: asyncio.Event | None = None


def _wake_event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


async def _listen_for_queued_runs() -> None:
    """Hold a LISTEN on the queued-run channel and wake the loop when submit() announces a run (delivered at ITS commit, so the row is
    visible when we look). Best effort: on any failure it retries, and the loop's own short poll covers the gap."""
    from sqlalchemy.engine import make_url

    from app.db.session import engine
    from app.services.run_service import RUN_QUEUED_CHANNEL

    while True:
        conn = None
        try:
            import asyncpg  # type: ignore[import-untyped]
            dsn = make_url(engine.url.render_as_string(hide_password=False)).set(drivername="postgresql").render_as_string(hide_password=False)
            conn = await asyncpg.connect(dsn)
            await conn.add_listener(RUN_QUEUED_CHANNEL, lambda *_: _wake_event().set())
            log.info("run_queue_listener_started")
            while not conn.is_closed():
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001 — the poll is the safety net
            log.warning("run_queue_listener_failed_retrying")
        finally:
            if conn is not None and not conn.is_closed():
                await conn.close()
        await asyncio.sleep(5)


async def reconciler_loop() -> None:
    """The ONE owner of dispatch (DBT-011): the full reconcile every `run_reconciler_poll_interval_seconds`, and in between the cheap
    queued-run sweep whenever a run is announced (or every `run_queue_poll_seconds` as a safety net)."""
    settings = get_settings()
    log.info("run_reconciler_starting", poll_interval=settings.run_reconciler_poll_interval_seconds,
             queue_poll=settings.run_queue_poll_seconds)
    listener = asyncio.create_task(_listen_for_queued_runs())
    last_full = 0.0
    try:
        while True:
            try:
                now = time.monotonic()
                if now - last_full >= settings.run_reconciler_poll_interval_seconds:
                    last_full = now
                    await reconcile_once()
                else:
                    await _reconcile_queued()
            except Exception:
                log.exception("run_reconciler_tick_failed")
            try:
                await asyncio.wait_for(_wake_event().wait(), timeout=settings.run_queue_poll_seconds)
            except TimeoutError:
                pass
            _wake_event().clear()
    finally:
        listener.cancel()
        # Leadership lost / shutdown: stop adopted monitors. Their leases
        # expire and the next leader takes over.
        for task in list(_IN_FLIGHT.values()):
            task.cancel()


async def reconcile_once() -> None:
    await _reconcile_cancelling()
    await _reconcile_queued()
    await _reconcile_running()


async def _reconcile_cancelling() -> None:
    """DBT-005: keep deleting the Job of every run being cancelled until the cluster confirms it is gone, then
    mark the run 'cancelled'; alert on one that will not complete. Never raises (the other sweeps still run)."""
    try:
        await run_cancel.sweep(get_runner_isolation())
    except Exception:
        log.exception("run_reconciler_cancelling_sweep_failed")


async def _bounded_dispatch(run_id: uuid.UUID) -> None:
    async with _semaphore():
        await dispatch_run(run_id)


async def _reconcile_queued() -> list[asyncio.Task]:
    """Start (do not await) a dispatch for each queued run not already in
    flight here, up to the free dispatch capacity."""
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Run.id).join(Project, Project.id == Run.project_id)
            .where(Run.status == "queued", Project.org_id == settings.org_id)       # DBT-004: this data plane's org only
            .order_by(Run.created_at)
            .limit(settings.run_dispatch_concurrency * 4)
        )
        run_ids = [row[0] for row in result]

    started: list[asyncio.Task] = []
    for run_id in run_ids:
        if run_id in _IN_FLIGHT:
            continue
        started.append(_spawn(run_id, _bounded_dispatch(run_id)))
    return started


async def _reconcile_running() -> list[asyncio.Task]:
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Run.id).join(Project, Project.id == Run.project_id).where(
                Run.status == "running",
                Project.org_id == settings.org_id,                                   # DBT-004
                or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < func.now()),
            )
        )
        run_ids = [row[0] for row in result]

    started: list[asyncio.Task] = []
    for run_id in run_ids:
        if run_id in _IN_FLIGHT:
            continue
        taken = await run_lease.take_over_expired(run_id)
        if taken is None:
            continue  # someone renewed or took it first

        if settings.runner_mode != "k8s_job":
            message = ("orphaned: the process running this build (RUNNER_MODE=subprocess) stopped "
                       "heartbeating and its in-process build cannot be reattached (local dev only)")
            await _finalize_run(run_id, taken.project_id, None,
                                {"success": False, "errors": [message], "nodes": []},
                                attempt=taken.attempt, owner=run_lease.INSTANCE_ID)
            continue

        if not taken.k8s_job_name:
            # The owner died before recording (hence before creating) a Job.
            if await run_lease.requeue(run_id, taken.attempt):
                log.warning("run_reconciler_requeued_no_job", run_id=str(run_id), attempt=taken.attempt)
                started.append(_spawn(run_id, _bounded_dispatch(run_id)))
            continue

        started.append(_spawn(run_id, _adopt(taken)))
    return started


async def _adopt(taken: run_lease.TakenOver) -> None:
    isolation = get_runner_isolation()
    async with run_lease.RunLease(taken.run_id, taken.attempt) as lease:
        try:
            result = await isolation.adopt(lease=lease, run_id=taken.run_id,
                                           job_name=taken.k8s_job_name,
                                           released=taken.job_released_at is not None)
        except Exception as e:  # noqa: BLE001
            log.exception("run_reconciler_adopt_failed", run_id=str(taken.run_id))
            return  # lease will expire; retried on a later tick
        log.info("run_reconciler_adopted", run_id=str(taken.run_id), attempt=taken.attempt,
                 success=result.get("success"), never_started=bool(result.get("_never_started")))
        await _complete_attempt(taken.run_id, taken.project_id, taken.attempt, None, result,
                                owner=lease.owner)
    if result.get("_never_started"):
        await _bounded_dispatch(taken.run_id)
