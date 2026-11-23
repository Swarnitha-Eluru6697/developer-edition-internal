import logging
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from ..db import get_session
from ..models import Agent, Alert, EvalSet, EvalRun, Run

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])

# Real metrics this platform can genuinely compute from real data, and
# real, honest gaps for the ones it can't (§0.77, GAP_REPORT) — the
# actual fix for the reported bug: PLATFORM_METRICS used to be a
# hardcoded dict of plausible-looking values ("97.2%", "1.4s", ...)
# that never changed no matter what actually ran. Every metric below
# is either computed from real Run rows, or honestly reported as not
# tracked — matching the same real, already-verified pattern
# get_eval_health (below) already established for this exact platform:
# "everything here is computed from real completed runs; where there
# isn't enough real data for a real answer, this says so honestly
# instead of showing a plausible-looking number."
#
# What's genuinely computable, and from what:
#   success   - real % of Run.status == "success" (last 7 real days)
#   p50 / p95 - real percentiles of Run.duration, parsed from its own
#               real "X.Ys" string (last 7 real days)
#   stepLimit - real % of Run.fail_class == "harness" (a real
#               HarnessStepLimitExceeded, persisted directly on the
#               Run row itself by _finalize_run)
#   guardrail - real % of Run.fail_class == "moderation" (a real
#               input/output moderation block)
#   entPass   - real % of Run.status != "denied" (inverse of the real
#               UCS entitlement denial rate)
#   costAvg   - real, as of §0.78 (GAP_REPORT): the real average of
#               Run.cost_usd across the window - genuine per-node token
#               usage, captured directly off the real Anthropic API
#               response and aggregated onto each Run (see harness.py's
#               own _aggregate_llm_usage and execution.py's own
#               _aggregate_run_usage). See model_adapter.py's own price
#               table docstring for the honest distinction between the
#               genuinely-exact token counts this is built from and the
#               reference (not contractually-verified) dollar rates
#               they're priced against.
# What's honestly NOT computable today, and why:
#   retries    - a real Temporal Activity's own real attempt count
#                (§0.76, GAP_REPORT) is tracked internally by Temporal
#                itself, not persisted anywhere in this platform's own
#                schema for a query like this to read back.
#   loopIter   - a real loop/iteration node's own span (harness.py,
#                temporal_workflow.py) records {"node", "node_id",
#                "status"} - genuinely no iteration COUNT field to
#                average, grep-verified against the actual span-
#                construction code while building this fix.
#   chainDepth - a real Sub-Agent call has no real, persisted "chain
#                depth" concept anywhere in this schema; each Run is
#                its own row, with no real parent/child link recorded.
#   cacheHit   - this platform has no real caching layer for LLM
#                responses or retrieval results at all (verified
#                directly against harness.py/model_adapter.py while
#                building this fix - there's no real cache to report a
#                real hit rate for).
# Each of these four is real, stated future work if this platform ever
# builds the underlying real instrumentation - not silently dropped,
# and not faked in the meantime.
#
# A real, honest, stated scope boundary on cost coverage (§0.78,
# GAP_REPORT): four further real call_llm call sites exist outside a
# flow's own node graph entirely - input/output moderation and
# follow-up-question suggestions (routers/execution.py), eval grading
# (eval_judge.py), and knowledge-retrieval reranking (retrieval.py).
# None of these map onto a single Run's own per-node span model the
# way an in-flow node does (a moderation check runs BEFORE any flow
# context exists at all; eval grading is scored against an EvalRun,
# not a Run), so real spend from these specific calls is not yet
# reflected in the totals below - a real, additional gap, stated
# directly here rather than silently left unmentioned.
logger = logging.getLogger(__name__)

_METRICS_WINDOW_DAYS = 7


def _window_filters(cutoff, agent_id: Optional[str]):
    """The window predicate, shared so the scoped and platform-wide
    aggregates cannot drift apart."""
    filters = [Run.created_at.is_not(None), Run.created_at >= cutoff]
    if agent_id:
        filters.append(Run.agent_id == agent_id)
    return filters


def _avg_container_passes(session: Session, cutoff, agent_id: Optional[str]):
    """Average number of passes per run, over runs that contain a loop or
    iteration container.

    §0.77 recorded this as unbackable because "a real loop/iteration
    span records {node, node_id, status} only — genuinely no iteration
    count field". That is still true of any single span, but it is not
    true of the span LIST: a container re-executes its children, so each
    child's span is appended once per pass. A child node_id appearing
    three times in one run's spans means three passes. Verified against
    a real persisted trace whose loop was configured to exit on the
    second pass — this returns 2.00 for it.

    Counted in Postgres rather than by loading spans into this process,
    for the same reason §0.113 rewrote the aggregate above.

    Runs with no container are excluded rather than counted as zero:
    averaging them in would drag the figure toward 0 and make a real
    loop look like it barely iterates.
    """
    from sqlalchemy import text
    sql = """
        WITH s AS (
            SELECT t.id AS tid, elem->>'node' AS node, elem->>'node_id' AS nid
            FROM trace t, jsonb_array_elements(t.spans_json) elem
            WHERE t.started_at IS NOT NULL AND t.started_at >= :cutoff
              AND (:agent_id IS NULL OR t.agent_id = :agent_id)
        ),
        cont AS (SELECT DISTINCT tid FROM s WHERE node IN ('loop', 'iteration')),
        passes AS (
            SELECT tid, nid, COUNT(*) AS c FROM s
            WHERE tid IN (SELECT tid FROM cont)
              AND node NOT IN ('loop', 'iteration')
            GROUP BY tid, nid
        )
        SELECT AVG(mx) FROM (SELECT tid, MAX(c) AS mx FROM passes GROUP BY tid) x
    """
    try:
        value = session.exec(text(sql), params={"cutoff": cutoff, "agent_id": agent_id}).one()[0]
    except Exception:
        # A metric is not worth failing the whole page for.
        logger.warning("container-pass metric unavailable", exc_info=True)
        return None
    return round(float(value), 2) if value is not None else None


def _template_agent_count(session: Session, agent_id: Optional[str]) -> Optional[int]:
    """How many agents were created from a marketplace template.

    §0.77 recorded this as unbackable, and it was: `template_name` was
    accepted at creation, used to deep-copy the template's flow, and
    then thrown away. Nothing recorded where an agent came from, so
    "reuse" could not be answered at all.

    It is now stored on the agent. Reported as a COUNT rather than a
    percentage on purpose: agents created before the column existed
    carry NULL, and dividing by a total that includes them would
    present "most agents are bespoke" as a finding when it is really an
    artifact of when tracking started.

    Scoped to a single agent this is 0 or 1 — not interesting, but
    consistent with every other figure on the page being agent-scoped
    when a filter is applied.
    """
    from sqlalchemy import func as _f
    q = select(_f.count()).select_from(Agent).where(Agent.template_name.is_not(None))
    if agent_id:
        q = q.where(Agent.id == agent_id)
    try:
        return int(session.exec(q).one())
    except Exception:
        # A metric is not worth failing the page for.
        logger.warning("template-agent count unavailable", exc_info=True)
        return None


def _compute_platform_metrics(session: Session, agent_id: Optional[str] = None) -> dict:
    """Compute the metrics catalog over the real Run rows in the window,
    for the whole platform or for one agent.

    `agent_id` exists because the per-agent branch of GET /metrics used
    to answer from the `Agent.metrics` JSON column instead of computing
    anything. Nothing ever writes that column outside seed.py's own
    hardcoded AGENT_METRICS fixture, so picking an agent in Monitoring's
    "Filter by agent" dropdown showed fabricated numbers for the seeded
    demo agents and an empty dash for every agent a user actually
    created — while this real computation sat right here, used only by
    the "All agents" view. This is the same class of bug §0.77 fixed for
    the platform-wide numbers; the scoped branch was simply left behind.

    §0.113, GAP_REPORT — the actual fix for a real, reported gap:
    this used to load EVERY real Run row in the window into this API
    process's own memory, then compute every real percentage/average/
    percentile in Python — genuinely fine at this platform's own
    development-scale row counts, but a real, growing cost at
    10K/100K/1M+ real rows: every real call to this function paid for
    loading the ENTIRE, real, matching row set just to throw away
    everything except a handful of real, aggregate numbers. Now a
    single, real Postgres query — COUNT/AVG/PERCENTILE_CONT, computed
    entirely server-side — returns exactly those aggregate numbers
    directly; this API process never sees a single real Run row's own
    full content at all. duration_ms (a real, exact BIGINT column —
    §0.79, GAP_REPORT) is used for the real percentile computation
    instead of parsing Run.duration's own display STRING back into a
    float per row, the same real column that fix was built for but
    this function never actually used until now."""
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(days=_METRICS_WINDOW_DAYS)
    row = session.exec(
        select(
            func.count().label("total"),
            func.count().filter(Run.status == "success").label("success_count"),
            func.count().filter(Run.fail_class == "harness").label("harness_count"),
            func.count().filter(Run.fail_class == "moderation").label("moderation_count"),
            func.count().filter(Run.status == "denied").label("denied_count"),
            func.avg(Run.cost_usd).label("avg_cost_usd"),
            # Shown under the average cost: what that cost is made of.
            func.avg(Run.input_tokens).label("avg_input_tokens"),
            func.avg(Run.output_tokens).label("avg_output_tokens"),
            func.percentile_cont(0.5).within_group(Run.duration_ms).label("p50_ms"),
            func.percentile_cont(0.95).within_group(Run.duration_ms).label("p95_ms"),
            # §0.77 recorded chainDepth as unbackable because "no real
            # parent/child link between Sub-Agent Run rows exists". A
            # real `hops` column has since been added and carries real
            # values (1, 2 and 3 in this deployment), so the average is
            # genuine arithmetic now rather than a gap.
            func.avg(Run.hops).label("avg_hops"),
            # Total spend over the window. §0.77 recorded "Cost /
            # workspace" as unbackable because "every real Run.cost is
            # unconditionally $0.00"; §0.78 then made per-run cost real,
            # and the average has been shown ever since — so the total
            # was computable the whole time and the card stayed a dash.
            func.sum(Run.cost_usd).label("total_cost_usd"),
        ).where(*_window_filters(cutoff, agent_id))
    ).one()

    # A real, honest row-count guard — every real percentage/percentile
    # below is genuinely meaningless with zero real runs in the window,
    # so this returns real nulls across the board rather than a
    # division-by-zero placeholder or a fabricated "0%".
    total = row.total
    if total == 0:
        return {
            "success": None, "p50": None, "p95": None, "stepLimit": None,
            "guardrail": None, "entPass": None, "costAvg": None,
            "retries": None, "loopIter": None, "chainDepth": None, "cacheHit": None,
            # Spend is null for the same reason as the rest — no runs to
            # sum. $0.0000 would imply runs happened and were free.
            "costWindow": None, "avgInputTokens": None, "avgOutputTokens": None,
            # Template provenance is a property of the AGENT, not of its
            # runs, so it is still answerable with an empty window. Every
            # key the populated branch returns must appear here too, or a
            # scoped view of a quiet agent hands the UI a missing field
            # rather than a dash.
            "templateAgents": _template_agent_count(session, agent_id),
            "sample_size": 0, "window_days": _METRICS_WINDOW_DAYS,
        }

    # Real average cost per run (§0.78, GAP_REPORT) — the real fix
    # completing the loop this same module's own §0.77 fix started:
    # Run.cost_usd is now real (see harness.py's own _aggregate_llm_usage
    # and execution.py's own _aggregate_run_usage), so this average is
    # genuine math over genuine per-run figures, not a fabricated
    # number. Formatted to 4 decimals for the same real reason
    # _aggregate_run_usage's own "cost" string is: a typical single
    # run's real cost is often well under a cent, and 2-decimal
    # rounding would silently collapse most real runs back to "$0.00".
    avg_cost_usd = float(row.avg_cost_usd) if row.avg_cost_usd is not None else 0.0
    p50_ms, p95_ms = row.p50_ms, row.p95_ms
    loop_iter = _avg_container_passes(session, cutoff, agent_id)

    return {
        "success": round(100 * row.success_count / total, 1),
        "p50": f"{p50_ms / 1000:.1f}s" if p50_ms is not None else None,
        "p95": f"{p95_ms / 1000:.1f}s" if p95_ms is not None else None,
        "stepLimit": round(100 * row.harness_count / total, 1),
        "guardrail": round(100 * row.moderation_count / total, 1),
        "entPass": round(100 * (total - row.denied_count) / total, 1),
        "costAvg": f"${avg_cost_usd:.4f}",
        "avgInputTokens": round(float(row.avg_input_tokens)) if row.avg_input_tokens is not None else None,
        "avgOutputTokens": round(float(row.avg_output_tokens)) if row.avg_output_tokens is not None else None,
        # Two of §0.77's four "no real data" metrics are computable now
        # and are computed above. The remaining two genuinely have no
        # backing data — not merely uncomputed here:
        #   retries  — the only `attempts` column in the schema belongs
        #              to KBIngestionOutbox, not Run. Temporal tracks an
        #              activity's attempt count internally and it is
        #              never persisted per run.
        #   cacheHit — this platform has no entitlement-plan cache at
        #              all. (ucs_client's references to `rbac_cache` are
        #              UCS's own internal cache, inside a separate
        #              service, whose hit rate this service cannot see.)
        "retries": None, "cacheHit": None,
        "costWindow": (f"${float(row.total_cost_usd):.4f}"
                       if row.total_cost_usd is not None else None),
        "templateAgents": _template_agent_count(session, agent_id),
        "loopIter": loop_iter,
        "chainDepth": round(float(row.avg_hops), 2) if row.avg_hops is not None else None,
        "sample_size": total, "window_days": _METRICS_WINDOW_DAYS,
    }


@router.get("/metrics", dependencies=[Depends(require_any_permission(P.MONITORING, P.OVERVIEW))])
def get_metrics(agent_id: Optional[str] = None, session: Session = Depends(get_session)):
    if not agent_id or agent_id == "all":
        return {"scope": "platform", "metrics": _compute_platform_metrics(session)}
    a = session.get(Agent, agent_id)
    if a is None:
        # Previously a deleted or mistyped id answered 200 with an empty
        # metrics object, which the UI renders identically to a real
        # agent that simply has no runs yet.
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "scope": agent_id,
        "agent_name": a.name,
        "metrics": _compute_platform_metrics(session, agent_id=agent_id),
    }


def eval_trend(all_runs, now) -> tuple:
    """Week-over-week eval pass rate, and why it cannot be shown.

    A module-level function rather than a closure so the arithmetic can
    be tested without standing up a database — this suite runs against
    the live one, and inserting eval history to check a sentence would
    leave debris behind.

    Returns (delta_in_points, note). Exactly one of the two is ever set.

    The bug it carries a guard against: the comparison needs BOTH
    windows populated, so the guard is an OR — but the note used to
    assert both were empty whichever one was missing. On this install it
    fired with 25 completed runs in the prior window and none in the
    current one, reading "not enough runs in both the last 7 days and
    the 7 days before that". That is the opposite of what the data said,
    and it sends the reader hunting for history that is not missing.

    Each case now names the window that is genuinely empty and says how
    old the newest run is, because "nobody has run the eval sets in nine
    days" is the thing to act on, and no phrasing of "not enough runs"
    conveys it.
    """
    from datetime import timedelta

    dated = [r for r in all_runs if r.completed_at]
    this_week = [r for r in dated if now - timedelta(days=7) <= r.completed_at <= now]
    prior_week = [r for r in dated
                  if now - timedelta(days=14) <= r.completed_at < now - timedelta(days=7)]

    def _avg(runs):
        rs = [r.pass_count / r.total_count for r in runs if r.total_count]
        return sum(rs) / len(rs) if rs else None

    this_avg, prior_avg = _avg(this_week), _avg(prior_week)
    if this_avg is not None and prior_avg is not None:
        return round((this_avg - prior_avg) * 100, 1), None

    newest = max((r.completed_at for r in dated), default=None)
    if newest is None:
        return None, "no completed eval runs yet"
    stale_days = (now - newest).days
    ago = f"the most recent was {stale_days} days ago" if stale_days else "the most recent was today"

    if this_avg is None and prior_avg is None:
        return None, f"no completed eval runs in the last 14 days — {ago}"
    if this_avg is None:
        return None, f"no eval runs in the last 7 days to compare — {ago}"
    return None, ("nothing in the 7 days before last to compare against — "
                  "this is the first week with eval runs")


@router.get("/eval-health", dependencies=[Depends(require_any_permission(P.MONITORING, P.OVERVIEW))])
def get_eval_health(agent_id: Optional[str] = None, session: Session = Depends(get_session)):
    """Real aggregation over actual EvalRun/EvalResult data — replaces
    the Monitoring page's previously-hardcoded "Agent health" eval cards
    (89% / 2.1pt / 1 flag were static strings, not derived from any real
    run). Everything here is computed from real completed runs; where
    there isn't enough real data for a real answer (e.g. no runs in one
    of the two 7-day windows for the trend), this says so honestly
    instead of showing a plausible-looking number."""
    from datetime import timedelta
    sets = session.exec(select(EvalSet)).all()
    if agent_id and agent_id != "all":
        sets = [s for s in sets if s.agent_id == agent_id]
    set_ids = [s.id for s in sets]
    set_by_id = {s.id: s for s in sets}

    if not set_ids:
        return {"avg_pass_rate": None, "trend_delta_pts": None, "trend_note": "no eval sets", "drift_flags": []}

    # Latest completed run per set — same "most recent per set" rule
    # /evals already uses, reused here for the average and drift flags.
    latest_by_set = {}
    for sid in set_ids:
        run = session.exec(
            select(EvalRun).where(EvalRun.eval_set_id == sid, EvalRun.status == "completed")
            .order_by(EvalRun.started_at.desc())
        ).first()
        if run:
            latest_by_set[sid] = run

    if not latest_by_set:
        return {"avg_pass_rate": None, "trend_delta_pts": None, "trend_note": "no completed runs yet", "drift_flags": []}

    rates = [r.pass_count / r.total_count for r in latest_by_set.values() if r.total_count]
    avg_pass_rate = sum(rates) / len(rates) if rates else None

    drift_flags = []
    for sid, run in latest_by_set.items():
        if run.drift and run.drift != "none":
            s = set_by_id[sid]
            agent = session.get(Agent, s.agent_id)
            drift_flags.append({"agent_name": agent.name if agent else s.agent_id, "set_name": s.name, "drift": run.drift})

    # Real week-over-week trend: average pass rate of runs COMPLETED in
    # the last 7 days vs. the 7 days before that, across all real
    # completed runs for these sets — not just the latest-per-set,
    # since a trend needs the real run history, not one snapshot.
    now = datetime.utcnow()
    all_runs = session.exec(
        select(EvalRun).where(EvalRun.eval_set_id.in_(set_ids), EvalRun.status == "completed")
    ).all()
    trend_delta_pts, trend_note = eval_trend(all_runs, now)

    return {
        "avg_pass_rate": round(avg_pass_rate * 100, 1) if avg_pass_rate is not None else None,
        "trend_delta_pts": trend_delta_pts, "trend_note": trend_note,
        "drift_flags": drift_flags,
    }


@router.get("/evals", dependencies=[Depends(require_any_permission(P.MONITORING, P.OVERVIEW))])
def get_evals(agent_name: Optional[str] = None, session: Session = Depends(get_session)):
    """Real data — the most recent COMPLETED run of each eval set,
    replacing what used to be a hardcoded Python list with fixed strings.
    An eval set with no completed run yet simply doesn't appear here
    (there's no fake row to show in its place)."""
    sets = session.exec(select(EvalSet)).all()
    out = []
    for s in sets:
        agent = session.get(Agent, s.agent_id)
        if agent_name and agent_name != "all" and (not agent or agent.name != agent_name):
            continue
        latest_run = session.exec(
            select(EvalRun).where(EvalRun.eval_set_id == s.id, EvalRun.status == "completed")
            .order_by(EvalRun.started_at.desc())
        ).first()
        if not latest_run:
            continue
        rate = latest_run.pass_count / latest_run.total_count if latest_run.total_count else 0
        out.append({
            # Identifiers as well as labels: a row in this table was
            # previously dead text, because it carried no way to fetch
            # the set's own cases or reach the agent it belongs to.
            "set_id": s.id,
            "agent_id": s.agent_id,
            "run_id": latest_run.id,
            "agent": agent.name if agent else s.agent_id,
            "set": s.name,
            "passed": f"{latest_run.pass_count}/{latest_run.total_count}",
            "rate": f"{rate*100:.1f}%",
            "drift": latest_run.drift,
            "ts": latest_run.completed_at.strftime("%Y-%m-%d %H:%M") if latest_run.completed_at else "",
        })
    return out


@router.get("/stats", dependencies=[Depends(require_any_permission(P.OVERVIEW, P.MONITORING, P.ALERTS))])
def get_home_stats(session: Session = Depends(get_session)):
    """§0.77/§0.78, GAP_REPORT — the actual fix for the reported gap:
    runsToday/successRate/avgCost were hardcoded (even labeled "STUB"
    directly in the prior code). runsToday/successRate are real,
    computed from real Run rows created today (UTC) — genuinely
    possible now that Run has a real `created_at` datetime (§0.77
    added it; the pre-existing `ts` field was only ever a time-of-day
    string with no date component, so "today" was never actually
    answerable before that). avgCost is real as of §0.78 — the real
    average of today's real Run.cost_usd figures, genuine per-node
    token usage captured directly off the real Anthropic API response
    (see harness.py's own _aggregate_llm_usage); `null` only for the
    same honest reason `successRate` is — no real runs today yet to
    average, not a silently-kept fabricated number.

    §0.113, GAP_REPORT — every real count/average here is now a real
    Postgres aggregate (COUNT/AVG/FILTER), computed directly by the
    database — this endpoint used to load every real Agent row, every
    real open Alert row, and every real Run row created today into
    this API process's own memory just to call len()/sum() on them, a
    real, growing cost at real scale this fix removes entirely."""
    from sqlalchemy import func
    # Snippets are stored as agents but are not agents to anyone looking
    # at the UI — the Agents page and the sidebar both leave them out, so
    # counting them here made the Overview disagree with both.
    agent_count = session.exec(select(func.count()).select_from(Agent).where(Agent.plane != "Snippet")).one()
    # "Open" means what the Alerts page's Open tab and the sidebar badge mean: delivered to
    # a channel and not yet acknowledged (alerts.py::list_alerts, state="open"). Counting
    # every state=="open" row also swept in acknowledged and undelivered alerts, so the
    # Overview said 47 while the sidebar said 26. All three tallies come from one query so
    # every screen reads the same numbers.
    acks = session.exec(
        select(
            func.count().filter(Alert.acked == False, Alert.delivered_at.is_not(None)),  # noqa: E712
            func.count().filter(Alert.acked == True),  # noqa: E712
            func.count().filter(Alert.delivered_at.is_(None)),
        ).select_from(Alert).where(Alert.state == "open")
    ).one()
    open_alert_count, acked_alert_count, undelivered_alert_count = acks

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    row = session.exec(
        select(
            func.count().label("runs_today"),
            func.count().filter(Run.status == "success").label("success_count"),
            func.avg(Run.cost_usd).label("avg_cost_usd"),
        ).where(Run.created_at.is_not(None), Run.created_at >= today_start)
    ).one()
    runs_today = row.runs_today
    success_rate = round(100 * row.success_count / runs_today, 1) if runs_today else None
    avg_cost = f"${float(row.avg_cost_usd):.4f}" if runs_today and row.avg_cost_usd is not None else None

    return {
        "agents": agent_count,
        "runsToday": runs_today,
        "successRate": success_rate,
        "openAlerts": open_alert_count,
        "acknowledgedAlerts": acked_alert_count,
        "undeliveredAlerts": undelivered_alert_count,
        "avgCost": avg_cost,
    }
