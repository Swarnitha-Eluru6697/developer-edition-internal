"""The scheduled maintenance control loop — deployable infrastructure,
not an optional API call.

    python -m app.maintenance_scheduler

Runs as its own process (see the `maintenance` service in
docker-compose.yml). The reported gap was that retention and
reconciliation existed only as functions someone had to remember to
invoke: nothing in the shipped deployment ran them, so expired uploads,
stale HITL claims, completed outbox rows and idempotency keys
accumulated indefinitely.

Three properties make this a control loop rather than a cron line:

  * Every run is RECORDED (MaintenanceRun). "Did retention run?" has an
    answer, which is the prerequisite for noticing that it stopped.
  * A missed run RAISES AN ALERT. A loop that dies quietly is
    indistinguishable from one with nothing to do, and the failure
    surfaces as a full disk weeks later instead. Overdue is judged
    against each job's own interval, so a job that runs every 5 minutes
    and one that runs daily are not held to the same standard.
  * It catches up rather than skipping. Due is computed from the last
    successful finish, so a process restart does not silently drop the
    window it was down for.

A separate process, not a thread inside the API: a maintenance job that
hangs must not take request handling with it, and a deployment can scale
the API to N replicas while keeping exactly one of these.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlmodel import Session, select

from .db import engine
from .maintenance_jobs import Job, jobs
from .models import MaintenanceRun

logger = logging.getLogger("maintenance")

# How far past its interval a job may drift before it counts as missed.
# 2.5x rather than 1x: a job that takes a while, or a loop that was
# briefly busy elsewhere, is late — not broken — and an alert that fires
# on ordinary jitter is an alert people learn to ignore.
OVERDUE_FACTOR = float(os.getenv("MAINT_OVERDUE_FACTOR", "2.5"))

# The loop's own tick. Jobs are due on their own intervals; this only
# bounds how precisely those are honoured.
TICK_SECONDS = max(5, int(os.getenv("MAINT_TICK_SECONDS", "30")))

# The record of runs is itself something that accumulates.
RUN_RECORD_RETENTION_DAYS = int(os.getenv("MAINT_RUN_RECORD_RETENTION_DAYS", "30"))

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    logger.info("received signal %s — finishing the current job then stopping", signum)
    _stop = True


def last_success(session: Session, job_name: str) -> Optional[datetime]:
    row = session.exec(
        select(MaintenanceRun)
        .where(MaintenanceRun.job == job_name, MaintenanceRun.status == "ok")
        .order_by(MaintenanceRun.finished_at.desc())
    ).first()
    return row.finished_at if row else None


def is_due(session: Session, job: Job, now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    previous = last_success(session, job.name)
    if previous is None:
        return True  # never run: run it now rather than waiting a full interval
    return (now - previous) >= timedelta(seconds=job.interval_seconds)


def overdue_by(session: Session, job: Job, now: Optional[datetime] = None) -> Optional[timedelta]:
    """How far past its allowed window a job is, or None if it is fine.

    A job that has NEVER succeeded is only overdue once more than its
    allowed window has passed since this table's first row — otherwise a
    fresh deployment alerts about a daily job in its first minute."""
    now = now or datetime.utcnow()
    allowed = timedelta(seconds=job.interval_seconds * OVERDUE_FACTOR)
    previous = last_success(session, job.name)
    if previous is None:
        first = session.exec(select(MaintenanceRun).order_by(MaintenanceRun.started_at)).first()
        if first is None:
            return None  # nothing has ever run; the loop may have just started
        previous = first.started_at
    late = now - previous
    return late - allowed if late > allowed else None


def _publish_freshness(session: Session, job: Job, now: Optional[datetime] = None) -> None:
    """Expose this job's freshness as metrics, so a missed run becomes an
    alert in whatever the operator already runs.

    Not a row in the Alert table: Alert.agent_id is NOT NULL with a
    foreign key to agent.id, so that table is per-agent by construction
    and a platform-level alert cannot go in it without inventing a fake
    agent. Attempting it raised a NotNullViolation, which is how this was
    caught — the alerting path would have crashed the first time a job
    was genuinely overdue, i.e. exactly when it was needed.
    """
    from .metrics import MAINTENANCE_JOB_OVERDUE, MAINTENANCE_LAST_SUCCESS_AGE_SECONDS

    now = now or datetime.utcnow()
    late = overdue_by(session, job, now)
    MAINTENANCE_JOB_OVERDUE.labels(job=job.name).set(1 if late else 0)
    previous = last_success(session, job.name)
    if previous:
        MAINTENANCE_LAST_SUCCESS_AGE_SECONDS.labels(job=job.name).set(
            (now - previous).total_seconds())


def _report_missed(session: Session, job: Job, late: timedelta) -> None:
    """A missed run, logged at WARNING and reflected in the gauges.

    Logged every tick on purpose: a loop that has been failing for a week
    should keep saying so, because the one thing worse than a noisy alert
    is a silent one. The gauge is the machine-readable half;
    GET /internal/maintenance/status is the human-readable one."""
    _publish_freshness(session, job)
    logger.warning(
        "MAINTENANCE JOB MISSED: '%s' has not succeeded for %ds beyond its allowed window "
        "(interval %ds, tolerance %.1fx). %s. Alert on "
        "inaiera_maintenance_job_overdue{job=\"%s\"} == 1.",
        job.name, int(late.total_seconds()), job.interval_seconds, OVERDUE_FACTOR,
        job.description, job.name,
    )


def run_job(session: Session, job: Job) -> MaintenanceRun:
    """Run one job, recording the attempt whether it succeeds or not.

    The record is written BEFORE the work starts, so a job that hangs or
    a process that is killed mid-run leaves evidence at "running"
    instead of no evidence at all."""
    record = MaintenanceRun(job=job.name, started_at=datetime.utcnow(), status="running")
    session.add(record)
    session.commit()
    session.refresh(record)

    began = time.monotonic()
    try:
        detail = job.run(session) or {}
        record.status = "ok"
        record.detail = detail
        logger.info("%s: %s", job.name, detail)
    except Exception as e:
        # A failing job must not stop the loop: the other jobs still
        # need to run, and the failure is recorded and alerted on.
        session.rollback()
        record = session.get(MaintenanceRun, record.id) or record
        record.status = "error"
        record.error = str(e)[:1000]
        logger.exception("maintenance job '%s' failed", job.name)
    finally:
        record.finished_at = datetime.utcnow()
        record.duration_ms = int((time.monotonic() - began) * 1000)
        session.add(record)
        session.commit()
        from .metrics import MAINTENANCE_JOB_RUNS_TOTAL
        MAINTENANCE_JOB_RUNS_TOTAL.labels(job=job.name, status=record.status).inc()
        _publish_freshness(session, job)
    return record


def prune_own_records(session: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(days=RUN_RECORD_RETENTION_DAYS)
    rows = session.exec(select(MaintenanceRun).where(MaintenanceRun.started_at < cutoff)).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def tick(session: Session) -> Dict[str, str]:
    """One pass over every job. Returns what it did, for the tests and
    for a single-shot `--once` invocation."""
    outcome: Dict[str, str] = {}
    for job in jobs():
        if is_due(session, job):
            record = run_job(session, job)
            outcome[job.name] = record.status
            continue
        late = overdue_by(session, job)
        if late is not None:
            _report_missed(session, job, late)
            outcome[job.name] = "overdue"
        else:
            _publish_freshness(session, job)
            outcome[job.name] = "not-due"
    return outcome


def main(argv=None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    argv = sys.argv[1:] if argv is None else argv
    once = "--once" in argv

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("maintenance control loop starting — tick %ss, jobs: %s",
                TICK_SECONDS, ", ".join(f"{j.name}@{j.interval_seconds}s" for j in jobs()))

    if once:
        with Session(engine) as session:
            result = tick(session)
            prune_own_records(session)
        logger.info("single pass complete: %s", result)
        return 0

    last_record_prune = 0.0
    while not _stop:
        try:
            with Session(engine) as session:
                tick(session)
                if time.monotonic() - last_record_prune > 86400:
                    prune_own_records(session)
                    last_record_prune = time.monotonic()
        except Exception:
            # Including a database that is temporarily unreachable: the
            # loop must outlive a restart of anything it depends on.
            logger.exception("maintenance tick failed; continuing")
        for _ in range(TICK_SECONDS):
            if _stop:
                break
            time.sleep(1)
    logger.info("maintenance control loop stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
