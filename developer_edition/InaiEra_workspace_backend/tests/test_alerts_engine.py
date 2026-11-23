"""Alert rules as pure functions: what breaches, per kind, and what the open / keep / clear bookkeeping does with it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.alerts.engine import Breach, LiveAlert, Obs, RuleSpec, evaluate, evaluate_job, reason_text, reconcile

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def run(outcome, minutes_ago, *, job="j1", name="customers", kind="dbt", failure_class=None, detail=None, ref=None):
    return Obs(job, name, kind, "run", outcome, NOW - timedelta(minutes=minutes_ago), ref or f"r-{outcome}-{minutes_ago}-{job}",
               failure_class, detail)


def rule(kind, *, threshold=1, window=3600, min_runs=1, scope=None, enabled=True, id="rule-1", severity="high"):
    return RuleSpec(id, f"rule-{kind}", kind, severity, threshold, window, min_runs, scope, enabled)


# ── run_failed ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_run_failed_fires_for_a_latest_failure_and_names_the_reason():
    b = evaluate_job(rule("run_failed"), [run("succeeded", 30), run("failed", 5, detail="Compilation Error\nmore detail")], NOW)
    assert b is not None and b.severity == "high" and b.job_name == "customers"
    assert b.summary == "dbt project 'customers' failed: Compilation Error more detail"       # the whole reason, on one line


def test_run_failed_does_not_fire_once_the_job_has_succeeded_again():
    assert evaluate_job(rule("run_failed"), [run("failed", 30), run("succeeded", 5)], NOW) is None


def test_run_failed_needs_the_threshold_within_the_window():
    r = rule("run_failed", threshold=3, window=3600)
    assert evaluate_job(r, [run("failed", 50), run("failed", 20)], NOW) is None
    b = evaluate_job(r, [run("failed", 50), run("failed", 20), run("failed", 5)], NOW)
    assert b is not None and b.summary.startswith("3 runs of dbt project 'customers' failed in the last 1 hour")


def test_run_failed_ignores_failures_older_than_the_window():
    assert evaluate_job(rule("run_failed", threshold=2, window=3600), [run("failed", 400), run("failed", 5)], NOW) is None


def test_a_cancelled_or_running_run_is_not_a_failure_and_not_a_recovery():
    r = rule("run_failed")
    assert evaluate_job(r, [run("failed", 20), run("cancelled", 5)], NOW) is not None      # the cancelled run is not looked at
    assert evaluate_job(r, [run("cancelled", 5)], NOW) is None
    assert evaluate_job(r, [Obs("j1", "customers", "dbt", "run", "running", NOW, "x")], NOW) is None


def test_default_window_is_24_hours_when_zero():
    assert evaluate_job(rule("run_failed", window=0), [run("failed", 23 * 60)], NOW) is not None
    assert evaluate_job(rule("run_failed", window=0), [run("failed", 25 * 60)], NOW) is None


def test_a_quality_failure_belongs_to_the_quality_rule_not_run_failed():
    obs = [run("failed", 5, failure_class="quality", detail="Got 3 results, configured to fail if != 0")]
    assert evaluate_job(rule("run_failed"), obs, NOW) is None
    b = evaluate_job(rule("quality_failed"), obs, NOW)
    assert b is not None and b.summary.startswith("A quality check failed on dbt project 'customers': Got 3 results")


def test_a_latest_quality_failure_is_not_a_run_failed_even_after_an_earlier_plain_failure():
    obs = [run("failed", 30, detail="Compilation Error"), run("failed", 5, failure_class="quality", detail="Got 3 results, configured to fail if != 0")]
    assert evaluate_job(rule("run_failed"), obs, NOW) is None                 # the quality alert owns it
    assert evaluate_job(rule("quality_failed"), obs, NOW) is not None


def test_quality_failed_does_not_fire_for_a_plain_failure():
    assert evaluate_job(rule("quality_failed"), [run("failed", 5)], NOW) is None


# ── consecutive_failures / failure_rate ────────────────────────────────────────────────────────────────────────────────────
def test_consecutive_failures_needs_the_last_n_runs_all_failed():
    r = rule("consecutive_failures", threshold=3, window=0)
    assert evaluate_job(r, [run("failed", 40), run("failed", 30), run("succeeded", 20), run("failed", 10)], NOW) is None
    assert evaluate_job(r, [run("failed", 30), run("failed", 20), run("failed", 10)], NOW) is not None
    assert evaluate_job(r, [run("failed", 20), run("failed", 10)], NOW) is None                      # not enough runs yet


def test_failure_rate_needs_min_runs_and_the_percentage():
    r = rule("failure_rate", threshold=50, window=0, min_runs=4, severity="medium")
    assert evaluate_job(r, [run("failed", 3), run("failed", 2), run("failed", 1)], NOW) is None      # only 3 runs: noise
    b = evaluate_job(r, [run("failed", 4), run("failed", 3), run("succeeded", 2), run("succeeded", 1)], NOW)
    assert b is not None and b.summary == "50% of 4 runs of dbt project 'customers' failed in the last 24 hours (threshold 50%)."
    assert evaluate_job(r, [run("failed", 4), run("succeeded", 3), run("succeeded", 2), run("succeeded", 1)], NOW) is None


# ── upstream_blocked / flink ───────────────────────────────────────────────────────────────────────────────────────────────
def blocked(minutes_ago, detail="Not started: upstream project 'customers' failed."):
    return Obs("j1", "revenue", "dbt", "blocked", "blocked", NOW - timedelta(minutes=minutes_ago), f"d-{minutes_ago}", None, detail)


def test_upstream_blocked_fires_while_the_latest_thing_is_a_refusal_and_clears_after_a_run():
    r = rule("upstream_blocked")
    b = evaluate_job(r, [blocked(10)], NOW)
    assert b is not None and "was refused: Not started: upstream project 'customers' failed." in b.summary
    assert evaluate_job(r, [blocked(10), run("succeeded", 2)], NOW) is None


def flink(status, minutes_ago, detail=None):
    return Obs("f1", "orders-stream", "flink", "flink_state", status, NOW - timedelta(minutes=minutes_ago), f"f1:{status}:{minutes_ago}",
               None, detail)


def test_flink_job_failed_follows_the_latest_state():
    r = rule("flink_job_failed", window=0)
    assert evaluate_job(r, [flink("running", 60), flink("failed", 5)], NOW).summary == "Flink job 'orders-stream' is in the failed state."
    assert evaluate_job(r, [flink("failed", 60), flink("running", 5)], NOW) is None
    assert evaluate_job(r, [flink("failed", 5, "bad sink")], NOW).summary.endswith("failed state: bad sink")


# ── evaluate: scope, enabled, many jobs ────────────────────────────────────────────────────────────────────────────────────
def test_evaluate_checks_every_job_and_honours_scope_and_enabled():
    obs = [run("failed", 5, job="a", name="a"), run("failed", 5, job="b", name="b"), run("succeeded", 5, job="c", name="c")]
    assert sorted(b.job_key for b in evaluate([rule("run_failed")], obs, NOW)) == ["a", "b"]
    assert [b.job_key for b in evaluate([rule("run_failed", scope="b")], obs, NOW)] == ["b"]
    assert evaluate([rule("run_failed", scope="zzz")], obs, NOW) == []                   # a scope with no observations: nothing
    assert evaluate([rule("run_failed", enabled=False)], obs, NOW) == []


def test_two_rules_on_one_job_are_two_breaches_with_different_fingerprints():
    obs = [run("failed", 5), run("failed", 4), run("failed", 3)]
    bs = evaluate([rule("run_failed", id="r1"), rule("consecutive_failures", threshold=3, id="r2")], obs, NOW)
    assert sorted(b.fingerprint for b in bs) == ["r1:j1", "r2:j1"]


# ── reconcile ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
def breach(rule_id="r1", job="j1"):
    return Breach(rule_id, "n", "run_failed", "high", job, "job", "dbt", "s", "run-1")


def live(rule_id="r1", job="j1", state="open", id="a1"):
    return LiveAlert(id, f"{rule_id}:{job}", state, rule_id)


def test_a_new_breach_is_raised_an_open_one_is_kept_and_a_stopped_one_is_cleared():
    plan = reconcile([breach("r1", "j1"), breach("r1", "j2")], [live("r1", "j1", id="a1"), live("r1", "j3", id="a3")],
                     evaluated_rule_ids=["r1"])
    assert [b.job_key for b in plan.raise_new] == ["j2"]
    assert [a.id for a, _ in plan.keep] == ["a1"]
    assert [a.id for a in plan.clear] == ["a3"]


def test_an_acknowledged_alert_that_still_breaches_stays_acknowledged_and_is_not_raised_again():
    plan = reconcile([breach()], [live(state="acknowledged")], evaluated_rule_ids=["r1"])
    assert plan.raise_new == [] and len(plan.keep) == 1 and plan.clear == []


def test_alerts_of_a_rule_that_was_not_evaluated_are_left_alone():
    plan = reconcile([], [live("r1", "j1"), live("r2", "j1", id="a2")], evaluated_rule_ids=["r2"])
    assert [a.id for a in plan.clear] == ["a2"]                           # r1 is disabled / skipped: its alert stays


def test_an_alert_whose_rule_was_deleted_is_never_cleared_by_the_sweep():
    plan = reconcile([], [LiveAlert("a9", "gone:j1", "open", None)], evaluated_rule_ids=["r1"])
    assert plan.clear == []


def test_the_same_breach_twice_raises_once():
    plan = reconcile([breach(), breach()], [], evaluated_rule_ids=["r1"])
    assert len(plan.raise_new) == 1


@pytest.mark.parametrize("kind", ["run_failed", "consecutive_failures", "failure_rate", "quality_failed", "upstream_blocked", "flink_job_failed"])
def test_no_observations_never_breach(kind):
    assert evaluate_job(rule(kind), [], NOW) is None


# ── the reason is never cut short ─────────────────────────────────────────────────────────────────────────────────────────
LONG = ("Not started: upstream project 'inaiera-dbt-test/customers' last succeeded 6 days ago, more than the 10 minutes allowed; "
        "upstream project 'inaiera-dbt-test/trino_sales' failed. It can run again once every upstream project has a recent successful run.")


def test_a_reason_of_several_hundred_characters_is_shown_in_full():
    assert len(LONG) > 240                                                            # it used to be cut at 240: "... a recent succe…"
    b = evaluate_job(rule("upstream_blocked"), [Obs("j1", "revenue", "sql", "blocked", "blocked", NOW, "d1", None, LONG)], NOW)
    assert b.summary.endswith("It can run again once every upstream project has a recent successful run.") and "…" not in b.summary


def test_a_multi_line_error_keeps_every_line_not_just_the_first():
    detail = "Database Error\n  failed to execute: HTTPConnectionPool(host='trino', port=8080): Max retries exceeded"
    b = evaluate_job(rule("run_failed"), [run("failed", 5, detail=detail)], NOW)
    assert b.summary == "dbt project 'customers' failed: Database Error failed to execute: HTTPConnectionPool(host='trino', port=8080): Max retries exceeded"


def test_reason_text_collapses_whitespace_and_cuts_only_past_the_limit():
    assert reason_text("  a \n\n  b\t c  ") == "a b c"
    assert reason_text(None) == "" and reason_text("") == ""
    exact = "x" * 1000
    assert reason_text(exact) == exact
    cut = reason_text("x" * 1001)
    assert len(cut) == 1000 and cut.endswith("…")
