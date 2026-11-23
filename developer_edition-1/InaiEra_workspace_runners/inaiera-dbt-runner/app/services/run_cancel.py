"""Cancellation as a desired-state transition (DBT-005).

Cancelling a running run used to flip it to 'cancelled' and only afterwards make a best-effort attempt to delete its
Kubernetes Job; if the deletion failed it was just logged, so a run could read 'cancelled' while its Job kept
executing dbt against the warehouse (Kubernetes' TTL-after-finished does not touch a running Job).

Now:

    queued   -> cancelled                 nothing can be running
    running  -> cancelled                 no Job was ever recorded (or subprocess/dev mode: no Job exists), so
                                          nothing is running that we could be failing to stop
    running  -> cancelling -> cancelled   a Job is recorded: 'cancelling' means "asked to cancel"; it becomes
                                          'cancelled' only once the cluster CONFIRMS the Job is gone or already
                                          finished (see RunnerIsolation.terminate_job)

advance() drives ONE cancelling run toward 'cancelled'; it is called once inline by RunService.cancel for an
immediate answer and then by the reconciler's sweep every tick, which keeps retrying the deletion (an API error is
retried, never swallowed into 'cancelled'). Both are idempotent and the final step is a compare-and-set, so any
number of them racing complete the run exactly once. A cancellation that is still not confirmed after
run_cancel_stuck_seconds is an alert: an error log, the `run_cancel_stuck` counter, and /readyz reports
run_cancellation_stuck.

The run's previous owner can no longer touch it: its lease is cleared when it is asked to cancel, and every guarded
step (renew, record the Job name, release the Job, finalize) requires status='running'.
"""
from __future__ import annotations

import time
import uuid

import structlog
from sqlalchemy import func, select, update

from app.core import metrics
from app.core.config import get_settings
from app.executors.isolation import CONFIRMED_JOB_STATES, JOB_FOREIGN
from app.models import Project, Run

log = structlog.get_logger()

# Alert again for a still-stuck run at most this often (the sweep runs every ~15 s).
STUCK_REALERT_SECONDS = 900.0
_STUCK_LAST_LOGGED: dict[uuid.UUID, float] = {}


def _session():
    from app.db import session as db_session  # resolved at call time (tests patch it)
    return db_session.AsyncSessionLocal()


async def advance(run_id: uuid.UUID, isolation) -> str:
    """Move one 'cancelling' run toward 'cancelled'. Returns the run's status afterwards."""
    async with _session() as db:
        row = (await db.execute(
            select(Run.status, Run.attempt, Run.k8s_job_name).where(Run.id == run_id)
        )).one_or_none()
    if row is None:
        return "gone"
    status, attempt, job_name = row
    if status != "cancelling":
        return status

    if job_name:
        try:
            state = await isolation.terminate_job(run_id=run_id, attempt=attempt, job_name=job_name)
        except Exception:  # retried by the reconciler; NEVER reported as cancelled
            log.warning("run_cancel_job_termination_failed", run_id=str(run_id), job_name=job_name,
                        exc_info=True)
            metrics.inc("run_cancel_retry")
            return "cancelling"
        if state == JOB_FOREIGN:
            log.error("run_cancel_job_name_not_ours", run_id=str(run_id), job_name=job_name,
                      note="the name belongs to a resource this attempt did not create; left untouched")
        if state not in CONFIRMED_JOB_STATES:
            return "cancelling"
    return await _mark_cancelled(run_id)


async def _mark_cancelled(run_id: uuid.UUID) -> str:
    async with _session() as db:
        result = await db.execute(
            update(Run).where(Run.id == run_id, Run.status == "cancelling")
            # A run stopped because a project it is a Dependent of failed ends as 'failed' (app/services/dependents.py).
            .values(status=func.coalesce(Run.cancel_outcome, "cancelled"), completed_at=func.now(),
                    lease_owner=None, lease_expires_at=None)
            .returning(Run.started_at, Run.completed_at, Run.status)
        )
        row = result.one_or_none()
        if row is None:                      # someone else completed it first: that is fine
            await db.rollback()
            return "cancelled"
        started, completed, final = row
        if started and completed:
            await db.execute(update(Run).where(Run.id == run_id)
                             .values(duration_ms=int((completed - started).total_seconds() * 1000)))
        await db.commit()
    _STUCK_LAST_LOGGED.pop(run_id, None)
    log.info("run_cancelled", run_id=str(run_id), status=final)
    return final


def _alert_if_stuck(run_id: uuid.UUID, age_seconds: float, job_name: str | None) -> None:
    if age_seconds < get_settings().run_cancel_stuck_seconds:
        return
    now = time.monotonic()
    last = _STUCK_LAST_LOGGED.get(run_id)
    if last is not None and now - last < STUCK_REALERT_SECONDS:
        return
    _STUCK_LAST_LOGGED[run_id] = now
    metrics.inc("run_cancel_stuck")
    log.error("run_cancel_stuck", run_id=str(run_id), job_name=job_name, age_seconds=int(age_seconds),
              note="cancellation requested but the Kubernetes Job is not confirmed gone; the Job may still be "
                   "running dbt against the warehouse. Check the cluster API and the Job.")


async def sweep(isolation, *, limit: int = 100) -> None:
    """One reconciler pass over every run that is 'cancelling', oldest request first. One run failing never
    stops the others."""
    async with _session() as db:
        rows = (await db.execute(
            select(Run.id, Run.k8s_job_name,
                   func.extract("epoch", func.now() - Run.cancel_requested_at))
            .join(Project, Project.id == Run.project_id)
            .where(Run.status == "cancelling", Project.org_id == get_settings().org_id)    # DBT-004
            .order_by(Run.cancel_requested_at).limit(limit)
        )).all()
    for run_id, job_name, age in rows:
        try:
            status = await advance(run_id, isolation)
        except Exception:
            log.exception("run_cancel_advance_failed", run_id=str(run_id))
            continue
        if status == "cancelling":
            _alert_if_stuck(run_id, float(age or 0), job_name)
