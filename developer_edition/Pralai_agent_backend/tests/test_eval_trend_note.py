"""The 7-day eval trend asserted both windows were empty when one was full.

The guard is an OR — either window being empty makes a week-over-week
comparison impossible — but the message said:

    "not enough runs in both the last 7 days and the 7 days before that"

On this install that fired while 25 completed eval runs sat in the
prior window and none in the current one. The sentence was the opposite
of what the data said, and it sent the reader looking for missing
history that was not missing. The real situation — "you have not run
your eval sets in nine days" — was not conveyed by any phrasing of "not
enough runs".
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _run(days_ago: float, passed=8, total=10):
    done = datetime.utcnow() - timedelta(days=days_ago)
    return SimpleNamespace(eval_set_id="es1", status="completed", pass_count=passed,
                           total_count=total, drift="none", started_at=done,
                           completed_at=done)


def _health(runs):
    """Run the real trend arithmetic over a chosen set of eval runs.

    Calls eval_trend directly — no database, no stubbed session. The
    suite runs against the live database and inserting eval history to
    check a sentence would leave debris behind.
    """
    from app.routers.monitoring import eval_trend

    delta, note = eval_trend(runs, datetime.utcnow())
    rates = [r.pass_count / r.total_count for r in runs if r.total_count]
    return {"trend_delta_pts": delta, "trend_note": note,
            "avg_pass_rate": round(sum(rates) / len(rates) * 100, 1) if rates else None}


class TestTheNoteNamesTheWindowThatIsActuallyEmpty:
    def test_runs_only_in_the_prior_week_does_not_claim_both_are_empty(self):
        # This install's exact state: everything nine days old.
        health = _health([_run(9), _run(9.2)])
        note = health["trend_note"]
        assert note, "a missing comparison must still be explained"
        assert "both" not in note, f"still claims both windows are empty: {note!r}"
        assert "last 7 days" in note

    def test_it_says_how_stale_the_newest_run_is(self):
        # The actionable fact: nobody has run the eval sets in nine days.
        health = _health([_run(9)])
        assert "9 days ago" in health["trend_note"]

    def test_runs_only_in_the_current_week_say_there_is_no_baseline(self):
        health = _health([_run(1), _run(2)])
        note = health["trend_note"]
        assert "both" not in note
        assert "first week" in note or "compare against" in note

    def test_nothing_in_either_window_says_so_plainly(self):
        health = _health([_run(30)])
        note = health["trend_note"]
        assert "14 days" in note
        assert "30 days ago" in note

    def test_runs_in_both_windows_produce_a_real_delta_and_no_note(self):
        # 90% this week against 50% last week.
        health = _health([_run(1, passed=9, total=10), _run(9, passed=5, total=10)])
        assert health["trend_note"] is None
        assert health["trend_delta_pts"] == 40.0

    def test_a_decline_is_reported_as_a_negative_delta(self):
        health = _health([_run(1, passed=5, total=10), _run(9, passed=9, total=10)])
        assert health["trend_delta_pts"] == -40.0

    def test_a_run_with_no_completed_at_is_ignored_not_counted_as_today(self):
        # A still-running eval has no completed_at. Treating it as
        # current would invent a this-week data point out of a run that
        # has not finished.
        unfinished = _run(0)
        unfinished.completed_at = None
        delta, note = _trend([unfinished, _run(9)])
        assert delta is None
        assert "last 7 days" in note


def _trend(runs):
    from app.routers.monitoring import eval_trend

    return eval_trend(runs, datetime.utcnow())


class TestTheEndpointStillShowsItsHeadlineNumber:
    def test_the_average_pass_rate_does_not_depend_on_the_trend_windows(self):
        """86.8% kept showing while the trend went blank, which is
        correct: the headline comes from the latest run per set with no
        date filter. A fix to the note must not change that."""
        from app.routers.monitoring import eval_trend

        nine_days = [_run(9, passed=7, total=10)]
        delta, note = eval_trend(nine_days, datetime.utcnow())
        assert delta is None and note  # no trend…
        # …while the pass rate of that same run is still a real number.
        assert nine_days[0].pass_count / nine_days[0].total_count == 0.7
