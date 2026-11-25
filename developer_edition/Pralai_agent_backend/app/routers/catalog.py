"""Agent runtime surfaces, addressed by UCS catalogue FQN.

UCS's own object page carries Api Access, Logs, Monitoring and Traces
tabs for an agent, and renders each as a placeholder saying:

    "<tab> is served by the agent platform. UCS catalogues this agent
     and governs access to it; its runtime surfaces live in the agent
     service. The tab is here so the object reads the same way in both
     places — wiring it means pointing this panel at that service's
     endpoint for <fqn>."

That is the contract these endpoints satisfy. UCS knows an agent by its
dotted catalogue name, not by this platform's internal id, so every
route here is keyed by FQN — otherwise the catalogue would have to hold
a second identifier for the same object, which is the kind of mapping
that goes stale.

Read-only, and deliberately the same data the agent drawer shows: the
point is that the object reads identically in both places, so a second
source here would defeat it.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select

from ..db import get_session
from ..models import Agent, AgentLog, Run, Trace
from ..pagination import paginate, pagination_params
from .. import ucs_client

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _agent_from_fqn(fqn: str, session: Session) -> Agent:
    """Resolve a UCS FQN to the agent it names.

    The FQN is `<agent-slug>.<schema>.<agent id>`, so the id is the last
    segment. It is checked by round-tripping rather than trusting the
    split: a request for `anything.at.all.a04c5406699` would otherwise
    resolve to a real agent through a name that is not its own, and the
    catalogue would appear to agree with a wrong FQN.
    """
    parts = (fqn or "").split(".")
    if len(parts) < 3:
        raise HTTPException(400, f"{fqn!r} is not a catalogue FQN (expected a dotted name)")
    agent = session.get(Agent, parts[-1])
    if not agent:
        raise HTTPException(404, f"No agent is catalogued as {fqn!r}")
    if ucs_client.agent_fqn(agent.id, agent.name) != fqn:
        raise HTTPException(
            404,
            f"{fqn!r} does not name this agent — it is catalogued as "
            f"{ucs_client.agent_fqn(agent.id, agent.name)!r}. An agent that was renamed "
            "changes slug, so the catalogue entry needs re-registering.")
    return agent


def _identity(agent: Agent) -> dict:
    return {"fqn": ucs_client.agent_fqn(agent.id, agent.name),
            "agent_id": agent.id, "agent_name": agent.name}


@router.get("/{fqn}/api-access", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def catalog_api_access(fqn: str, session: Session = Depends(get_session)) -> dict:
    """How this agent can be invoked, and what it demands of a caller."""
    agent = _agent_from_fqn(fqn, session)
    features = agent.features or {}
    return {
        **_identity(agent),
        "status": agent.status,
        "version": agent.version,
        # Paths rather than absolute URLs: the host a caller should use
        # depends on where they are (gateway, container network, laptop),
        # and this service cannot know which.
        "run_endpoint": f"/api/agents/{agent.id}/run",
        "stream_endpoint": f"/api/agents/{agent.id}/run/stream",
        "mcp_enabled": bool(getattr(agent, "mcp_enabled", False)),
        "mcp_endpoint": f"/mcp/{agent.id}" if getattr(agent, "mcp_enabled", False) else None,
        "requires_authenticated_user": bool(features.get("require_authenticated_user")),
        "published": agent.status == "published",
    }


@router.get("/{fqn}/logs", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def catalog_logs(fqn: str, response: Response, level: Optional[str] = None,
                 session: Session = Depends(get_session),
                 page: dict = Depends(pagination_params)):
    """This agent's log lines, newest first.

    Each row is returned with an `at` field: the full timestamp of the line, or null.

    AgentLog.ts is a bare clock string ("04:28:07.696") with no date, so a log row cannot
    say what day it belongs to. The only date available is the one on the trace the line
    was emitted under, so `at` is resolved from that — and is null for a line with no
    trace, which is about a third of them. Callers must treat null as "unknown date"
    rather than as a date they can compare; the alternative was inventing today's date for
    every undated line, which would make a date filter confidently wrong.
    """
    agent = _agent_from_fqn(fqn, session)
    q = select(AgentLog).where(AgentLog.agent_id == agent.id)
    if level:
        q = q.where(AgentLog.level == level)
    rows = paginate(session, AgentLog, q, limit=page["limit"], cursor=page["cursor"],
                    sort_col=AgentLog.id, id_col=AgentLog.id, response=response)

    # One query for the whole page rather than one per line.
    tids = {r.trace_id for r in rows if getattr(r, "trace_id", None)}
    starts: dict = {}
    if tids:
        for t in session.exec(select(Trace).where(Trace.id.in_(tids))).all():
            when = getattr(t, "started_at", None) or getattr(t, "ts", None)
            if when is not None:
                starts[t.id] = when

    out = []
    for r in rows:
        d = r.model_dump() if hasattr(r, "model_dump") else dict(r.__dict__)
        d.pop("_sa_instance_state", None)
        # The row's own timestamp wins; the trace's start is the fallback for lines
        # written before the column existed. Null stays null — an undated line is reported
        # as undated rather than given today's date, which a filter would then trust.
        when = getattr(r, "at", None) or starts.get(getattr(r, "trace_id", None))
        d["at"] = when.isoformat() if hasattr(when, "isoformat") else (when or None)
        out.append(d)
    return out


@router.get("/{fqn}/traces", dependencies=[Depends(require_permission(P.TRACES))])
def catalog_traces(fqn: str, response: Response,
                   session: Session = Depends(get_session),
                   page: dict = Depends(pagination_params)):
    """This agent's traces, newest first."""
    agent = _agent_from_fqn(fqn, session)
    q = select(Trace).where(Trace.agent_id == agent.id)
    return paginate(session, Trace, q, limit=page["limit"], cursor=page["cursor"],
                    sort_col=Trace.started_at, id_col=Trace.id, response=response)


@router.get("/{fqn}/monitoring", dependencies=[Depends(require_permission(P.MONITORING))])
def catalog_monitoring(fqn: str, session: Session = Depends(get_session)) -> dict:
    """The same metrics the Monitoring page computes for this agent.

    Imported here rather than recomputed: two places computing a success
    rate from the same rows is two numbers that can disagree about the
    same agent.
    """
    from .monitoring import _compute_platform_metrics

    agent = _agent_from_fqn(fqn, session)
    return {**_identity(agent), "metrics": _compute_platform_metrics(session, agent.id)}


@router.get("/{fqn}/live-jobs", dependencies=[Depends(require_permission(P.RUNS))])
def catalog_live_jobs(fqn: str, session: Session = Depends(get_session)) -> dict:
    """Runs of this agent that have not finished."""
    agent = _agent_from_fqn(fqn, session)
    rows = session.exec(
        select(Run).where(Run.agent_id == agent.id,
                          Run.status.in_(("running", "queued", "paused")))
        .order_by(Run.created_at.desc())).all()
    now = datetime.utcnow()
    return {**_identity(agent), "jobs": [{
        "id": r.id, "status": r.status,
        "started_at": r.created_at.isoformat() if r.created_at else None,
        "running_for_s": int((now - r.created_at).total_seconds()) if r.created_at else None,
        "invoking_user": r.invoking_user,
    } for r in rows]}


@router.get("/{fqn}", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def catalog_index(fqn: str, session: Session = Depends(get_session)) -> dict:
    """What this service serves for this object.

    Declared so the wiring is discoverable: someone pointing a UCS panel
    here can ask what exists rather than reading this file.
    """
    agent = _agent_from_fqn(fqn, session)
    return {**_identity(agent), "surfaces": {
        name: f"/api/catalog/{fqn}/{name}"
        for name in ("api-access", "logs", "traces", "monitoring", "live-jobs")
    }}
