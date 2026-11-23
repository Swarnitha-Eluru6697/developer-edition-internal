from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from ..db import get_session
from ..models import Trace
from ..pagination import paginate, pagination_params

router = APIRouter(prefix="/api/traces", tags=["traces"])


@router.get("", dependencies=[Depends(require_any_permission(P.TRACES, P.RUNS))])
def list_traces(
    response: Response, agent_id: Optional[str] = None,
    from_date: Optional[str] = None, to_date: Optional[str] = None,
    session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[Trace]:
    """§0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py's own identical gap; see that module's own docstring for
    the full real reasoning. Ordered by `started_at` (Trace's own real,
    typed timestamp — §0.79, GAP_REPORT), not `ts` (a real, existing
    display STRING with no real ordering guarantee a SQL query could
    trust)."""
    q = select(Trace)
    if agent_id and agent_id != "all":
        q = q.where(Trace.agent_id == agent_id)

    # Date filtering belongs here, not in the client. This endpoint is
    # paginated, so filtering a page in the browser would narrow only
    # the rows that happened to be fetched and silently hide every
    # matching trace on later pages — the answer would depend on how far
    # someone had scrolled.
    #
    # Filtered on `started_at`, the real typed timestamp this query
    # already orders by (§0.79); `ts` is a display string with no
    # ordering a SQL comparison could trust.
    #
    # `to_date` is treated as INCLUSIVE of the whole day: a person
    # picking the same date in both boxes means "that day", not an empty
    # range, and a plain `<=` against midnight would return nothing.
    for raw, is_end in ((from_date, False), (to_date, True)):
        if not raw:
            continue
        try:
            bound = datetime.fromisoformat(raw)
        except ValueError:
            raise HTTPException(
                400, f"{'to_date' if is_end else 'from_date'} must be ISO-8601 "
                     f"(YYYY-MM-DD or a full timestamp), got {raw!r}")
        if is_end:
            if len(raw) == 10:  # a bare date, so include its whole day
                bound = bound + timedelta(days=1)
            q = q.where(Trace.started_at < bound)
        else:
            q = q.where(Trace.started_at >= bound)
    return paginate(session, Trace, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=Trace.started_at, id_col=Trace.id, response=response)


@router.get("/{trace_id}", dependencies=[Depends(require_any_permission(P.TRACES, P.RUNS))])
def get_trace(trace_id: str, session: Session = Depends(get_session)) -> Trace:
    t = session.get(Trace, trace_id)
    if not t:
        raise HTTPException(404, "Trace not found")
    return t


@router.get("/{trace_id}/spans", dependencies=[Depends(require_any_permission(P.TRACES, P.RUNS))])
def get_trace_spans(trace_id: str, session: Session = Depends(get_session)):
    """Real spans, persisted from the actual execution.py run that
    produced this trace — genuinely what ran, in real node order, with
    real per-node timing and (where the node type produces one) a real
    content preview. This used to synthesize a fake, always-identical
    4-span waterfall regardless of what actually executed — fixed."""
    t = session.get(Trace, trace_id)
    if not t:
        raise HTTPException(404, "Trace not found")
    fail_map = {"ucs_denial": "ucs", "harness_fail": "harness", "orch_fail": "orchestration", "ok": None}
    fail = fail_map.get(t.status)

    kind_map = {
        "gov": "ucs", "trigger": "orch", "respond": "orch",
        "llm": "harn", "agent": "harn", "subagent": "harn",
    }
    spans = []
    for s in (t.spans_json or []):
        status = s.get("status", "ok")
        kind = "fail" if status in ("error", "ucs_denied", "harness_fail") else kind_map.get(s.get("node"), "orch")
        note = None
        if status == "ucs_denied":
            note = "DENIED"
        elif status == "harness_fail":
            note = "STEP LIMIT"
        elif status == "error":
            note = (s.get("error") or "ERROR")[:60]
        elif status == "skipped":
            note = "not wired yet"
        spans.append({
            "name": s.get("node") or "unknown", "kind": kind,
            "duration_ms": s.get("duration_ms", 0), "note": note,
            "input_preview": s.get("input_preview"), "output_preview": s.get("output_preview"),
        })
    return {
        "trace_id": trace_id, "fail_class": fail, "spans": spans,
        "user_input": t.user_input, "final_output": t.final_output,
    }
