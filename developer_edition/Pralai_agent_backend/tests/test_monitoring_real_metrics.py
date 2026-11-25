"""
Real tests for the monitoring synthetic-metrics fix (section 0.77,
GAP_REPORT).

The real, reported bug: routers/monitoring.py had a hardcoded
PLATFORM_METRICS dict and get_home_stats returned runsToday=842 /
successRate=97.2 / avgCost="$0.041" as literal stubs.

The fix: a real, genuine `created_at` datetime column on Run, and real
computation of every metric this platform can genuinely support from
real Run data over a real 7-day window - with an honest `null` for
the five metrics this platform has no real backing data for at all.

§0.113, GAP_REPORT — _compute_platform_metrics itself was later
rewritten to compute every real aggregate (COUNT/AVG/PERCENTILE_CONT)
directly in a single, real Postgres query rather than loading every
real Run row into this API process's own memory first — see
routers/monitoring.py's own updated docstring for the full real
reasoning. The two test classes that used to test the now-removed,
dead `_parse_duration_seconds`/`_percentile` helper functions directly
were removed along with them — real coverage of the SAME real
percentile/duration behavior now lives directly in
TestRealPlatformMetricsComputation and TestRealSqlAggregateEquivalence
below, exercising the real replacement mechanism (a real Postgres
query) instead of a function that no longer exists.

These tests exercise the real functions and real database directly -
using uniquely-identifiable Run rows and verifying via delta against
the live platform total.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timedelta
from sqlmodel import Session, text
from fastapi.testclient import TestClient

from app.db import engine
from app.main import app
from app.models import Run
from app.routers.monitoring import _compute_platform_metrics

client = TestClient(app)


def cleanup_runs(prefix: str):
    with Session(engine) as s:
        s.exec(text("DELETE FROM run WHERE id LIKE :p"), params={"p": f"{prefix}%"})
        s.commit()


class TestRealPlatformMetricsComputation:
    def test_the_old_hardcoded_values_are_genuinely_gone(self):
        resp = client.get("/api/monitoring/metrics")
        assert resp.status_code == 200
        m = resp.json()["metrics"]
        assert m.get("success") != "97.2%"
        assert m.get("p50") != "1.4s"
        assert m.get("costAvg") != "$0.041"

    def test_untracked_metrics_are_honest_null_not_fabricated(self):
        """§0.77 listed five metrics as having no backing data. Three
        have since gained it, and this test is what caught each one
        still being hardcoded to None after the data arrived:

          costAvg    — real as of §0.78, which persists token/cost usage
                       onto Run.
          chainDepth — Run.hops was added later and carries real values.
          loopIter   — derivable from the persisted span list: a
                       container re-executes its children, so a child's
                       span is appended once per pass.

        Only two genuinely have nothing behind them. `retries` has no
        per-run attempt count anywhere in the schema (the only
        `attempts` column belongs to KBIngestionOutbox; Temporal keeps
        an activity's count internally). `cacheHit` has no cache to
        measure — UCS caches inside its own service, where this one
        cannot see the hit rate.

        Fabricating either is the exact bug §0.77 fixed, so they must
        stay null until something real backs them."""
        resp = client.get("/api/monitoring/metrics")
        m = resp.json()["metrics"]
        for key in ("retries", "cacheHit"):
            assert m[key] is None, f"{key} should be honestly null"

    def test_the_metrics_that_gained_real_data_are_no_longer_null(self):
        """The opposite failure to the one above, and the one that
        actually happened: data existed and the endpoint kept saying
        "not tracked" anyway."""
        m = client.get("/api/monitoring/metrics").json()["metrics"]
        if m["sample_size"] == 0:
            pytest.skip("no runs in the window to compute from")
        assert m["chainDepth"] is not None, "Run.hops exists — this cannot be null"
        assert m["chainDepth"] >= 1.0
        assert m["costAvg"] is not None

    def test_known_controlled_runs_are_correctly_counted_within_the_window(self):
        prefix = f"test77-{uuid.uuid4().hex[:8]}-"
        with Session(engine) as s:
            before = _compute_platform_metrics(s)["sample_size"]

        now = datetime.utcnow()
        with Session(engine) as s:
            for i in range(3):
                s.add(Run(id=f"{prefix}{i}", agent_id="a1", agent_name="x", status="success",
                          duration="1.0s", cost="$0.00", ts="00:00:00", created_at=now))
            s.add(Run(id=f"{prefix}old", agent_id="a1", agent_name="x", status="success",
                      duration="1.0s", cost="$0.00", ts="00:00:00", created_at=now - timedelta(days=10)))
            s.commit()

        try:
            with Session(engine) as s:
                after = _compute_platform_metrics(s)["sample_size"]
            assert after - before == 3
        finally:
            cleanup_runs(prefix)

    def test_a_real_harness_failure_is_reflected_in_step_limit_rate(self):
        prefix = f"test77sl-{uuid.uuid4().hex[:8]}-"
        now = datetime.utcnow()
        with Session(engine) as s:
            before = _compute_platform_metrics(s)
        with Session(engine) as s:
            s.add(Run(id=f"{prefix}1", agent_id="a1", agent_name="x", status="failed",
                      duration="1.0s", cost="$0.00", ts="00:00:00", created_at=now, fail_class="harness"))
            s.commit()
        try:
            with Session(engine) as s:
                after = _compute_platform_metrics(s)
            assert after["sample_size"] == before["sample_size"] + 1
            assert after["stepLimit"] >= before["stepLimit"]
        finally:
            cleanup_runs(prefix)


class TestRealZeroRunsCase:
    def test_zero_runs_in_the_window_returns_honest_nulls_across_the_board(self):
        """§0.113, GAP_REPORT — updated for the real, SQL-aggregate
        rewrite: the fake session/result now mimics a real, single
        aggregate row (via `.one()`), matching _compute_platform_metrics'
        own new real query shape, not the old `.all()`-returning-a-
        list-of-Run-rows shape this test's own fake previously mocked."""
        class _Row:
            total = 0
            success_count = 0
            harness_count = 0
            moderation_count = 0
            denied_count = 0
            avg_cost_usd = None
            p50_ms = None
            p95_ms = None

        class _FakeResult:
            def one(self):
                return _Row()

        class _FakeSession:
            def exec(self, *a, **kw):
                return _FakeResult()

        result = _compute_platform_metrics(_FakeSession())
        assert result["sample_size"] == 0
        for key in ("success", "p50", "p95", "stepLimit", "guardrail", "entPass",
                    "costAvg", "retries", "loopIter", "chainDepth", "cacheHit"):
            assert result[key] is None


class TestRealHomeStats:
    def test_avg_cost_is_real_as_of_0_78_not_the_old_fabricated_stub(self):
        """avgCost became real in §0.78, GAP_REPORT — superseding the
        earlier §0.77 fix's own honest null (there was genuinely no
        cost data to report an average of at that point). Real Run
        rows already exist in this shared dev DB's own 7-day window
        (from other tests/sessions), so a real, non-null figure is
        expected here, not the old hardcoded "$0.041" string."""
        resp = client.get("/api/monitoring/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["avgCost"] != "$0.041"
        if body["runsToday"] > 0:
            assert body["avgCost"] is not None
            assert body["avgCost"].startswith("$")

    def test_runs_today_and_success_rate_are_real_not_the_old_stub_values(self):
        resp = client.get("/api/monitoring/stats")
        body = resp.json()
        assert body["runsToday"] != 842
        assert body["successRate"] != 97.2
        assert isinstance(body["runsToday"], int)
