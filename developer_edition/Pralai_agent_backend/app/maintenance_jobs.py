"""The maintenance control loop's jobs, as plain functions.

The reported gap: cleanup and reconciliation existed only as functions
someone had to remember to invoke. Nothing in the shipped deployment ran
them, so expired uploads, stale HITL claims, completed outbox rows and
idempotency keys accumulated indefinitely. Measured on this project's own
development database before this was written: 124 completed
KBIngestionOutbox rows going back to 2026-09-07, none of which any code
path would ever have removed.

Each job returns a dict describing what it actually did, and is safe to
run repeatedly — the scheduler is a control loop, so every job has to be
idempotent rather than assume it runs exactly once.

Two things the report lists are deliberately NOT here, because shipping a
job that silently does nothing would be worse than admitting the gap:

  * Orphaned storage objects (an object in the bucket with no
    UploadedFile row). Detecting them needs to enumerate the bucket, and
    StorageBackend exposes only put/get/delete/exists — no listing. That
    is a backend API change, not a scheduling one.
  * AgentLog retention. AgentLog.ts is a display string ("14:08:44.102")
    with no date in it, so there is nothing to compare a cutoff against.
    Pruning by row id would delete by insertion order, which is not the
    same thing as age and would be wrong the moment ids are reused or
    backfilled. That needs a real timestamp column first.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Callable, Dict, List, NamedTuple

from sqlmodel import Session, delete, select

logger = logging.getLogger(__name__)


def _days(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("%s is not an integer; using the default of %s days", name, default)
        return default


# ---------------------------------------------------------------- jobs

def sweep_expired_uploads_job(session: Session) -> dict:
    """Delete uploads past their retention TTL, and their stored objects."""
    from .routers.uploads import sweep_expired_uploads

    return {"swept": sweep_expired_uploads(session)}


def reconcile_stale_hitl_job(session: Session) -> dict:
    """Release HITL requests stuck mid-claim by a process that died."""
    from .routers.execution import reconcile_stale_hitl_requests

    return {"reconciled": reconcile_stale_hitl_requests(session)}


def reconcile_outbox_job(session: Session) -> dict:
    """Finish outbox rows whose remote commit succeeded but whose local
    commit never did — across every knowledge base.

    reconcile_kb is per-KB and was reachable only by naming one, so
    drift in a KB nobody thought to check stayed there. A control loop
    cannot depend on someone guessing which KB crashed."""
    from .models import KnowledgeBase
    from .routers.knowledge_bases import reconcile_kb

    all_kbs = session.exec(select(KnowledgeBase)).all()
    # Only providers whose document management is actually wired.
    # reconcile_kb raises a 400 for the others, and recording that as a
    # per-KB "error" every 15 minutes turns the log into noise and makes
    # a real failure impossible to spot. Not applicable is not a failure.
    candidates = [kb for kb in all_kbs if kb.provider == "pgvector"]

    repaired: Dict[str, dict] = {}
    for kb in candidates:
        try:
            result = reconcile_kb(kb.id, session)
        except Exception as e:  # one broken KB must not stop the rest
            repaired[kb.id] = {"error": str(e)[:200]}
            continue
        # Only report the ones that actually had something to do, or the
        # log is a wall of zeroes.
        if any(isinstance(v, int) and v for v in result.values()):
            repaired[kb.id] = result
    return {"knowledge_bases_checked": len(candidates),
            "skipped_unsupported_provider": len(all_kbs) - len(candidates),
            "repaired": repaired}


def prune_completed_outbox_job(session: Session) -> dict:
    """Remove outbox rows that have already done their job.

    An outbox row exists to survive a crash between two commits. Once it
    is `completed` it is history, and it is the single fastest-growing
    table in this platform: one row per ingested document per KB,
    forever. `failed` rows are LEFT ALONE deliberately — they are the
    record of something that needs attention, and a cleanup job that
    quietly deletes evidence of failures is worse than no cleanup."""
    from .models import KBIngestionOutbox

    cutoff = datetime.utcnow() - timedelta(days=_days("OUTBOX_RETENTION_DAYS", 7))
    rows = session.exec(
        select(KBIngestionOutbox).where(
            KBIngestionOutbox.status == "completed",
            KBIngestionOutbox.created_at < cutoff,
        )
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return {"pruned": len(rows), "kept_failed_rows_for_inspection": True,
            "cutoff": cutoff.isoformat()}


def prune_idempotency_keys_job(session: Session) -> dict:
    """Remove idempotency keys older than the window in which a retry
    could still arrive. They are write-once markers that nothing ever
    deletes, so the table only grows."""
    from .models import ActivityIdempotency

    cutoff = datetime.utcnow() - timedelta(days=_days("IDEMPOTENCY_RETENTION_DAYS", 7))
    rows = session.exec(
        select(ActivityIdempotency).where(ActivityIdempotency.created_at < cutoff)
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return {"pruned": len(rows), "cutoff": cutoff.isoformat()}


def prune_execution_history_job(session: Session) -> dict:
    """Remove Run and Trace rows older than RUN_RETENTION_DAYS.

    OFF by default (0 = keep forever), and that default is deliberate:
    this is the one job here that deletes a record of something a person
    actually did. Shipping it enabled would mean an upgrade silently
    destroying execution history nobody agreed to lose. An operator sets
    a window knowingly.
    """
    from .models import Run, Trace

    window = _days("RUN_RETENTION_DAYS", 0)
    if window <= 0:
        return {"skipped": "RUN_RETENTION_DAYS is unset or 0 — execution history is kept forever"}

    cutoff = datetime.utcnow() - timedelta(days=window)
    traces = session.exec(select(Trace).where(Trace.started_at < cutoff)).all()
    for t in traces:
        session.delete(t)
    runs = session.exec(select(Run).where(Run.created_at < cutoff)).all()
    for r in runs:
        session.delete(r)
    session.commit()
    return {"runs_pruned": len(runs), "traces_pruned": len(traces), "cutoff": cutoff.isoformat()}


# ------------------------------------------------------------ registry

class Job(NamedTuple):
    name: str
    run: Callable[[Session], dict]
    interval_seconds: int
    description: str


def _interval(name: str, default_seconds: int) -> int:
    try:
        value = int(os.getenv(name, str(default_seconds)))
    except ValueError:
        return default_seconds
    return max(60, value)  # a control loop that runs every second is a busy loop


def evaluate_alert_rules_job(session: Session) -> dict:
    """Raise alerts for conditions that are genuinely true, and deliver
    them to the configured Slack/Teams channels.

    Before this, every row on the Alerts page came from seed.py and
    nothing in the platform ever raised one — the page was a mock-up and
    the channels had nothing to carry. Alerts that have cleared are
    resolved in the same pass, because dedup keys on an OPEN alert: one
    left open forever would stop the condition ever being reported
    again.
    """
    from . import alert_rules
    result = alert_rules.sweep(session)
    result["resolved"] = str(alert_rules.resolve_stale(session))
    return result


def publish_agent_quality_job(session: Session) -> dict:
    """Publish every published agent's measured quality to UCS.

    Lineage publishes itself on each run, because a run is an event.
    Quality is a measurement over a window — it changes as runs and eval
    results accumulate even when nothing new happens — so it is swept
    rather than triggered, and the catalogue is kept in step without
    anyone pressing a button.

    Draft agents are skipped: they are not governed objects yet, and
    publishing a verdict for something nobody can invoke would put rows
    in the catalogue for work in progress.
    """
    from . import agent_catalog
    from .models import Agent

    agents = session.exec(select(Agent).where(Agent.status == "published")).all()
    sent = failed = skipped = 0
    for agent in agents:
        try:
            result = agent_catalog.publish_quality(session, agent)
            sent += len(result["sent"])
            failed += len(result["failed"])
            if not result["sent"] and not result["failed"]:
                skipped += 1
        except Exception:
            logger.warning("Could not publish quality for %s", agent.id, exc_info=True)
            failed += 1
    return {"agents": str(len(agents)), "dimensions_sent": str(sent),
            "failed": str(failed), "nothing_measured": str(skipped)}


def jobs() -> List[Job]:
    """Every scheduled job, with its interval.

    Intervals are per-job on purpose: releasing a stuck HITL claim
    matters within minutes (somebody is waiting on a form), while
    pruning week-old outbox rows does not need to happen more than daily.
    """
    return [
        Job("reconcile-stale-hitl", reconcile_stale_hitl_job,
            _interval("MAINT_HITL_INTERVAL_SECONDS", 300),
            "release HITL requests stuck mid-claim"),
        Job("sweep-expired-uploads", sweep_expired_uploads_job,
            _interval("MAINT_UPLOAD_SWEEP_INTERVAL_SECONDS", 3600),
            "delete uploads past their TTL and their stored objects"),
        Job("reconcile-outbox", reconcile_outbox_job,
            _interval("MAINT_OUTBOX_RECONCILE_INTERVAL_SECONDS", 900),
            "finish KB writes whose remote half committed and local half did not"),
        Job("prune-completed-outbox", prune_completed_outbox_job,
            _interval("MAINT_OUTBOX_PRUNE_INTERVAL_SECONDS", 86400),
            "remove outbox rows that have already completed"),
        Job("prune-idempotency-keys", prune_idempotency_keys_job,
            _interval("MAINT_IDEMPOTENCY_PRUNE_INTERVAL_SECONDS", 86400),
            "remove expired idempotency markers"),
        Job("prune-execution-history", prune_execution_history_job,
            _interval("MAINT_RUN_PRUNE_INTERVAL_SECONDS", 86400),
            "remove Run/Trace rows beyond RUN_RETENTION_DAYS (off by default)"),
        # Every 15 minutes: fast enough that an outage is reported while
        # it is still happening, slow enough that a flapping condition
        # does not post repeatedly. Dedup does the rest.
        # Hourly: quality moves as runs accumulate, but not minute to
        # minute, and each sweep is one call per dimension per agent.
        Job("publish-agent-quality", publish_agent_quality_job,
            _interval("MAINT_QUALITY_PUBLISH_INTERVAL_SECONDS", 3600),
            "publish each agent's measured quality to the UCS catalogue"),
        Job("evaluate-alert-rules", evaluate_alert_rules_job,
            _interval("MAINT_ALERT_RULES_INTERVAL_SECONDS", 900),
            "raise alerts for real conditions and send them to Slack/Teams"),
    ]


def job_by_name(name: str) -> Job:
    for job in jobs():
        if job.name == name:
            return job
    raise KeyError(f"no such maintenance job: {name!r}. Known: {[j.name for j in jobs()]}")
