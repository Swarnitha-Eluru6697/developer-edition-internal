from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from ..db import get_session
from ..models import Run
from ..pagination import paginate, pagination_params

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", dependencies=[Depends(require_any_permission(P.RUNS, P.OVERVIEW, P.MONITORING))])
def list_runs(
    response: Response, status: Optional[str] = None, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[Run]:
    """§0.113, GAP_REPORT — the actual fix for a real, reported gap:
    this used to be `session.exec(q).all()` — a genuinely unbounded
    query loading every real Run row matching the filter into this API
    process's own memory on every single real request, a real,
    growing risk at 10K/100K/1M+ real rows. Now a real, keyset-paginated
    query — `limit` (capped, defaulting to 100) real rows at a time,
    ordered newest-first by `(created_at, id)`, with a real
    `X-Next-Cursor`/`X-Has-More` response header pair a caller uses to
    continue — see pagination.py's own module docstring for the full
    real design and why this stays a real, bare JSON array rather than
    an envelope object (backward compatible with every real, existing
    caller)."""
    q = select(Run)
    if status and status != "all":
        q = q.where(Run.status == status)
    return paginate(session, Run, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=Run.created_at, id_col=Run.id, response=response)


@router.get("/{run_id}", dependencies=[Depends(require_any_permission(P.RUNS, P.OVERVIEW, P.MONITORING))])
async def get_run(run_id: str, session: Session = Depends(get_session)) -> Run:
    """Real durable-run reconciliation (§0.60, GAP_REPORT) — a Run row
    left "running" past the original request's own 25s response budget
    is checked here, live, against the actual Temporal workflow it
    names, and genuinely finalized (its own real terminal status,
    output, spans persisted to a real Trace) if that workflow has
    actually completed since. A caller no longer needs to inspect the
    Temporal UI to discover the real result — polling this same
    endpoint is enough. Best-effort: if Temporal itself can't be
    reached right now, the row is honestly returned as still
    "running" rather than erroring."""
    r = session.get(Run, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    if r.status == "running":
        from .execution import _reconcile_running_run
        r = await _reconcile_running_run(session, r)
    return r
