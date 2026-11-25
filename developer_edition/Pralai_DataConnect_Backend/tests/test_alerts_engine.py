"""The alert rules as pure functions (app/services/alerts/engine.py): every DataConnect rule kind, windows, scope, the open / keep / clear
bookkeeping, and "after a person resolved it, raise again only on newer evidence"."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.alerts.engine import (
    LiveAlert,
    Obs,
    RuleSpec,
    evaluate,
    evaluate_pipeline,
    reason_text,
    reconcile,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def obs(category, outcome, minutes_ago, *, pid="p1", name="orders-cdc", detail=None, ref="", count=1, direction="SOURCE"):
    return Obs(pid, name, direction, category, outcome, ago(minutes_ago), ref or f"{pid}:{category}:{minutes_ago}", detail, count)


def rule(kind, threshold=1, window=0, *, scope=None, enabled=True, rid="r1", name=None):
    return RuleSpec(rid, name or kind.replace("_", "-"), kind, "high", threshold, window, 1, scope, enabled)


def state(outcome, minutes_ago, **kw):
    return obs("state", outcome, minutes_ago, **kw)


# ── state rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_failed_pipeline_breaches_with_its_full_reason():
    reason = "Kafka Connect reports the connector FAILED: org.apache.kafka.connect.errors.ConnectException: Could not connect\n  to db:5432"
    b = evaluate_pipeline(rule("pipeline_failed"), [state("running", 30), state("failed", 2, detail=reason)], NOW)
    assert b is not None
    assert b.pipeline_id == "p1"
    assert b.direction == "SOURCE"
    assert b.summary == "Pipeline 'orders-cdc' failed: " + " ".join(reason.split())
    assert b.evidence_at == ago(2)


@pytest.mark.parametrize("latest", ["running", "paused", "degraded"])
def test_failed_rule_only_fires_while_the_latest_known_state_is_failed(latest):
    assert evaluate_pipeline(rule("pipeline_failed"), [state("failed", 30), state(latest, 2)], NOW) is None
    assert evaluate_pipeline(rule("pipeline_failed"), [state("failed", 30), state(latest, 2), state("unknown", 1)], NOW) is None


@pytest.mark.parametrize(("kind", "outcome"), [("pipeline_failed", "failed"), ("pipeline_degraded", "degraded")])
def test_an_unknown_reading_is_not_a_recovery(kind, outcome):
    """Connect reports a connector it is restarting (the self-heal, every tick) without a state: the alert must neither clear nor come back."""
    b = evaluate_pipeline(rule(kind), [state("running", 90), state(outcome, 30, detail="task crashed"), state("unknown", 2)], NOW)
    assert b is not None
    assert b.summary.endswith("task crashed")
    assert b.evidence_at == ago(30)


def test_failed_again_after_unknown_readings_is_the_same_failure_not_newer_evidence():
    seen = [state("failed", 30, detail="a"), state("unknown", 25), state("failed", 20, detail="a"), state("unknown", 15),
            state("failed", 10, detail="b")]
    b = evaluate_pipeline(rule("pipeline_failed"), seen, NOW)
    assert b.evidence_at == ago(30)                        # began 30 minutes ago: a person who resolved it since is not paged again
    assert b.summary == "Pipeline 'orders-cdc' failed: b"   # but the newest reason is the one shown


def test_failed_again_after_it_ran_is_a_new_failure():
    b = evaluate_pipeline(rule("pipeline_failed"), [state("failed", 30), state("running", 20), state("unknown", 15), state("failed", 10)], NOW)
    assert b.evidence_at == ago(10)


def test_only_unknown_readings_raise_no_failure():
    assert evaluate_pipeline(rule("pipeline_failed"), [state("unknown", 30), state("unknown", 2)], NOW) is None


def test_a_failure_without_a_reason_still_reads_as_a_sentence():
    assert evaluate_pipeline(rule("pipeline_failed"), [state("failed", 1)], NOW).summary == "Pipeline 'orders-cdc' failed."


def test_degraded_has_its_own_rule_and_wording():
    b = evaluate_pipeline(rule("pipeline_degraded"), [state("degraded", 1, detail="task 1 FAILED")], NOW)
    assert b.summary == "Pipeline 'orders-cdc' is degraded: task 1 FAILED"
    assert evaluate_pipeline(rule("pipeline_failed"), [state("degraded", 1)], NOW) is None
    assert evaluate_pipeline(rule("pipeline_degraded"), [state("failed", 1)], NOW) is None


def test_the_state_rules_ignore_the_window_the_state_counts_however_old():
    assert evaluate_pipeline(rule("pipeline_failed", window=300), [state("failed", 60 * 24 * 10)], NOW) is not None


def test_unknown_state_only_alerts_after_the_threshold_in_minutes():
    r = rule("runtime_unreachable", threshold=10)
    assert evaluate_pipeline(r, [state("unknown", 5)], NOW) is None
    b = evaluate_pipeline(r, [state("unknown", 15, detail="connection refused")], NOW)
    assert b is not None
    assert "has not reported the state of pipeline 'orders-cdc' for 15 minutes" in b.summary
    assert evaluate_pipeline(r, [state("unknown", 30), state("running", 1)], NOW) is None


# ── counting rules ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
OPS = "pipeline_deploy"


def op(outcome, minutes_ago, *, kind=OPS, detail=None, pid="p1"):
    return obs("operation", outcome, minutes_ago, pid=pid, ref=f"op-{minutes_ago}|{kind}", detail=detail)


def test_a_failed_deploy_breaches_and_names_the_action():
    b = evaluate_pipeline(rule("operation_failed", window=3_600), [op("failed", 5, detail="Kafka Connect rejected the connector configuration.")], NOW)
    assert b.summary == "A deploy of pipeline 'orders-cdc' failed: Kafka Connect rejected the connector configuration."


def test_a_later_successful_action_clears_a_failed_one():
    assert evaluate_pipeline(rule("operation_failed", window=3_600), [op("failed", 10), op("succeeded", 2)], NOW) is None


def test_operation_threshold_and_window():
    r = rule("operation_failed", threshold=2, window=3_600)
    assert evaluate_pipeline(r, [op("failed", 5)], NOW) is None                                       # only one
    assert evaluate_pipeline(r, [op("failed", 90), op("failed", 5)], NOW) is None                    # one is outside the hour
    b = evaluate_pipeline(r, [op("failed", 50, kind="pipeline_restart"), op("failed", 5, kind="pipeline_resume")], NOW)
    assert b.summary.startswith("2 actions on pipeline 'orders-cdc' failed in the last 1 hour; the latest, a resume")


def test_an_error_threshold_pause_breaches_until_the_pipeline_runs_again():
    pause = obs("threshold", "paused", 10, detail="250 dead-letter records in 5 minutes, limit 100")
    r = rule("error_threshold_paused", window=3_600)
    b = evaluate_pipeline(r, [pause], NOW)
    assert b.summary == ("DataConnect paused pipeline 'orders-cdc' because its dead-letter errors passed its error threshold: "
                         "250 dead-letter records in 5 minutes, limit 100")
    assert evaluate_pipeline(r, [pause, state("running", 3)], NOW) is None                            # resumed and running
    assert evaluate_pipeline(r, [pause, state("running", 20)], NOW) is not None                       # running BEFORE the pause doesn't count


@pytest.mark.parametrize(("kind", "category", "words"), [("dlq_errors", "dlq", "went to the dead-letter queue of"),
                                                 ("quality_failures", "quality", "failed the quality check of")])
def test_counting_rules_sum_the_buckets_in_the_window(kind, category, words):
    r = rule(kind, threshold=100, window=900)
    buckets = [obs(category, "count", m, count=n) for m, n in ((20, 500), (10, 40), (5, 30))]        # 20 min ago is outside 15 minutes
    assert evaluate_pipeline(r, buckets, NOW) is None                                                 # 70 < 100
    b = evaluate_pipeline(r, [*buckets, obs(category, "count", 1, count=31)], NOW)
    assert b.summary == f"101 records {words} pipeline 'orders-cdc' in the last 15 minutes (threshold 100)."
    assert b.evidence_at == ago(1)


def test_a_count_rule_with_no_window_uses_the_default_hour():
    assert evaluate_pipeline(rule("dlq_errors", threshold=10), [obs("dlq", "count", 50, count=10)], NOW) is not None
    assert evaluate_pipeline(rule("dlq_errors", threshold=10), [obs("dlq", "count", 70, count=10)], NOW) is None


def test_the_two_counters_do_not_mix():
    assert evaluate_pipeline(rule("dlq_errors", threshold=5), [obs("quality", "count", 1, count=50)], NOW) is None


def test_a_schema_change_waiting_too_long():
    r = rule("schema_drift_pending", threshold=24)
    assert evaluate_pipeline(r, [obs("drift", "pending", 60 * 10)], NOW) is None
    b = evaluate_pipeline(r, [obs("drift", "pending", 60 * 30, ref="d1"), obs("drift", "pending", 60, ref="d2")], NOW)
    assert b.summary.startswith("2 schema changes seen on pipeline 'orders-cdc' are waiting for approval, the oldest for 30 hours")
    assert b.evidence_at == ago(60)


# ── evaluate: which pipelines, which rules ──────────────────────────────────────────────────────────────────────────────────────
def test_every_pipeline_is_evaluated_and_a_scoped_rule_looks_at_one():
    o = [state("failed", 1, pid="a", name="A"), state("failed", 1, pid="b", name="B"), state("running", 1, pid="c", name="C")]
    assert [b.pipeline_id for b in evaluate([rule("pipeline_failed")], o, NOW)] == ["a", "b"]
    assert [b.pipeline_id for b in evaluate([rule("pipeline_failed", scope="b")], o, NOW)] == ["b"]
    assert evaluate([rule("pipeline_failed", scope="zzz")], o, NOW) == []


def test_a_disabled_rule_evaluates_nothing():
    assert evaluate([rule("pipeline_failed", enabled=False)], [state("failed", 1)], NOW) == []


def test_the_fingerprint_is_rule_and_pipeline():
    assert evaluate([rule("pipeline_failed", rid="R")], [state("failed", 1, pid="P")], NOW)[0].fingerprint == "R:P"


# ── reconcile ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def breach(rid="r1", pid="p1", minutes_ago=1):
    return evaluate([rule("pipeline_failed", rid=rid)], [state("failed", minutes_ago, pid=pid)], NOW)[0]


def test_new_breaches_are_raised_reported_ones_are_kept_quiet_and_stopped_ones_cleared():
    live = [LiveAlert("a1", "r1:p1", "open", "r1"), LiveAlert("a2", "r1:p2", "acknowledged", "r1")]
    plan = reconcile([breach(pid="p1"), breach(pid="p3")], live, evaluated_rule_ids=["r1"])
    assert [b.pipeline_id for b in plan.raise_new] == ["p3"]
    assert [(a.id, b.pipeline_id) for a, b in plan.keep] == [("a1", "p1")]
    assert [a.id for a in plan.clear] == ["a2"]


def test_alerts_of_a_rule_that_was_not_evaluated_are_never_cleared():
    plan = reconcile([], [LiveAlert("a1", "r1:p1", "open", "r1"), LiveAlert("a2", "r9:p1", "open", None)], evaluated_rule_ids=[])
    assert plan.clear == []


def test_the_same_breach_twice_is_raised_once():
    assert len(reconcile([breach(), breach()], [], evaluated_rule_ids=["r1"]).raise_new) == 1


def test_after_a_person_resolved_it_the_same_failure_is_not_raised_again():
    resolved_at = ago(5)
    plan = reconcile([breach(minutes_ago=30)], [], evaluated_rule_ids=["r1"], resolved_by_hand={"r1:p1": resolved_at})
    assert plan.raise_new == []
    assert [b.fingerprint for b in plan.held_back] == ["r1:p1"]


def test_but_a_newer_failure_after_that_is():
    plan = reconcile([breach(minutes_ago=1)], [], evaluated_rule_ids=["r1"], resolved_by_hand={"r1:p1": ago(5)})
    assert [b.fingerprint for b in plan.raise_new] == ["r1:p1"]


def test_resolving_one_pipeline_does_not_hold_back_another():
    plan = reconcile([breach(pid="p2", minutes_ago=30)], [], evaluated_rule_ids=["r1"], resolved_by_hand={"r1:p1": ago(5)})
    assert [b.pipeline_id for b in plan.raise_new] == ["p2"]


# ── reasons ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_reason_is_kept_whole_on_one_line_and_only_cut_past_1000_characters():
    long = "word " * 150                                                   # 750 characters: the Workspace once cut at 240
    assert reason_text(long) == long.strip()
    assert reason_text("a\n\n  b\tc") == "a b c"
    cut = reason_text("x" * 5000)
    assert len(cut) == 1000
    assert cut.endswith("…")
    assert reason_text(None) == ""
