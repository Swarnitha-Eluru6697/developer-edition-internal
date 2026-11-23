"""Alerts raised from real conditions, not from seed data.

Every row on the Alerts page used to come from seed.py — four fixed
alerts about events that never happened. Nothing raised one, so the page
was a mock-up and the Slack/Teams channels had nothing to carry.

The two properties worth testing are not the rules themselves but the
things that decide whether anyone keeps the channel unmuted:

  * dedup — a rule evaluated every 15 minutes must not post the same
    alert 96 times a day;
  * thresholds — "100% failure" over two runs is noise, and a rule that
    fires on thin data teaches people to ignore it.

These tests run inside a transaction that is rolled back, so they leave
nothing in the database. That matters here specifically: the suite's
other integration tests create agents in the live database, which is
where the several hundred junk "Test"/"Guardrails Test" agents came
from.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, select

from app.db import engine
from app import alert_rules
from app.models import (Agent, Alert, AlertDestination, AlertRule, EvalRun,
                        EvalSet, Run, Trace)


@pytest.fixture
def session():
    """A session whose writes are always rolled back.

    The rules use Postgres-only SQL (percentile_cont, FILTER, regex
    substring), so this runs against the real database rather than
    SQLite — but nothing survives the test.
    """
    conn = engine.connect()
    outer = conn.begin()
    s = Session(bind=conn)
    try:
        yield s
    finally:
        s.close()
        outer.rollback()
        conn.close()


NOW = datetime(2026, 9, 24, 12, 0, 0)


def make_rule(session, **kw):
    """A rule row. Rules are data now, so a test configures one rather
    than monkeypatching a module constant."""
    base = dict(name=f"r-{uuid.uuid4().hex[:8]}", kind="error_rate", severity="high",
                threshold=25, window_seconds=86400, min_runs=20,
                fail_classes=[], agent_id=None, enabled=True)
    base.update(kw)
    r = AlertRule(**base)
    session.add(r)
    session.flush()
    return r


def only(candidates, agent):
    """Just this agent's candidates.

    The rules run against the real database, which has its own genuinely
    failing agents — asserting on the whole result would make these
    tests depend on unrelated production data.
    """
    return [c for c in candidates if c.agent_id == agent.id]


def make_agent(session, name="Rule Fixture") -> Agent:
    a = Agent(id=f"rt{uuid.uuid4().hex[:9]}", name=name, type="App", subtype="Custom",
              plane="App", owner="tests", status="published", version="v1",
              health_score=100, health_status="pass", model="Claude Haiku 4.5",
              mode="fixed", app_type="workflow", runs_7d=0, cost_7d="$0",
              first_party=False, tags=[], description="", metrics={},
              flow={"nodes": [], "edges": []}, features={})
    session.add(a)
    session.flush()
    return a


def add_runs(session, agent, *, n, status="success", when=None,
             duration_ms=1000, cost=0.001):
    when = when or (NOW - timedelta(hours=1))
    for _ in range(n):
        session.add(Run(
            id=f"r-{uuid.uuid4().hex[:12]}", agent_id=agent.id, agent_name=agent.name,
            status=status, hops=1, created_at=when,
            duration_ms=duration_ms, cost_usd=cost))
    session.flush()


# --- thresholds: a rule must not fire on thin data -----------------------

def test_a_high_failure_rate_on_too_few_runs_does_not_fire(session):
    """100% of three runs is noise. Firing here is how an alerting
    system teaches people to ignore it."""
    a = make_agent(session)
    add_runs(session, a, n=3, status="failed")
    assert only(alert_rules.error_rate(session, NOW, make_rule(session)), a) == []


def test_a_high_failure_rate_on_enough_runs_fires(session):
    a = make_agent(session)
    add_runs(session, a, n=30, status="failed")
    add_runs(session, a, n=10, status="success")
    found = only(alert_rules.error_rate(session, NOW, make_rule(session)), a)
    assert len(found) == 1
    assert "30 of 40" in found[0].summary


def test_a_healthy_agent_does_not_fire(session):
    """A rule that flags everything is as useless as one that flags
    nothing."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="success")
    assert only(alert_rules.error_rate(session, NOW, make_rule(session)), a) == []


def test_severity_scales_with_how_bad_it_is(session):
    """Severity is what routes an alert to an on-call channel, so it has
    to mean something."""
    a = make_agent(session)
    add_runs(session, a, n=12, status="failed")
    add_runs(session, a, n=28, status="success")  # 30%
    assert only(alert_rules.error_rate(session, NOW, make_rule(session)), a)[0].sev == "medium"

    b = make_agent(session, "Worse")
    add_runs(session, b, n=30, status="failed")
    add_runs(session, b, n=10, status="success")  # 75%
    sevs = {c.agent_id: c.sev for c in alert_rules.error_rate(session, NOW, make_rule(session))}
    assert sevs[b.id] == "high"


def test_runs_outside_the_window_are_ignored(session):
    """Otherwise an outage from last month keeps alerting forever."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed", when=NOW - timedelta(days=5))
    assert only(alert_rules.error_rate(session, NOW, make_rule(session)), a) == []


def test_denied_and_blocked_are_not_counted_as_failures(session):
    """UCS refusing and moderation refusing are guardrails WORKING.
    Counting them as errors reports a functioning control as an outage."""
    a = make_agent(session)
    add_runs(session, a, n=20, status="denied")
    add_runs(session, a, n=20, status="blocked")
    assert only(alert_rules.error_rate(session, NOW, make_rule(session)), a) == []


# --- the other rules -----------------------------------------------------

def test_repeated_authorization_denials_fire(session):
    a = make_agent(session)
    add_runs(session, a, n=3, status="denied")
    found = only(alert_rules.denied_burst(session, NOW, make_rule(session, kind='denied_burst', threshold=3)), a)
    assert len(found) == 1 and found[0].sev == "high"


def test_a_single_denial_does_not_fire(session):
    a = make_agent(session)
    add_runs(session, a, n=1, status="denied")
    assert only(alert_rules.denied_burst(session, NOW, make_rule(session, kind='denied_burst', threshold=3)), a) == []


def test_moderation_blocks_fire_and_name_both_explanations(session):
    """A block spike can mean probing OR an over-broad rule. The alert
    must not assert which."""
    a = make_agent(session)
    for _ in range(4):
        session.add(Run(id=f"r-{uuid.uuid4().hex[:12]}", agent_id=a.id,
                        agent_name=a.name, status="blocked", fail_class="moderation",
                        hops=1, created_at=NOW - timedelta(hours=1)))
    session.flush()
    found = only(alert_rules.guardrail_blocks(session, NOW, make_rule(session, kind='guardrail_block', threshold=3)), a)
    assert len(found) == 1
    assert "probed" in found[0].summary and "legitimate" in found[0].summary


def test_a_step_limit_hit_fires_on_a_single_run(session):
    """Rare enough not to be noisy, and one exhausted budget is already
    a defect worth reading."""
    a = make_agent(session)
    session.add(Run(id=f"r-{uuid.uuid4().hex[:12]}", agent_id=a.id, agent_name=a.name,
                    status="failed", fail_class="harness", hops=1,
                    created_at=NOW - timedelta(hours=1)))
    session.flush()
    assert len(only(alert_rules.step_limit(session, NOW, make_rule(session, kind='step_limit', threshold=1)), a)) == 1


def test_a_failing_eval_fires_and_a_passing_one_does_not(session):
    a = make_agent(session)
    es = EvalSet(id=f"es_{uuid.uuid4().hex[:10]}", agent_id=a.id, name="Recall")
    session.add(es)
    session.flush()
    session.add(EvalRun(id=f"er_{uuid.uuid4().hex[:10]}", eval_set_id=es.id,
                        agent_id=a.id, status="complete", started_at=NOW - timedelta(hours=2),
                        pass_count=2, total_count=10, drift="none"))
    session.flush()
    found = [c for c in alert_rules.quality_breach(session, NOW, make_rule(session, kind='quality_breach', threshold=70)) if c.agent_id == a.id]
    assert len(found) == 1 and found[0].sev == "high"   # 20% -> high

    b = make_agent(session, "Healthy evals")
    es2 = EvalSet(id=f"es_{uuid.uuid4().hex[:10]}", agent_id=b.id, name="Recall")
    session.add(es2)
    session.flush()
    session.add(EvalRun(id=f"er_{uuid.uuid4().hex[:10]}", eval_set_id=es2.id,
                        agent_id=b.id, status="complete", started_at=NOW - timedelta(hours=2),
                        pass_count=9, total_count=10, drift="none"))
    session.flush()
    assert [c for c in alert_rules.quality_breach(session, NOW, make_rule(session, kind='quality_breach', threshold=70)) if c.agent_id == b.id] == []


def test_only_the_latest_eval_run_counts(session):
    """An old failing run says nothing about the agent as it stands, and
    alerting on history would re-report it forever."""
    a = make_agent(session)
    es = EvalSet(id=f"es_{uuid.uuid4().hex[:10]}", agent_id=a.id, name="Recall")
    session.add(es)
    session.flush()
    session.add(EvalRun(id=f"er_{uuid.uuid4().hex[:10]}", eval_set_id=es.id, agent_id=a.id,
                        status="complete", started_at=NOW - timedelta(days=3),
                        pass_count=1, total_count=10, drift="none"))
    session.add(EvalRun(id=f"er_{uuid.uuid4().hex[:10]}", eval_set_id=es.id, agent_id=a.id,
                        status="complete", started_at=NOW - timedelta(hours=1),
                        pass_count=10, total_count=10, drift="none"))
    session.flush()
    assert [c for c in alert_rules.quality_breach(session, NOW, make_rule(session, kind='quality_breach', threshold=70)) if c.agent_id == a.id] == []


# --- dedup: the property that decides whether anyone keeps the channel ----

def test_the_same_condition_is_not_raised_twice(session):
    """Evaluated every 15 minutes, this rule would otherwise post 96
    identical alerts a day and the channel would be muted by lunchtime."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")

    first = alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
    assert len([c for c in first if c.agent_id == a.id]) == 1

    second = alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
    assert [c for c in second if c.agent_id == a.id] == []


def test_dedup_keys_on_the_condition_not_the_wording(session):
    """The numbers in a summary change every sweep while the condition
    stays the same. Matching on text would defeat dedup entirely."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))

    add_runs(session, a, n=10, status="failed")  # summary now reads differently
    again = alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
    assert [c for c in again if c.agent_id == a.id] == []


def test_a_resolved_alert_lets_the_condition_be_reported_again(session):
    """Dedup keys on an OPEN alert. If it never closed, an agent that
    degraded a second time could never alert again."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    raised = [c for c in alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
              if c.agent_id == a.id]
    raised[0].state = "resolved"
    session.add(raised[0])
    session.flush()

    again = [c for c in alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
             if c.agent_id == a.id]
    assert len(again) == 1


def test_an_unacknowledged_alert_stays_open_when_the_condition_clears(session):
    """An alert nobody has looked at is not the platform's to close.

    Auto-closing it means a problem can appear and disappear between two
    glances at the page, and whoever was paged finds nothing when they
    click through from Slack or Teams.
    """
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    # Named, because the seeded global error-rate rule fires for this
    # agent too and the assertion has to be about ours.
    r = make_rule(session, min_runs=20, agent_id=a.id)
    alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))

    later = NOW + timedelta(days=2)   # the failures age out of the window
    alert_rules.resolve_stale(session, later)

    mine = session.exec(select(Alert).where(
        Alert.agent_id == a.id, Alert.rule == r.name, Alert.state == "open")).all()
    assert len(mine) == 1, "an unacknowledged alert must not be closed for you"
    # …but it is marked, so the page can show it is no longer live.
    assert any(h["action"] == alert_rules.CLEARED_NOTE for h in mine[0].history)


def test_the_cleared_note_is_added_only_once(session):
    """This runs every sweep; repeating the line every 15 minutes would
    bury the real history."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    r = make_rule(session, min_runs=20, agent_id=a.id)
    alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))

    later = NOW + timedelta(days=2)
    for _ in range(3):
        alert_rules.resolve_stale(session, later)

    alert = session.exec(select(Alert).where(
        Alert.agent_id == a.id, Alert.rule == r.name)).first()
    notes = [h for h in alert.history if h["action"] == alert_rules.CLEARED_NOTE]
    assert len(notes) == 1


def test_an_acknowledged_alert_is_resolved_when_the_condition_clears(session):
    """Acknowledged means somebody knows, so closing it is a convenience
    rather than a loss."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    r = make_rule(session, min_runs=20, agent_id=a.id)
    raised = [x for x in alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
              if x.agent_id == a.id and x.rule == r.name]
    raised[0].acked = True
    session.add(raised[0])
    session.flush()

    alert_rules.resolve_stale(session, NOW + timedelta(days=2))
    session.refresh(raised[0])
    assert raised[0].state == "resolved"


def test_resolving_does_not_touch_an_alert_with_no_history(session):
    """A seeded row, or one from before this engine existed, has an
    empty history. Written as `alert.history and ...` the empty case
    fell through and resolved it — which silently closed the four
    seeded alerts on the page."""
    a = make_agent(session)
    orphan = Alert(id=f"al-{uuid.uuid4().hex[:10]}", sev="high", rule="quality-breach",
                   agent_id=a.id, agent_name=a.name, state="open", acked=False,
                   ts="2026-08-02 13:58", summary="Seeded row.", history=[])
    session.add(orphan)
    session.flush()
    alert_rules.resolve_stale(session, NOW)
    session.refresh(orphan)
    assert orphan.state == "open"


def test_resolving_does_not_touch_a_hand_raised_alert(session):
    """Someone else's alert is someone else's to close."""
    a = make_agent(session)
    manual = Alert(id=f"al-{uuid.uuid4().hex[:10]}", sev="high", rule="error-rate",
                   agent_id=a.id, agent_name=a.name, state="open", acked=False,
                   ts="2026-09-24 10:00", summary="Raised by a person.",
                   history=[{"action": "Raised", "by": "alice", "ts": "2026-09-24 10:00"}])
    session.add(manual)
    session.flush()
    alert_rules.resolve_stale(session, NOW)
    session.refresh(manual)
    assert manual.state == "open"


# --- the sweep as a whole ------------------------------------------------

def test_one_broken_rule_does_not_stop_the_others(monkeypatch, session):
    """A sweep that aborts on the first bad rule reports nothing at all."""
    def explode(session, now, rule):
        raise RuntimeError("bad SQL")
    monkeypatch.setitem(alert_rules.KIND_HANDLERS, "error_rate", explode)
    a = make_agent(session)
    add_runs(session, a, n=3, status="denied")
    make_rule(session, kind="denied_burst", threshold=3, min_runs=1)
    found = only(alert_rules.evaluate(session, NOW), a)
    assert len(found) >= 1


def test_a_disabled_rule_is_not_evaluated(session):
    """The switch has to actually stop the rule, or it is decoration."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    r = make_rule(session, threshold=25, min_runs=20, enabled=False)
    assert r not in alert_rules.active_rules(session)


def test_a_rule_scoped_to_one_agent_ignores_every_other(session):
    """The point of storing rules as data: hold one agent to a tighter
    bar without touching the rest."""
    a = make_agent(session, "Scoped")
    b = make_agent(session, "Untouched")
    add_runs(session, a, n=40, status="failed")
    add_runs(session, b, n=40, status="failed")
    scoped = make_rule(session, threshold=25, min_runs=20, agent_id=a.id)
    found = alert_rules.error_rate(session, NOW, scoped)
    assert [c.agent_id for c in found] == [a.id]


def test_the_threshold_on_the_row_is_what_decides(session):
    """Not a module constant — changing the row has to change the outcome."""
    a = make_agent(session)
    add_runs(session, a, n=12, status="failed")
    add_runs(session, a, n=28, status="success")  # 30%
    assert only(alert_rules.error_rate(
        session, NOW, make_rule(session, threshold=25, min_runs=20, agent_id=a.id)), a)
    assert not only(alert_rules.error_rate(
        session, NOW, make_rule(session, threshold=50, min_runs=20, agent_id=a.id)), a)


def test_the_window_on_the_row_is_what_decides(session):
    """A 1-hour rule must not see a failure from six hours ago."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed", when=NOW - timedelta(hours=6))
    wide = make_rule(session, window_seconds=86400, min_runs=20, agent_id=a.id)
    narrow = make_rule(session, window_seconds=3600, min_runs=20, agent_id=a.id)
    assert only(alert_rules.error_rate(session, NOW, wide), a)
    assert not only(alert_rules.error_rate(session, NOW, narrow), a)


def test_the_rule_name_is_what_the_alert_is_filed_under(session):
    """Dedup keys on (rule, agent), so two differently-named rules of the
    same kind must be able to raise separately."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    r = make_rule(session, name="my-tight-rule", min_runs=20, agent_id=a.id)
    found = alert_rules.error_rate(session, NOW, r)
    assert found[0].rule == "my-tight-rule"


def test_an_unknown_kind_is_logged_and_skipped_not_crashed(session, monkeypatch):
    """The CHECK constraint should prevent it; if the constraint and the
    handler table ever drift, the sweep must survive."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    monkeypatch.delitem(alert_rules.KIND_HANDLERS, "error_rate")
    alert_rules.evaluate(session, NOW)  # must not raise


def test_a_raised_alert_carries_its_provenance(session):
    """So resolve_stale can tell its own alerts from a person's, and so
    the Alerts page can show where one came from."""
    a = make_agent(session)
    add_runs(session, a, n=40, status="failed")
    raised = [c for c in alert_rules.raise_alerts(session, alert_rules.evaluate(session, NOW))
              if c.agent_id == a.id]
    assert raised[0].history[0]["by"] == "alert-rules"


def test_a_backlog_of_undelivered_alerts_is_trickled_not_dumped(session, monkeypatch):
    """The first sweep after a channel is added must not post the whole
    backlog at once. Observed: 17 open alerts across two channels sent
    30 messages in one go — exactly the flood dedup exists to prevent."""
    a = make_agent(session)
    for n in range(10):
        session.add(Alert(id=f"al-backlog-{n:02d}", sev="high", rule="error-rate",
                          agent_id=a.id, agent_name=a.name, state="open", acked=False,
                          ts="2026-09-24 09:00", summary=f"Backlog {n}.",
                          history=[{"action": "Raised", "by": "alert-rules",
                                    "ts": "2026-09-24 09:00"}],
                          delivered_at=None, delivered_to=[]))
    session.flush()

    sent = []
    # sweep() imports alert_delivery inside the function, so the real
    # module is what has to be patched.
    from app import alert_delivery
    # No rules evaluated, so the sweep does only the retry pass.
    monkeypatch.setattr(alert_rules, "active_rules", lambda s: [])
    monkeypatch.setattr(alert_delivery, "fan_out",
                        lambda s, al, d: (sent.append(al.id) or []))
    alert_rules.sweep(session)
    assert len(sent) <= alert_rules.RETRY_MAX_PER_SWEEP, sent


# --- what each tab means -------------------------------------------------
#
# Open = sent to a channel and not yet acted on. Acting on an alert from
# Slack or Teams has to move it out of Open, and an alert nobody was
# told about is not something anyone is working.

def test_delivery_is_recorded_on_the_alert_not_only_the_destination(session):
    """The Open tab filters on it, so it is part of the alert's state."""
    from app import alert_delivery
    a = make_agent(session)
    al = Alert(id=f"al-{uuid.uuid4().hex[:10]}", sev="high", rule="error-rate",
               agent_id=a.id, agent_name=a.name, state="open", acked=False,
               ts="2026-09-24 09:00", summary="x", history=[], delivered_to=[])
    session.add(al)
    session.flush()

    dest = AlertDestination(name="oncall", kind="slack", enabled=True,
                            min_severity="low", agent_id=None,
                            webhook_url_encrypted="enc", url_hint="h")
    session.add(dest)
    session.flush()

    original = alert_delivery.deliver
    alert_delivery.deliver = lambda d, x, **kw: {"ok": True, "detail": "HTTP 200"}
    try:
        alert_delivery.fan_out(session, al, [dest])
    finally:
        alert_delivery.deliver = original

    assert al.delivered_at is not None
    assert al.delivered_to == ["oncall"]


def test_a_failed_delivery_leaves_the_alert_undelivered(session):
    """So the sweep retries it, rather than it sitting in Open as though
    somebody had been told."""
    from app import alert_delivery
    a = make_agent(session)
    al = Alert(id=f"al-{uuid.uuid4().hex[:10]}", sev="high", rule="error-rate",
               agent_id=a.id, agent_name=a.name, state="open", acked=False,
               ts="2026-09-24 09:00", summary="x", history=[], delivered_to=[])
    session.add(al)
    session.flush()

    dest = AlertDestination(name="broken", kind="slack", enabled=True,
                            min_severity="low", agent_id=None,
                            webhook_url_encrypted="enc", url_hint="h")
    session.add(dest)
    session.flush()

    original = alert_delivery.deliver
    alert_delivery.deliver = lambda d, x, **kw: {"ok": False, "detail": "HTTP 404"}
    try:
        alert_delivery.fan_out(session, al, [dest])
    finally:
        alert_delivery.deliver = original

    assert al.delivered_at is None
    assert al.delivered_to == []


def test_a_retry_to_a_second_channel_does_not_erase_the_first(session):
    """delivered_to is merged, not replaced — otherwise the record of
    which channels were told is lost on every retry."""
    from app import alert_delivery
    a = make_agent(session)
    al = Alert(id=f"al-{uuid.uuid4().hex[:10]}", sev="high", rule="error-rate",
               agent_id=a.id, agent_name=a.name, state="open", acked=False,
               ts="2026-09-24 09:00", summary="x", history=[],
               delivered_to=["first"])
    session.add(al)
    session.flush()

    dest = AlertDestination(name="second", kind="slack", enabled=True,
                            min_severity="low", agent_id=None,
                            webhook_url_encrypted="enc", url_hint="h")
    session.add(dest)
    session.flush()

    original = alert_delivery.deliver
    alert_delivery.deliver = lambda d, x, **kw: {"ok": True, "detail": "HTTP 200"}
    try:
        alert_delivery.fan_out(session, al, [dest])
    finally:
        alert_delivery.deliver = original

    assert al.delivered_to == ["first", "second"]
