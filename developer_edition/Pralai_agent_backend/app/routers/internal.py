"""Operator maintenance operations, off the product API surface.

Storage sweeping, bulk storage-backend migration and reconciliation used
to be ordinary routes on the product routers, unauthenticated, marked
only by a leading underscore in the path. They are operationally
privileged: `_migrate-backend` relocates up to 10,000 objects per call
and `_sweep-expired` deletes database rows together with their storage
objects.

Nothing here is re-implemented. Each route registers the SAME function
the product router used to expose, which is why moving the boundary
cannot change what the operation does — a second copy of a bulk delete
is the last thing this should introduce.

Three layers, in the order a request meets them:

  1. The /internal prefix, which an ingress refuses in one rule. This
     repo's nginx.conf returns 404 for it, so a browser client cannot
     reach these paths at all.
  2. require_operator, which fails closed: a gateway-verified principal
     holding agent:maintenance:run, or no HTTP route to these operations.
  3. The CLI (app/maintenance.py), which is the supported path for a
     scheduler or a Kubernetes Job and needs no network surface
     whatsoever.

Excluded from the OpenAPI schema on purpose: the published contract is
the product API, and a maintenance operation appearing there invites a
client to depend on it.
"""
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from ..db import get_session
from gateway_auth import require_permission

from ..internal_auth import require_operator
from ..permissions import MAINTENANCE
from .execution import trigger_stale_hitl_reconcile
from .knowledge_bases import reconcile_kb
from .uploads import trigger_backend_migration, trigger_expired_sweep

router = APIRouter(
    prefix="/internal/maintenance",
    tags=["internal-maintenance"],
    # Applied to every route on this router, so a route added later
    # cannot forget it — the failure mode that produced this module.
    dependencies=[Depends(require_permission(MAINTENANCE))],
    include_in_schema=False,
)


@router.post("/uploads/sweep-expired")
def sweep_expired(operator: str = Depends(require_operator),
                  session: Session = Depends(get_session)) -> dict:
    """Delete uploads past their retention TTL, and their stored objects."""
    result = trigger_expired_sweep(session)
    return {**result, "operator": operator}


@router.post("/uploads/migrate-backend")
def migrate_backend(target: str = Query(..., description="Target storage backend name, e.g. 'local' or 's3'"),
                    limit: int = Query(default=100, ge=1, le=10000),
                    operator: str = Depends(require_operator),
                    session: Session = Depends(get_session)) -> dict:
    """Move stored objects to a different storage backend, in batches."""
    result = trigger_backend_migration(target=target, limit=limit, session=session)
    return {**result, "operator": operator}


@router.post("/hitl/reconcile-stale")
def reconcile_stale_hitl(operator: str = Depends(require_operator),
                         session: Session = Depends(get_session)) -> dict:
    """Resolve HITL requests whose owning run is no longer live."""
    result = trigger_stale_hitl_reconcile(session)
    return {**result, "operator": operator}


@router.post("/knowledge-bases/{kb_id}/reconcile")
def reconcile_knowledge_base(kb_id: str,
                             operator: str = Depends(require_operator),
                             session: Session = Depends(get_session)) -> dict:
    """Finish outbox rows whose remote commit succeeded but whose local
    commit never did."""
    result = reconcile_kb(kb_id, session)
    return {**result, "operator": operator}


@router.get("/status")
def maintenance_status(operator: str = Depends(require_operator),
                       session: Session = Depends(get_session)) -> dict:
    """Whether the scheduled control loop is actually running each job.

    The question the reported gap made unanswerable. Every job reports
    when it last succeeded, whether it is currently overdue, and by how
    much — so a monitoring system can alert on `overdue` without
    knowing anything about this platform's internals, and an operator
    can see at a glance that retention stopped three weeks ago instead
    of discovering it from a full disk.
    """
    from ..maintenance_jobs import jobs
    from ..maintenance_scheduler import OVERDUE_FACTOR, last_success, overdue_by
    from ..models import MaintenanceRun

    out = []
    for job in jobs():
        previous = last_success(session, job.name)
        late = overdue_by(session, job)
        recent = session.exec(
            select(MaintenanceRun)
            .where(MaintenanceRun.job == job.name)
            .order_by(MaintenanceRun.started_at.desc())
        ).first()
        out.append({
            "job": job.name,
            "description": job.description,
            "interval_seconds": job.interval_seconds,
            "last_success": previous.isoformat() if previous else None,
            "last_status": recent.status if recent else None,
            "last_detail": recent.detail if recent else None,
            "last_error": recent.error if recent else None,
            "overdue": late is not None,
            "overdue_by_seconds": int(late.total_seconds()) if late else 0,
        })
    return {
        "operator": operator,
        "overdue_factor": OVERDUE_FACTOR,
        "any_overdue": any(j["overdue"] for j in out),
        "jobs": out,
    }


@router.post("/run/{job_name}")
def run_one_job(job_name: str,
                operator: str = Depends(require_operator),
                session: Session = Depends(get_session)) -> dict:
    """Run a single scheduled job now, on demand.

    The scheduler owns the routine cadence; this is for an operator who
    needs one job to run immediately without waiting for its interval.
    It records a MaintenanceRun exactly as the loop does, so an
    out-of-band run is visible in the same history rather than being
    invisible activity."""
    from ..maintenance_jobs import job_by_name
    from ..maintenance_scheduler import run_job

    try:
        job = job_by_name(job_name)
    except KeyError as e:
        from fastapi import HTTPException
        raise HTTPException(404, str(e))
    record = run_job(session, job)
    return {"job": job.name, "status": record.status, "detail": record.detail,
            "error": record.error, "duration_ms": record.duration_ms, "operator": operator}


# Diagnostics, on its own router because it is not maintenance: nothing
# here changes state. Same three layers regardless — the /internal
# prefix an ingress refuses, require_operator failing closed, and no
# OpenAPI entry.
diagnostics_router = APIRouter(
    prefix="/internal/diagnostics",
    tags=["internal-diagnostics"],
    dependencies=[Depends(require_permission(MAINTENANCE))],
    include_in_schema=False,
)


@diagnostics_router.get("/readyz")
async def readyz_detail(operator: str = Depends(require_operator)) -> dict:
    """Readiness with the diagnostic detail /readyz no longer returns.

    Temporal's address, the task queue and its poller count, the UCS URL
    and http status, and the exception text behind any component that is
    failing. That detail used to be on the public probe, where an
    unauthenticated caller could read this deployment's internal
    topology out of a health check.

    Registers health.readyz_detail rather than recomputing anything, for
    the same reason every other route on this module registers the
    function the product router used to expose: a second implementation
    of a readiness check is a second answer about the same process.
    """
    from ..health import readyz_detail as _readyz_detail

    return {**await _readyz_detail(), "operator": operator}
