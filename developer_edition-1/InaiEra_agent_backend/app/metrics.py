"""
Real Prometheus instrumentation — genuine counters/histograms updated at
the actual call sites where runs happen (execution.py's /run endpoint,
webhooks.py's webhook trigger, routers/evals.py's eval runs), not
synthesized at scrape time from a DB snapshot. `GET /metrics` (mounted at
the app root in main.py, not under /api, matching Prometheus's own
convention of a bare /metrics path) exposes these in real Prometheus
exposition format via prometheus_client's own generate_latest() — point a
real Prometheus server's scrape config at this app and it genuinely
works. Standing up that scraper itself is infrastructure outside this
app's own repo; what's real here is that there's something correct for it
to scrape.
"""
from fastapi import APIRouter
from gateway_auth import require_permission
from fastapi import Depends
from . import permissions as P
from .permissions import require_any_permission
from fastapi.responses import Response
import logging
from datetime import datetime

from prometheus_client import (Counter, Gauge, Histogram, CollectorRegistry, generate_latest,
                               CONTENT_TYPE_LATEST)

logger = logging.getLogger(__name__)

REGISTRY = CollectorRegistry()

AGENT_RUNS_TOTAL = Counter(
    "inaiera_agent_runs_total", "Total real agent executions",
    ["agent_id", "status", "trigger"], registry=REGISTRY,
)
AGENT_RUN_DURATION_SECONDS = Histogram(
    "inaiera_agent_run_duration_seconds", "Real agent execution duration in seconds",
    ["agent_id"], registry=REGISTRY,
)
UCS_DENIALS_TOTAL = Counter(
    "inaiera_ucs_denials_total", "Real UCS entitlement denials encountered during execution",
    ["agent_id", "enforced"], registry=REGISTRY,  # enforced=true (hard-blocked) | false (UCS_SOFT_CHECK logged only)
)
EVAL_RUNS_TOTAL = Counter(
    "inaiera_eval_runs_total", "Real eval set runs, by resulting drift classification",
    ["agent_id", "drift"], registry=REGISTRY,
)

# Scheduled maintenance freshness — how a missed retention run becomes
# an alert somewhere a human will see it.
#
# Gauges, not the Alert table: Alert.agent_id is NOT NULL with a foreign
# key to agent.id, so that table is per-agent by construction and a
# platform-level alert cannot be represented in it without inventing a
# fake agent row. A scrape-and-alert rule on these is also what an
# operator's own monitoring already does for every other loop they run.
#
# Alert on: inaiera_maintenance_job_overdue == 1, or
# inaiera_maintenance_last_success_age_seconds > your own threshold.
MAINTENANCE_JOB_OVERDUE = Gauge(
    "inaiera_maintenance_job_overdue",
    "1 when a scheduled maintenance job has not succeeded within its allowed window",
    ["job"], registry=REGISTRY,
)
MAINTENANCE_LAST_SUCCESS_AGE_SECONDS = Gauge(
    "inaiera_maintenance_last_success_age_seconds",
    "Seconds since a scheduled maintenance job last completed successfully",
    ["job"], registry=REGISTRY,
)
MAINTENANCE_JOB_RUNS_TOTAL = Counter(
    "inaiera_maintenance_job_runs_total", "Scheduled maintenance job runs, by outcome",
    ["job", "status"], registry=REGISTRY,
)


router = APIRouter(tags=["metrics"])


def refresh_maintenance_gauges() -> None:
    """Recompute maintenance freshness from the database at scrape time.

    Necessary because prometheus_client gauges are PER PROCESS and the
    maintenance scheduler is its own container with no HTTP endpoint —
    so gauges it set were unscrapeable, and this endpoint reported
    nothing at all for them. Caught by scraping the real deployment;
    the unit tests could not see it because they run the scheduler and
    the endpoint in one process.

    Deriving from MaintenanceRun instead means any replica answers
    correctly, the numbers cannot drift from what actually ran, and a
    scheduler that is completely dead still produces a rising
    last_success_age and an overdue gauge of 1 — which is exactly the
    condition that needs alerting.
    """
    try:
        from sqlmodel import Session

        from .db import engine
        from .maintenance_jobs import jobs
        from .maintenance_scheduler import last_success, overdue_by

        with Session(engine) as session:
            now = datetime.utcnow()
            for job in jobs():
                MAINTENANCE_JOB_OVERDUE.labels(job=job.name).set(
                    1 if overdue_by(session, job, now) else 0)
                previous = last_success(session, job.name)
                if previous:
                    MAINTENANCE_LAST_SUCCESS_AGE_SECONDS.labels(job=job.name).set(
                        (now - previous).total_seconds())
    except Exception:
        # A scrape must never fail because of this: the rest of the
        # metrics are more valuable than these two, and a 500 on
        # /metrics looks like the whole service is down.
        logger.exception("could not refresh maintenance gauges")


@router.get("/metrics", dependencies=[Depends(require_permission(P.MONITORING))])
def metrics():
    refresh_maintenance_gauges()
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
