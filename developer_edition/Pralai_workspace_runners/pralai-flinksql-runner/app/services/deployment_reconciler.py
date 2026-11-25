"""Durable deploy/stop controller (RUN-FLINK-013/014).

Runs on exactly ONE replica: the loop holds a Postgres session-level
advisory lock (app.services.coordination.LeaderElector) for as long as it
is leader; other replicas keep trying to acquire it each interval.

Each sweep only touches a deployment after re-claiming its lease with a
compare-and-set (an expired lease = its owner died); the original owner,
while alive, keeps heartbeating, so a resume can never race it.

  'deploying' — never resubmits something Flink may already be running:
    * flink_job_id recorded  -> live state alive: re-attach (mark running);
                                UNKNOWN: wait; terminal / gone: resubmit.
    * submission started but no job id (crash after the Gateway accepted
      the statement) -> look the job up by its unique submission_tag
      (pipeline.name): alive -> re-attach with that id; JobManager
      unreachable -> wait; absent -> if the Gateway operation is still
      running, wait; otherwise resubmit.
    * never reached the Gateway -> resubmit.

  'pausing' — never marks a job paused on a transient failure:
    * live state UNKNOWN (JobManager unreachable) -> wait;
    * still running -> savepoint in progress: wait; otherwise retry the
      stop-with-savepoint from the persisted desired state;
    * terminal / gone -> paused only if the recorded stop trigger's
      savepoint COMPLETED; otherwise cancelled/failed with the reason.

  FL-011: this loop is the ONLY thing that starts a deploy or a stop. deploy()/stop() persist the desired state (job
  'deploying'/'pausing' + an unowned deployment row) and send a NOTIFY at commit; nothing runs in the API request's process.

  orphan 'submitting' rows whose job is no longer deploying (they would
  block every future deploy through the one-active-generation index) are
  marked failed once their lease has expired.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import structlog
from sqlalchemy import func, or_, select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.executors.flink_gateway import get_flink_client
from app.models import FlinkDeployment, FlinkJob
from app.services.coordination import LeaderElector, LeaseHeartbeat, claim_lease
from app.services.job_service import (
    _ACTIVE_DEPLOYMENT_IDS,
    NOT_FOUND,
    UNKNOWN,
    _execute_deploy,
    _execute_stop,
    _finalize_running,
    is_alive_state,
    locate_submitted_job,
    resolve_pausing,
)

log = structlog.get_logger()

LEADER_LOCK_NAME = "flinksql_deployment_reconciler"
_elector: LeaderElector | None = None


def get_elector() -> LeaderElector:
    global _elector
    if _elector is None:
        _elector = LeaderElector(LEADER_LOCK_NAME)
    return _elector


_wake: asyncio.Event | None = None


def _wake_event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


async def _listen_for_work() -> None:
    """LISTEN for 'a deploy/stop was persisted' (FL-011) and wake the loop. The NOTIFY is delivered at the creating request's COMMIT,
    so the rows are visible when the loop looks. Best effort: on failure it retries; the loop's own short poll covers the gap."""
    from sqlalchemy.engine import make_url

    from app.db import session as db_session
    from app.services.job_service import WORK_CHANNEL

    while True:
        conn = None
        try:
            import asyncpg  # type: ignore[import-untyped]
            url = make_url(db_session.engine.url.render_as_string(hide_password=False)).set(drivername="postgresql")
            conn = await asyncpg.connect(url.render_as_string(hide_password=False))
            await conn.add_listener(WORK_CHANNEL, lambda *_: _wake_event().set())
            log.info("deployment_work_listener_started")
            while not conn.is_closed():
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001 — the poll is the safety net
            log.warning("deployment_work_listener_failed_retrying")
        finally:
            if conn is not None and not conn.is_closed():
                await conn.close()
        await asyncio.sleep(5)


async def reconciler_loop() -> None:
    """The ONE owner of deploy/stop execution (FL-011). Leader-only. The full sweep (incl. running-job checks) runs every
    `deployment_reconciler_poll_interval_seconds`; in between, a cheap sweep of 'deploying'/'pausing' jobs runs as soon as a
    deploy/stop is announced, or every `deployment_dispatch_poll_seconds` as the safety net. Deploys/stops are started as bounded
    background tasks so a slow Gateway never delays the sweep (or another job's deploy)."""
    settings = get_settings()
    elector = get_elector()
    log.info("deployment_reconciler_starting",
             poll_interval=settings.deployment_reconciler_poll_interval_seconds,
             dispatch_poll=settings.deployment_dispatch_poll_seconds)
    listener = asyncio.create_task(_listen_for_work())
    last_full = 0.0
    try:
        while True:
            try:
                if await elector.ensure():
                    now = time.monotonic()
                    if now - last_full >= settings.deployment_reconciler_poll_interval_seconds:
                        last_full = now
                        await reconcile_once(wait=False)
                    else:
                        await _reconcile_work(wait=False)
            except Exception:
                log.exception("deployment_reconciler_tick_failed")
            try:
                await asyncio.wait_for(_wake_event().wait(), timeout=settings.deployment_dispatch_poll_seconds)
            except asyncio.TimeoutError:        # noqa: UP041 — the runner image is Python 3.10, where this is NOT the builtin TimeoutError
                pass
            _wake_event().clear()
    finally:
        listener.cancel()
        await elector.release()


async def tick_if_leader(elector: LeaderElector) -> bool:
    """One full sweep iff this replica holds the lifetime leader lock (waits for the work it starts)."""
    if not await elector.ensure():
        return False
    await reconcile_once()
    return True


_IN_FLIGHT: dict[uuid.UUID, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None


def _bounded() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().deployment_dispatch_concurrency)
    return _semaphore


def _spawn(deployment_id: uuid.UUID, coro_fn) -> None:
    """Start `coro_fn(deployment_id)` in the background unless one is already running for it here."""
    running = _IN_FLIGHT.get(deployment_id)
    if running is not None and not running.done():
        return

    async def _run():
        async with _bounded():
            await coro_fn(deployment_id)

    task = asyncio.create_task(_run())
    _IN_FLIGHT[deployment_id] = task

    def _forget(t: asyncio.Task) -> None:
        if _IN_FLIGHT.get(deployment_id) is t:
            _IN_FLIGHT.pop(deployment_id, None)

    task.add_done_callback(_forget)


async def _drain_in_flight() -> None:
    await asyncio.gather(*list(_IN_FLIGHT.values()), return_exceptions=True)


async def reconcile_once(*, wait: bool = True) -> None:
    """One full sweep (callers are responsible for leadership). With wait=False deploys/stops are started in the background."""
    await _reconcile_work(wait=wait)
    await _reconcile_running()
    await _reconcile_orphan_submitting()


async def _reconcile_work(*, wait: bool = True) -> None:
    await _reconcile_deploying(wait=wait)
    await _reconcile_pausing(wait=wait)


async def _latest_deployment_id(job_id: uuid.UUID) -> uuid.UUID | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(FlinkDeployment.id).where(FlinkDeployment.job_id == job_id)
            .order_by(FlinkDeployment.created_at.desc()).limit(1)
        )).scalar_one_or_none()


async def _job_ids_in(status: str) -> list[uuid.UUID]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(FlinkJob.id).where(FlinkJob.status == status)
        )).scalars().all())


async def _safely(coro_fn, event: str, job_id: uuid.UUID):
    async def _wrapped(deployment_id: uuid.UUID) -> None:
        try:
            await coro_fn(deployment_id)
        except Exception:
            log.exception(event, job_id=str(job_id))
    return _wrapped


async def _reconcile_deploying(*, wait: bool = True) -> None:
    for job_id in await _job_ids_in("deploying"):
        deployment_id = await _latest_deployment_id(job_id)
        if deployment_id is None or deployment_id in _ACTIVE_DEPLOYMENT_IDS:
            continue
        _spawn(deployment_id, await _safely(_resume_deploy, "deployment_reconciler_deploy_resume_failed", job_id))
    if wait:
        await _drain_in_flight()


async def _resume_deploy(deployment_id: uuid.UUID) -> None:
    client = get_flink_client()
    _ACTIVE_DEPLOYMENT_IDS.add(deployment_id)
    try:
        async with AsyncSessionLocal() as db:
            # RUN-FLINK-014: re-claim the row before acting on it.
            if not await claim_lease(db, deployment_id):
                return
            async with LeaseHeartbeat(deployment_id):
                dep = await db.get(FlinkDeployment, deployment_id)
                job = await db.get(FlinkJob, dep.job_id) if dep else None
                if dep is None or job is None or job.status != "deploying":
                    return

                if dep.status == "running" and dep.flink_job_id:
                    job.status = "running"   # half-finished finalize
                    await db.commit()
                    return
                if dep.status != "submitting":
                    return

                if dep.flink_job_id:
                    state = await client.job_state(dep.flink_job_id)
                    if state == UNKNOWN:
                        return
                    if is_alive_state(state):
                        await _finalize_running(db, deployment_id, dep.flink_job_id)
                        log.info("deployment_reconciler_reattached_running",
                                 job_id=str(job.id), flink_job_id=dep.flink_job_id)
                        return
                elif dep.submit_started_at is not None:
                    outcome, located, _ = await locate_submitted_job(client, dep.submission_tag)
                    if outcome == "unknown":
                        return
                    if outcome == "alive":
                        await _finalize_running(db, deployment_id, located)
                        log.info("deployment_reconciler_reattached_by_tag",
                                 job_id=str(job.id), flink_job_id=located, tag=dep.submission_tag)
                        return
                    if outcome == "absent" and dep.gateway_session_id and dep.gateway_operation_handle:
                        try:
                            op_state = await client.get_operation_status(
                                dep.gateway_session_id, dep.gateway_operation_handle)
                        except Exception:  # noqa: BLE001
                            return  # Gateway unreachable — can't rule out an in-flight submit
                        if op_state in ("PENDING", "RUNNING", "INITIALIZED"):
                            return  # still being submitted

                log.warning("deployment_reconciler_resuming_deploy",
                            job_id=str(job.id), deployment_id=str(deployment_id))
                await db.commit()  # end this read transaction before delegating
                _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)
                await _execute_deploy(deployment_id, lease_held=True)
    finally:
        _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)


async def _reconcile_pausing(*, wait: bool = True) -> None:
    for job_id in await _job_ids_in("pausing"):
        deployment_id = await _latest_deployment_id(job_id)
        if deployment_id is None or deployment_id in _ACTIVE_DEPLOYMENT_IDS:
            continue
        _spawn(deployment_id, await _safely(_resume_stop, "deployment_reconciler_stop_resume_failed", job_id))
    if wait:
        await _drain_in_flight()


async def _resume_stop(deployment_id: uuid.UUID) -> None:
    client = get_flink_client()
    _ACTIVE_DEPLOYMENT_IDS.add(deployment_id)
    try:
        async with AsyncSessionLocal() as db:
            if not await claim_lease(db, deployment_id):
                return
            async with LeaseHeartbeat(deployment_id):
                dep = await db.get(FlinkDeployment, deployment_id)
                job = await db.get(FlinkJob, dep.job_id) if dep else None
                if dep is None or job is None or job.status != "pausing":
                    return
                if not dep.flink_job_id:
                    await _fail_unresumable_stop(db, job, dep, "no Flink job ID recorded to check or stop")
                    return

                state = await client.job_state(dep.flink_job_id)
                if state == UNKNOWN:
                    log.info("deployment_reconciler_pausing_flink_unknown", job_id=str(job.id))
                    return
                if is_alive_state(state):
                    if dep.stop_trigger_id:
                        sp = await client.get_savepoint_status(dep.flink_job_id, dep.stop_trigger_id)
                        if sp["status"] in ("IN_PROGRESS", "UNKNOWN", "COMPLETED"):
                            return  # stop still in flight (job finishes after the savepoint)
                    log.warning("deployment_reconciler_resuming_stop",
                                job_id=str(job.id), deployment_id=str(deployment_id))
                    await db.commit()  # end this read transaction before delegating
                    _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)
                    await _execute_stop(deployment_id, lease_held=True)
                    return
                action = await resolve_pausing(db, deployment_id, state)
                log.warning("deployment_reconciler_stop_resolved", job_id=str(job.id),
                            flink_state=state, action=action)
    finally:
        _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)


# Flink states that end a "running" deployment, mapped to (deployment, job) status.
_RUNNING_ENDED = {
    "FAILED": ("failed", "failed"),
    "CANCELED": ("cancelled", "cancelled"),
    "FINISHED": ("completed", "completed"),
}


async def _reconcile_running() -> None:
    """A deployment recorded as running whose Flink job has ended or vanished.

    The common case is a Flink cluster restart: the JobManager no longer knows
    the job (404 -> NOT_FOUND, which is definitive, unlike UNKNOWN), yet the
    row stayed "running" forever and the metrics poller failed on it every
    tick. UNKNOWN (JobManager unreachable) is never treated as an ending."""
    for job_id in await _job_ids_in("running"):
        deployment_id = await _latest_deployment_id(job_id)
        if deployment_id is None or deployment_id in _ACTIVE_DEPLOYMENT_IDS:
            continue
        try:
            await _check_running(deployment_id)
        except Exception:
            log.exception("deployment_reconciler_running_check_failed", job_id=str(job_id))


async def _check_running(deployment_id: uuid.UUID) -> None:
    client = get_flink_client()
    _ACTIVE_DEPLOYMENT_IDS.add(deployment_id)
    try:
        async with AsyncSessionLocal() as db:
            if not await claim_lease(db, deployment_id):
                return
            async with LeaseHeartbeat(deployment_id):
                dep = await db.get(FlinkDeployment, deployment_id)
                job = await db.get(FlinkJob, dep.job_id) if dep else None
                if dep is None or job is None or job.status != "running" \
                        or dep.status != "running" or not dep.flink_job_id:
                    return
                state = await client.job_state(dep.flink_job_id)
                if state == UNKNOWN or is_alive_state(state):
                    return
                if state == NOT_FOUND:
                    dep_status, job_status = "failed", "failed"
                    reason = (f"Flink job {dep.flink_job_id} no longer exists on the cluster "
                              "(e.g. lost when the Flink cluster restarted). Redeploy to run it again.")
                elif state in _RUNNING_ENDED:
                    dep_status, job_status = _RUNNING_ENDED[state]
                    reason = f"Flink reports the job as {state}."
                else:
                    return
                dep.status = dep_status
                dep.error_message = reason if dep_status == "failed" else dep.error_message
                dep.stopped_at = func.now()
                job.status = job_status
                await db.commit()
                log.warning("deployment_reconciler_running_job_ended", job_id=str(job.id),
                            flink_job_id=dep.flink_job_id, flink_state=state,
                            deployment_status=dep_status)
    finally:
        _ACTIVE_DEPLOYMENT_IDS.discard(deployment_id)


async def _fail_unresumable_stop(db, job: FlinkJob, dep: FlinkDeployment, reason: str) -> None:
    dep.status = "failed"
    dep.error_message = f"orphaned by a process restart: {reason}"
    job.status = "failed"
    await db.commit()
    log.error("deployment_reconciler_stop_unresumable", job_id=str(job.id), reason=reason)


async def _reconcile_orphan_submitting() -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(FlinkDeployment)
            .join(FlinkJob, FlinkJob.id == FlinkDeployment.job_id)
            .where(FlinkDeployment.status == "submitting",
                   FlinkJob.status != "deploying",
                   or_(FlinkDeployment.lease_expires_at.is_(None),
                       FlinkDeployment.lease_expires_at < func.now()))
            .with_for_update(of=FlinkDeployment, skip_locked=True)
        )).scalars().all()
        for dep in rows:
            if dep.id in _ACTIVE_DEPLOYMENT_IDS:
                continue
            dep.status = "failed"
            dep.error_message = ((dep.error_message + " | ") if dep.error_message else "") + \
                "orphaned 'submitting' row (job no longer deploying)"
            log.warning("deployment_reconciler_orphan_submitting_failed", deployment_id=str(dep.id))
        await db.commit()
