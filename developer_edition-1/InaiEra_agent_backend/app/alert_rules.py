"""Raise alerts from what actually happened, not from seed data.

Until now every row on the Alerts page came from `seed.py`: four fixed
alerts describing events that never occurred, with timestamps from
August. Nothing in the platform ever raised one, so the page was a
mock-up and the Slack/Teams channels had nothing to carry.

This module evaluates real conditions against real rows — `Run.status`,
`Run.fail_class`, `Run.duration_ms`, `Run.cost_usd` and `EvalRun`
pass counts — and raises an `Alert` when one is genuinely met.

Two properties matter more than the rules themselves:

  * **Deduplication.** A rule evaluated every five minutes must not
    produce 288 identical alerts a day. While an alert for the same
    (rule, agent) is still open, the condition is considered already
    reported. Without this the channels become unusable within an hour
    and people mute them, which is worse than having no alerting.

  * **Thresholds that mean something.** Every rule needs a minimum
    volume before it fires. "100% failure rate" over two runs is noise;
    over eighty runs it is an outage. A rule that fires on thin data
    trains people to ignore it.

Rules deliberately reuse the names already on the Alerts page
(`denied-burst`, `quality-breach`, `harness — step-limit`) so the real
alerts are continuous with what the UI already showed rather than a
second, parallel vocabulary.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, select

from .models import Agent, Alert, AlertRule

logger = logging.getLogger(__name__)


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# How far back a rule looks, and the baseline it compares against.
WINDOW_HOURS = _num("ALERT_WINDOW_HOURS", 24)
BASELINE_DAYS = _num("ALERT_BASELINE_DAYS", 7)

# Minimum volume before a rate-based rule is allowed to fire at all.
MIN_RUNS = int(_num("ALERT_MIN_RUNS", 20))

ERROR_RATE_PCT = _num("ALERT_ERROR_RATE_PCT", 25)
DENIED_BURST = int(_num("ALERT_DENIED_BURST", 3))
GUARDRAIL_BLOCKS = int(_num("ALERT_GUARDRAIL_BLOCKS", 3))
LATENCY_FACTOR = _num("ALERT_LATENCY_FACTOR", 3)
LATENCY_FLOOR_MS = _num("ALERT_LATENCY_FLOOR_MS", 10_000)
COST_FACTOR = _num("ALERT_COST_FACTOR", 3)
COST_FLOOR_USD = _num("ALERT_COST_FLOOR_USD", 0.50)
EVAL_PASS_PCT = _num("ALERT_EVAL_PASS_PCT", 70)

# How many previously-undelivered alerts one sweep may retry.
#
# Deliberately small. Without a cap, the first sweep after a channel is
# added posts the entire backlog at once — 17 open alerts across two
# channels sent 30 messages in one go, which is exactly the flood the
# dedup logic exists to prevent. Trickling them keeps the retry useful
# without letting a backlog arrive as a wall.
RETRY_MAX_PER_SWEEP = int(_num("ALERT_RETRY_MAX_PER_SWEEP", 3))


@dataclass
class Candidate:
    """A condition a rule found to be true, before dedup."""
    sev: str
    rule: str
    agent_id: str
    agent_name: str
    summary: str


# The window a rule uses when its own is left at 0 — "use the kind's
# default", so a rule can be created without knowing a sensible number.
DEFAULT_WINDOW_SECONDS = int(WINDOW_HOURS * 3600)


def rule_window(rule) -> timedelta:
    return timedelta(seconds=rule.window_seconds or DEFAULT_WINDOW_SECONDS)


def _window(now: datetime) -> datetime:
    return now - timedelta(hours=WINDOW_HOURS)


def _cutoff(rule, now: datetime) -> datetime:
    return now - rule_window(rule)


def _window_label(rule) -> str:
    """How the window reads in an alert summary."""
    secs = rule.window_seconds or DEFAULT_WINDOW_SECONDS
    if secs % 86400 == 0 and secs >= 86400:
        n = secs // 86400
        return f"{n} day" + ("s" if n > 1 else "")
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


def _scope(rule):
    """The agent filter for a rule, or None for every agent."""
    return rule.agent_id or None


def _fail_class_clause(rule, alias: str = "r") -> str:
    """Optional filter on which failure classes count.

    The analogue of the catalog's "dimensions": empty means any. Built as
    a fragment rather than a bound list because it is either present or
    absent from the SQL entirely.
    """
    return f" AND {alias}.fail_class = ANY(:fail_classes)" if rule.fail_classes else ""


def _hours(n: float) -> str:
    """'24h' reads better than '24.0 hours' in an alert summary."""
    return f"{int(n)}h" if float(n).is_integer() else f"{n}h"


# --- rules ---------------------------------------------------------------
#
# Each takes (session, now) and returns the conditions it found true.
# They are plain functions over real SQL so each can be tested on its
# own against a known set of rows.


def _dominant_cause(session: Session, agent_id: str, cutoff: datetime) -> Optional[str]:
    """The most common failure message for this agent in the window.

    "67% of runs failed" tells an operator there is a problem; it does
    not tell them it is a missing API key. The cause is what makes the
    alert actionable, and it is already sitting in the trace spans.
    """
    row = session.exec(text("""
        SELECT substring(spans_json::text from '"error": "([^"]{1,200})') AS cause,
               COUNT(*) AS n
        FROM trace
        WHERE agent_id = :agent_id AND started_at >= :cutoff AND status <> 'ok'
        GROUP BY 1
        HAVING substring(spans_json::text from '"error": "([^"]{1,200})') IS NOT NULL
        ORDER BY n DESC
        LIMIT 1
    """), params={"agent_id": agent_id, "cutoff": cutoff}).first()
    if not row or not row[0]:
        return None
    # These arrive as escaped JSON text; a literal \n in an alert summary
    # would break the Slack block rendering.
    cause = " ".join(row[0].replace("\\n", " ").split())
    if len(cause) <= 130:
        return cause
    # Cut at a word boundary — a message ending "the rea" reads as
    # corruption rather than as truncation.
    return cause[:130].rsplit(" ", 1)[0] + "…"


def error_rate(session: Session, now: datetime, rule) -> List[Candidate]:
    """An agent failing an unusual share of its runs.

    Counts `failed` only. `denied` and `blocked` are deliberate refusals
    — UCS said no, or moderation did — and have their own kinds; folding
    them in here would report a working guardrail as an outage.
    """
    rows = session.exec(text(f"""
        SELECT r.agent_id, a.name,
               COUNT(*) AS runs,
               COUNT(*) FILTER (WHERE r.status = 'failed'{_fail_class_clause(rule)}) AS failed
        FROM run r JOIN agent a ON a.id = r.agent_id
        WHERE r.created_at >= :cutoff
          AND (:agent_id IS NULL OR r.agent_id = :agent_id)
        GROUP BY r.agent_id, a.name
        HAVING COUNT(*) >= :min_runs
           AND 100.0 * COUNT(*) FILTER (WHERE r.status = 'failed') / COUNT(*) >= :pct
    """), params={"cutoff": _cutoff(rule, now), "min_runs": rule.min_runs,
                  "pct": rule.threshold, "agent_id": _scope(rule),
                  "fail_classes": rule.fail_classes or []}).all()

    cutoff = _cutoff(rule, now)
    out = []
    for agent_id, name, runs, failed in rows:
        pct = 100.0 * failed / runs
        cause = _dominant_cause(session, agent_id, cutoff)
        summary = (f"{failed} of {runs} runs failed in the last {_window_label(rule)} "
                   f"({pct:.0f}%) — above the {rule.threshold:.0f}% threshold.")
        if cause:
            summary += f" Most common cause: {cause}"
        out.append(Candidate(
            # Half the runs failing is a different conversation from a
            # quarter, so a rule set to 'high' still reports a merely
            # borderline breach as medium.
            sev=rule.severity if pct >= 2 * rule.threshold else _step_down(rule.severity),
            rule=rule.name, agent_id=agent_id, agent_name=name, summary=summary,
        ))
    return out


def denied_burst(session: Session, now: datetime, rule) -> List[Candidate]:
    """Repeated authorization denials for one agent.

    A burst usually means a misconfigured sub-agent or a revoked
    entitlement rather than an attack, but either way someone has to look.
    """
    rows = session.exec(text("""
        SELECT r.agent_id, a.name, COUNT(*) AS denied
        FROM run r JOIN agent a ON a.id = r.agent_id
        WHERE r.created_at >= :cutoff AND r.status = 'denied'
          AND (:agent_id IS NULL OR r.agent_id = :agent_id)
        GROUP BY r.agent_id, a.name
        HAVING COUNT(*) >= :threshold
    """), params={"cutoff": _cutoff(rule, now), "threshold": rule.threshold,
                  "agent_id": _scope(rule)}).all()

    return [Candidate(
        sev=rule.severity, rule=rule.name, agent_id=agent_id, agent_name=name,
        summary=(f"{denied} runs denied by authorization in the last "
                 f"{_window_label(rule)} — check the agent's entitlements "
                 "and any sub-agent it calls."),
    ) for agent_id, name, denied in rows]


def guardrail_blocks(session: Session, now: datetime, rule) -> List[Candidate]:
    """Moderation blocking an unusual number of messages.

    Worth surfacing in both directions: a spike can mean the agent is
    being probed, or that a moderation rule is too broad and is refusing
    legitimate questions. The summary says so rather than assuming.
    """
    rows = session.exec(text("""
        SELECT r.agent_id, a.name, COUNT(*) AS blocked
        FROM run r JOIN agent a ON a.id = r.agent_id
        WHERE r.created_at >= :cutoff
          AND r.status = 'blocked' AND r.fail_class = 'moderation'
          AND (:agent_id IS NULL OR r.agent_id = :agent_id)
        GROUP BY r.agent_id, a.name
        HAVING COUNT(*) >= :threshold
    """), params={"cutoff": _cutoff(rule, now), "threshold": rule.threshold,
                  "agent_id": _scope(rule)}).all()

    return [Candidate(
        sev=rule.severity, rule=rule.name, agent_id=agent_id, agent_name=name,
        summary=(f"Moderation blocked {blocked} messages in the last "
                 f"{_window_label(rule)} — either the agent is being probed, "
                 "or a moderation rule is refusing legitimate questions."),
    ) for agent_id, name, blocked in rows]


def step_limit(session: Session, now: datetime, rule) -> List[Candidate]:
    """A reasoning loop hitting its step cap."""
    rows = session.exec(text("""
        SELECT r.agent_id, a.name, COUNT(*) AS hit
        FROM run r JOIN agent a ON a.id = r.agent_id
        WHERE r.created_at >= :cutoff AND r.fail_class = 'harness'
          AND (:agent_id IS NULL OR r.agent_id = :agent_id)
        GROUP BY r.agent_id, a.name
        HAVING COUNT(*) >= :threshold
    """), params={"cutoff": _cutoff(rule, now), "threshold": rule.threshold,
                  "agent_id": _scope(rule)}).all()

    return [Candidate(
        sev=rule.severity, rule=rule.name, agent_id=agent_id, agent_name=name,
        summary=(f"{hit} run(s) hit the harness step cap in the last "
                 f"{_window_label(rule)} before producing a final answer."),
    ) for agent_id, name, hit in rows]


def latency_spike(session: Session, now: datetime, rule) -> List[Candidate]:
    """p95 latency well above the agent's own recent baseline.

    Compared against the agent's own history rather than a fixed number,
    because a research agent that always takes 40s is not broken while a
    lookup that suddenly takes 12s is. The absolute floor stops a jump
    between two trivially small numbers from firing.
    """
    rows = session.exec(text("""
        WITH recent AS (
            SELECT agent_id,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                   COUNT(*) AS runs
            FROM run
            WHERE created_at >= :cutoff AND duration_ms IS NOT NULL
            GROUP BY agent_id
        ),
        baseline AS (
            SELECT agent_id,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                   COUNT(*) AS runs
            FROM run
            WHERE created_at >= :baseline_start AND created_at < :cutoff
              AND duration_ms IS NOT NULL
            GROUP BY agent_id
        )
        SELECT r.agent_id, a.name, r.p95, b.p95
        FROM recent r
        JOIN baseline b ON b.agent_id = r.agent_id
        JOIN agent a ON a.id = r.agent_id
        WHERE r.runs >= :min_runs AND b.runs >= :min_runs
          AND b.p95 > 0
          AND r.p95 >= b.p95 * :factor
          AND r.p95 >= :floor_ms
          AND (:agent_id IS NULL OR r.agent_id = :agent_id)
    """), params={
        "cutoff": _cutoff(rule, now),
        "baseline_start": now - timedelta(days=BASELINE_DAYS),
        "min_runs": rule.min_runs, "factor": rule.threshold,
        "floor_ms": LATENCY_FLOOR_MS, "agent_id": _scope(rule),
    }).all()

    return [Candidate(
        sev=rule.severity, rule=rule.name, agent_id=agent_id, agent_name=name,
        summary=(f"p95 latency is {float(p95)/1000:.1f}s over the last "
                 f"{_window_label(rule)}, against {float(base)/1000:.1f}s "
                 f"for the preceding {int(BASELINE_DAYS)} days "
                 f"({float(p95)/float(base):.1f}× slower)."),
    ) for agent_id, name, p95, base in rows]


def cost_spike(session: Session, now: datetime, rule) -> List[Candidate]:
    """Spend well above the agent's own recent daily rate.

    The baseline is normalised to the same window length, so a 24h window
    is compared against an average day rather than a whole week's total.
    """
    window_days = rule_window(rule).total_seconds() / 86400.0
    rows = session.exec(text("""
        WITH recent AS (
            SELECT agent_id, COALESCE(SUM(cost_usd), 0) AS spend
            FROM run WHERE created_at >= :cutoff GROUP BY agent_id
        ),
        baseline AS (
            SELECT agent_id, COALESCE(SUM(cost_usd), 0) / :baseline_days AS per_day
            FROM run
            WHERE created_at >= :baseline_start AND created_at < :cutoff
            GROUP BY agent_id
        )
        SELECT r.agent_id, a.name, r.spend, b.per_day * :window_days AS expected
        FROM recent r
        JOIN baseline b ON b.agent_id = r.agent_id
        JOIN agent a ON a.id = r.agent_id
        WHERE b.per_day > 0
          AND r.spend >= b.per_day * :window_days * :factor
          AND r.spend >= :floor_usd
          AND (:agent_id IS NULL OR r.agent_id = :agent_id)
    """), params={
        "cutoff": _cutoff(rule, now),
        "baseline_start": now - timedelta(days=BASELINE_DAYS),
        "baseline_days": BASELINE_DAYS, "window_days": window_days,
        "factor": rule.threshold, "floor_usd": COST_FLOOR_USD,
        "agent_id": _scope(rule),
    }).all()

    return [Candidate(
        sev=rule.severity, rule=rule.name, agent_id=agent_id, agent_name=name,
        summary=(f"Spent ${float(spend):.2f} in the last {_window_label(rule)}, "
                 f"against ${float(expected):.2f} expected from the preceding "
                 f"{int(BASELINE_DAYS)} days ({float(spend)/float(expected):.1f}×)."),
    ) for agent_id, name, spend, expected in rows]


def quality_breach(session: Session, now: datetime, rule) -> List[Candidate]:
    """The agent's most recent eval run came in below the pass bar.

    Only the latest completed run per agent is considered. An eval run
    from a month ago says nothing about the agent as it stands now, and
    alerting on historic runs would re-report the same old failure
    forever.
    """
    rows = session.exec(text("""
        SELECT DISTINCT ON (er.agent_id)
               er.agent_id, a.name, er.pass_count, er.total_count,
               es.name AS set_name, er.started_at
        FROM evalrun er
        JOIN agent a ON a.id = er.agent_id
        JOIN evalset es ON es.id = er.eval_set_id
        WHERE er.total_count > 0
          AND (:agent_id IS NULL OR er.agent_id = :agent_id)
        ORDER BY er.agent_id, er.started_at DESC
    """), params={"agent_id": _scope(rule)}).all()

    out = []
    for agent_id, name, passed, total, set_name, started in rows:
        pct = 100.0 * passed / total
        if pct >= rule.threshold:
            continue
        out.append(Candidate(
            sev=rule.severity if pct < rule.threshold / 2 else _step_down(rule.severity),
            rule=rule.name, agent_id=agent_id, agent_name=name,
            summary=(f"Eval set '{set_name}' passed {passed}/{total} "
                     f"({pct:.0f}%) on {started:%Y-%m-%d %H:%M} — below the "
                     f"{rule.threshold:.0f}% bar."),
        ))
    return out


def _step_down(sev: str) -> str:
    """One severity below, for a breach that is real but not extreme.

    A rule set to 'high' should still distinguish 26% failing from 80%
    failing; reporting both as high is how a severity floor on a channel
    stops meaning anything.
    """
    return {"high": "medium", "medium": "low"}.get(sev, sev)


KIND_HANDLERS = {
    "error_rate": error_rate,
    "denied_burst": denied_burst,
    "guardrail_block": guardrail_blocks,
    "step_limit": step_limit,
    "latency_spike": latency_spike,
    "cost_spike": cost_spike,
    "quality_breach": quality_breach,
}

# --- evaluation ----------------------------------------------------------


def active_rules(session: Session) -> List[AlertRule]:
    return session.exec(
        select(AlertRule).where(AlertRule.enabled == True)  # noqa: E712
        .order_by(AlertRule.id)).all()


def evaluate(session: Session, now: Optional[datetime] = None) -> List[Candidate]:
    """Every condition currently true, before dedup.

    Rules come from the database, so what is evaluated is what someone
    configured — not what was compiled in. A rule that raises is logged
    and skipped rather than aborting the sweep: one bad rule must not
    stop the others from reporting.
    """
    now = now or datetime.utcnow()
    found: List[Candidate] = []
    for rule in active_rules(session):
        handler = KIND_HANDLERS.get(rule.kind)
        if not handler:
            # Guarded by a CHECK constraint, so this means the constraint
            # and the handler table have drifted — worth a log rather
            # than a silent skip.
            logger.error("Alert rule %r has unknown kind %r", rule.name, rule.kind)
            continue
        try:
            found.extend(handler(session, now, rule))
        except Exception:
            logger.exception("Alert rule %r failed to evaluate", rule.name)
    return found


def already_open(session: Session, candidate: Candidate) -> bool:
    """Whether this condition is already reported and unresolved.

    Keyed on (rule, agent) rather than on the summary text, because the
    numbers in a summary change every sweep while the condition is the
    same one. Matching on text would defeat the dedup entirely.
    """
    return session.exec(
        select(Alert).where(
            Alert.rule == candidate.rule,
            Alert.agent_id == candidate.agent_id,
            Alert.state == "open",
        )
    ).first() is not None


def raise_alerts(session: Session, candidates: List[Candidate]) -> List[Alert]:
    """Create an Alert for each condition not already open."""
    created = []
    for c in candidates:
        if already_open(session, c):
            continue
        alert = Alert(
            id=f"al-{uuid4().hex[:10]}", sev=c.sev, rule=c.rule,
            agent_id=c.agent_id, agent_name=c.agent_name,
            state="open", acked=False,
            ts=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            summary=c.summary,
            history=[{"action": "Raised", "by": "alert-rules",
                      "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M")}],
        )
        session.add(alert)
        created.append(alert)
    if created:
        session.commit()
        for a in created:
            session.refresh(a)
    return created


def sweep(session: Session) -> dict:
    """Evaluate every rule, raise what is new, deliver it to the channels.

    Delivery is imported here rather than at module import so this module
    can be exercised without the egress stack, and so a delivery failure
    is clearly separate from a rule failure.
    """
    from . import alert_delivery
    from .models import AlertDestination

    candidates = evaluate(session)
    created = raise_alerts(session, candidates)

    destinations = session.exec(select(AlertDestination)).all()

    # New alerts, plus any still-open alert that never reached a channel.
    #
    # The retry is not an extra: an alert raised while no channel existed
    # — or while one was down — would otherwise never be sent, and since
    # the Open tab shows only delivered alerts it would be permanently
    # invisible. Dedup prevents a second alert being raised for the same
    # condition, so without this there would be no second chance at all.
    undelivered = session.exec(
        select(Alert)
        .where(Alert.state == "open", Alert.delivered_at.is_(None))
        .order_by(Alert.id)
        .limit(RETRY_MAX_PER_SWEEP)
    ).all()
    created_ids = {a.id for a in created}
    to_send = created + [a for a in undelivered if a.id not in created_ids]

    delivered = 0
    for alert in to_send:
        # fan_out never raises: an alert that could not be delivered is
        # still a real alert, and must survive the attempt.
        for outcome in alert_delivery.fan_out(session, alert, destinations):
            if outcome["ok"]:
                delivered += 1

    return {
        "evaluated": str(len(candidates)),
        "raised": str(len(created)),
        "suppressed_as_duplicate": str(len(candidates) - len(created)),
        "retried_undelivered": str(len(to_send) - len(created)),
        "deliveries": str(delivered),
        "destinations": str(len(destinations)),
    }


CLEARED_NOTE = "Condition cleared — still awaiting acknowledgement"


def resolve_stale(session: Session, now: Optional[datetime] = None) -> int:
    """Close alerts whose condition is no longer true — but only ones a
    person has already acknowledged.

    An alert nobody has looked at stays open even after the condition
    clears. Auto-closing it means a problem can appear and disappear
    between two glances at the page, and the person who was paged about
    it finds nothing when they click through from Slack or Teams. The
    alert is the record that something happened; it is not the platform's
    to close on the operator's behalf.

    Acknowledged is the signal that somebody knows. Once it is
    acknowledged, closing it when the condition clears is a convenience
    rather than a loss.

    An unacknowledged alert instead gets a one-time note saying the
    condition has gone, so the page can show it is no longer live without
    removing it. One-time because this runs every sweep, and appending
    the same line every 15 minutes would bury the real history.
    """
    now = now or datetime.utcnow()
    live = {(c.rule, c.agent_id) for c in evaluate(session, now)}
    closed = 0
    noted = 0
    for alert in session.exec(select(Alert).where(Alert.state == "open")).all():
        # Only alerts this engine raised. An alert with NO history at all
        # — a seeded row, or one created before this engine existed — is
        # not ours either.
        if not alert.history or alert.history[0].get("by") != "alert-rules":
            continue
        if (alert.rule, alert.agent_id) in live:
            continue

        if not alert.acked:
            if any(h.get("action") == CLEARED_NOTE for h in alert.history):
                continue
            alert.history = [*alert.history, {
                "action": CLEARED_NOTE, "by": "alert-rules",
                "ts": now.strftime("%Y-%m-%d %H:%M")}]
            session.add(alert)
            noted += 1
            continue

        alert.state = "resolved"
        alert.history = [*alert.history, {
            "action": "Resolved — condition cleared", "by": "alert-rules",
            "ts": now.strftime("%Y-%m-%d %H:%M")}]
        session.add(alert)
        closed += 1
    if closed or noted:
        session.commit()
    return closed
