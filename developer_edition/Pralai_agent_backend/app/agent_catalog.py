"""Lineage and quality for an agent, computed here and published to UCS.

This platform is the source of truth for both. It is the thing that runs
agents, so it is the only thing that knows what an agent read and how
well it did; UCS is the catalogue those facts are published INTO, so
that an agent appears alongside every other governed object with the
same lineage and quality vocabulary.

An earlier version of this had the arrow pointing the other way — the
agent drawer asked UCS for lineage and quality and rendered whatever
came back. That was wrong twice over: UCS only knows what it has been
told, so the panels showed an empty graph and a zero score for agents
this platform had full history for, and it made the catalogue the
authority on data it does not own.

So: compute here, show here, push there.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlmodel import Session, select

from . import ucs_client
from .models import Agent, EvalRun, EvalSet, KnowledgeBase, Run

logger = logging.getLogger(__name__)

# How the numbers become verdicts. Stated once here so the drawer, the
# push to UCS and any future report cannot disagree about what "pass"
# means for the same agent.
EVAL_PASS_OK = 90.0
EVAL_PASS_WARN = 70.0
RUN_SUCCESS_OK = 95.0
RUN_SUCCESS_WARN = 80.0
QUALITY_WINDOW_DAYS = 7

# How each agent dimension is named in UCS, and what it is worth there.
#
# UCS scores an object as a weighted average over the dimensions it has,
# and its weights already carry entries for the agent ones:
# reliability 0.20, drift 0.15, validity 0.10. Pushing reliability and
# drift as "custom" — which is what this did first — put them in a
# bucket the weights do not mention, so they collapsed into one chip and
# were scored at a default weight instead of the ones UCS intends.
#
# `accuracy` has no entry in UCS's enum, so it rides on `validity`: an
# eval case passing is the agent producing a valid answer. The check
# keeps the name `agent:accuracy`, so the Checks table still tells them
# apart.
UCS_DIMENSION = {
    "accuracy": "validity",
    "reliability": "reliability",
    "drift": "drift",
}

# UCS's own scoring, reproduced exactly so the two screens cannot show
# different numbers for the same agent.
#
# It does NOT average the observed percentages — it buckets each
# dimension's verdict (pass 100 / warn 70 / fail 0), takes a weighted
# average over the dimensions present, then caps the result if any
# dimension failed. Averaging the raw percentages here, which is what
# this did first, produced 38 against the catalogue's 0 for the same
# agent on the same data.
UCS_STATUS_VALUE = {"pass": 100, "warn": 70, "fail": 0}
UCS_WEIGHTS = {"validity": 0.10, "reliability": 0.20, "drift": 0.15}
UCS_DEFAULT_WEIGHT = 0.1
UCS_FAIL_SCORE_CAP = 50


def catalogue_score(dims: Dict[str, Dict[str, Any]]) -> tuple[Optional[int], str]:
    """The score and status UCS will compute from these dimensions.

    Returns (None, "unknown") when nothing was measured — an agent with
    no eval runs and no runs has not scored zero, and saying so is the
    difference between a coverage gap and a failing agent.
    """
    measured = {name: d["verdict"] for name, d in dims.items()
                if d["verdict"] in UCS_STATUS_VALUE}
    if not measured:
        return None, "unknown"

    num = den = 0.0
    for name, verdict in measured.items():
        weight = UCS_WEIGHTS.get(UCS_DIMENSION.get(name, "custom"), UCS_DEFAULT_WEIGHT)
        num += UCS_STATUS_VALUE[verdict] * weight
        den += weight
    score = round(num / den) if den else 100

    worst = max(measured.values(), key=lambda v: ("pass", "warn", "fail").index(v))
    if worst == "fail":
        score = min(score, UCS_FAIL_SCORE_CAP)
    return int(score), worst


def _verdict(pct: Optional[float], ok: float, warn: float) -> str:
    if pct is None:
        return "unknown"
    if pct >= ok:
        return "pass"
    if pct >= warn:
        return "warn"
    return "fail"


def knowledge_bases_read(session: Session, flow: Optional[dict]) -> List[Dict[str, str]]:
    """The knowledge bases this flow declares it reads.

    Declared, not observed: a KB that was searched and matched nothing
    is still a read, and inferring lineage from what a run happened to
    return would quietly drop it.

    Flows are inconsistent about what they store — some hold the KB id
    (`scen_qa_hq`), others the display name (`ACME Support FAQ`) — so
    each reference is resolved to a real row before its catalogue name
    is built. Left unresolved, a display name produced an FQN with
    spaces in it that matched nothing in the catalogue.
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for node in ((flow or {}).get("nodes") or []):
        if node.get("type") != "retrieval":
            continue
        for ref in ((node.get("config") or {}).get("knowledge_bases") or []):
            kb = session.get(KnowledgeBase, ref)
            if kb is None:
                kb = session.exec(
                    select(KnowledgeBase).where(KnowledgeBase.name == ref)).first()
            if kb is None:
                logger.warning("Flow references unknown knowledge base %r", ref)
                continue
            # A KB backed by an already-governed table names its own
            # catalogue object; honour that over the default location.
            fqn = (kb.config or {}).get("catalog_object") or ucs_client.kb_fqn(kb.id)
            if fqn in seen:
                continue
            seen.add(fqn)
            out.append({"id": kb.id, "name": kb.name, "fqn": fqn})
    return out


def agent_lineage(session: Session, agent: Agent) -> dict:
    """What this agent reads and produces, from this platform's own data."""
    upstream = knowledge_bases_read(session, agent.flow)
    recent = session.exec(
        select(Run).where(Run.agent_id == agent.id)
        .order_by(Run.created_at.desc()).limit(5)).all()
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "fqn": ucs_client.agent_fqn(agent.id, agent.name),
        "upstream": upstream,
        # An agent's output is the agent object itself in the catalogue:
        # nothing here writes into another governed table, and inventing
        # a "generated report" node would be a claim with nothing behind
        # it — which is exactly what the hardcoded panel used to do.
        "downstream": [],
        "runs": [{"id": r.id, "status": r.status,
                  "ts": r.created_at.isoformat() if r.created_at else None}
                 for r in recent],
    }


def agent_quality(session: Session, agent: Agent) -> dict:
    """This agent's quality, measured from its own evals and runs.

    Three dimensions, each from a real table:

      * accuracy   — the latest completed run of each eval set
      * reliability— the share of runs that succeeded in the window
      * drift      — what the latest eval run recorded

    A dimension with nothing behind it reports "unknown" rather than a
    number. An agent with no eval sets has not scored 0% on them; it has
    not been measured, and saying so is the difference between a gap in
    coverage and a failing agent.
    """
    cutoff = datetime.utcnow() - timedelta(days=QUALITY_WINDOW_DAYS)

    # Accuracy: the latest completed run per eval set, then pooled. Pooled
    # by case rather than averaging percentages, so a 20-case set does not
    # carry the same weight as a 2-case one.
    eval_rows = session.exec(text("""
        SELECT DISTINCT ON (er.eval_set_id)
               er.eval_set_id, es.name, er.pass_count, er.total_count, er.drift, er.started_at
        FROM evalrun er JOIN evalset es ON es.id = er.eval_set_id
        WHERE er.agent_id = :agent_id AND er.total_count > 0
        ORDER BY er.eval_set_id, er.started_at DESC
    """), params={"agent_id": agent.id}).all()

    passed = sum(r[2] for r in eval_rows)
    total = sum(r[3] for r in eval_rows)
    eval_pct = (100.0 * passed / total) if total else None

    run_row = session.exec(text("""
        SELECT COUNT(*) AS runs,
               COUNT(*) FILTER (WHERE status = 'success') AS ok
        FROM run WHERE agent_id = :agent_id AND created_at >= :cutoff
    """), params={"agent_id": agent.id, "cutoff": cutoff}).first()
    runs, ok = (run_row[0], run_row[1]) if run_row else (0, 0)
    run_pct = (100.0 * ok / runs) if runs else None

    drift = "none"
    if eval_rows:
        latest = max(eval_rows, key=lambda r: r[5])
        drift = latest[4] or "none"

    dims = {
        "accuracy": {
            "verdict": _verdict(eval_pct, EVAL_PASS_OK, EVAL_PASS_WARN),
            "observed": round(eval_pct, 1) if eval_pct is not None else None,
            "threshold": EVAL_PASS_OK,
            "detail": (f"{passed}/{total} eval cases passed across "
                       f"{len(eval_rows)} set(s)" if total else "no eval runs yet"),
            "rows_scanned": total,
        },
        "reliability": {
            "verdict": _verdict(run_pct, RUN_SUCCESS_OK, RUN_SUCCESS_WARN),
            "observed": round(run_pct, 1) if run_pct is not None else None,
            "threshold": RUN_SUCCESS_OK,
            "detail": (f"{ok}/{runs} runs succeeded in the last "
                       f"{QUALITY_WINDOW_DAYS} days" if runs
                       else f"no runs in the last {QUALITY_WINDOW_DAYS} days"),
            "rows_scanned": runs,
        },
        "drift": {
            "verdict": {"none": "pass", "watch": "warn"}.get(drift, "fail"),
            "observed": None,
            "threshold": None,
            "detail": f"latest eval run recorded drift: {drift}",
            "rows_scanned": len(eval_rows),
        },
    }

    # Scored the way the catalogue scores it, so the drawer and the
    # catalogue agree. The per-dimension percentages below are still the
    # real observed numbers — only the headline follows UCS's method.
    score, status = catalogue_score(dims)

    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "fqn": ucs_client.agent_fqn(agent.id, agent.name),
        "score": score,
        "status": status,
        "dims": dims,
        "window_days": QUALITY_WINDOW_DAYS,
        "computed_at": datetime.utcnow().isoformat(),
    }


def publish_quality(session: Session, agent: Agent) -> dict:
    """Send this agent's measured quality to UCS, one result per dimension.

    Fail-soft: the measurement is already recorded here, and a catalogue
    that is down must not turn a successful measurement into an error.
    """
    q = agent_quality(session, agent)
    sent, failed = [], []
    for name, dim in q["dims"].items():
        if dim["verdict"] == "unknown":
            # Nothing measured, so there is nothing to report. Pushing a
            # placeholder would put a verdict in the catalogue that no
            # data supports.
            continue
        result = ucs_client.push_quality_result(
            fqn=q["fqn"], name=f"agent:{name}",
            dimension=UCS_DIMENSION.get(name, "custom"),
            status=dim["verdict"], observed=dim["observed"],
            threshold=dim["threshold"], rows_scanned=dim["rows_scanned"],
            detail=dim["detail"],
        )
        (sent if result.get("ok") else failed).append(name)
    return {"fqn": q["fqn"], "sent": sent, "failed": failed, "quality": q}


def publish_lineage(session: Session, agent: Agent, run_id: str, state: str) -> dict:
    """Report one run's reads to UCS as an OpenLineage event."""
    lineage = agent_lineage(session, agent)
    inputs = [u["fqn"] for u in lineage["upstream"]]
    if not inputs:
        # An agent that reads nothing governed produces no edge; an event
        # with no inputs would add a run record and no lineage.
        return {"ok": True, "skipped": "no governed inputs"}
    return ucs_client.emit_lineage_event(
        job_fqn=lineage["fqn"], run_id=run_id, state=state,
        inputs=inputs, outputs=[lineage["fqn"]])
