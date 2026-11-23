"""
Real tests for the typed Trace/Run schema fix (section 0.79,
GAP_REPORT).

The real, reported bug: Run/Trace used string fields (duration =
"1.8s", cost = "$0.00", ts = "14:22:08") - genuinely unusable for a
real SQL aggregation, retention, or SLO query without first parsing
them back into real numbers.

The fix: real duration_ms (Integer), started_at/completed_at
(TIMESTAMPTZ) added to both Run and Trace; cost_usd (added §0.78 as a
plain Float) converted to Decimal/NUMERIC(12,6) - the correct real
type for currency. Two real bugs were caught and fixed while building
this: _aggregate_run_usage's own float default would crash sum() the
moment a Decimal-carrying span mixed with a non-Decimal one; and
Trace.spans_json (a JSONB column) can't natively store a Decimal,
raising a genuine TypeError - fixed via _json_safe_spans, converting
only at that one JSON-storage boundary while keeping the authoritative
typed columns at full precision.

These tests exercise the real, running API and real database directly.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from decimal import Decimal
from sqlmodel import Session, text
from fastapi.testclient import TestClient

from app.db import engine
from app.main import app
from app.routers.execution import _typed_timing, _aggregate_run_usage, _json_safe_spans

client = TestClient(app)

FLOW_ONE_LLM = {
    "nodes": [
        {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
        {"id": "n2", "type": "llm", "x": 100, "y": 0, "sub": "", "config": {
            "model": "Claude Sonnet 5", "system_prompt": "You are helpful.", "user_prompt": "{{input}}",
        }},
        {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
        {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
    ],
}


def make_agent() -> str:
    r = client.post("/api/agents", json={
        "name": f"Typed Metrics Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": FLOW_ONE_LLM,
    })
    return r.json()["id"]


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        for table in ("run", "trace", "agentlog"):
            s.exec(text(f"DELETE FROM {table} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealTypedTiming:
    def test_duration_ms_is_a_real_millisecond_integer(self):
        timing = _typed_timing(1.834)
        assert timing["duration_ms"] == 1834
        assert isinstance(timing["duration_ms"], int)

    def test_started_at_and_completed_at_are_internally_consistent(self):
        timing = _typed_timing(2.5)
        delta = (timing["completed_at"] - timing["started_at"]).total_seconds()
        assert delta == pytest.approx(2.5, abs=0.01)

    def test_zero_duration_is_handled_cleanly(self):
        timing = _typed_timing(0.0)
        assert timing["duration_ms"] == 0
        assert timing["started_at"] == timing["completed_at"]


class TestRealDecimalCostHandling:
    def test_a_decimal_carrying_span_mixed_with_a_plain_span_does_not_crash(self):
        spans = [
            {"node": "trigger", "status": "ok"},
            {"node": "llm", "status": "ok", "input_tokens": 5, "output_tokens": 10, "cached_tokens": 0, "cost_usd": Decimal("0.0003")},
        ]
        usage = _aggregate_run_usage(spans)
        assert usage["cost_usd"] == Decimal("0.0003")

    def test_json_safe_spans_converts_decimal_for_jsonb_storage(self):
        spans = [{"node": "llm", "cost_usd": Decimal("0.000123")}]
        safe = _json_safe_spans(spans)
        assert isinstance(safe[0]["cost_usd"], float)
        assert safe[0]["cost_usd"] == pytest.approx(0.000123)
        assert isinstance(spans[0]["cost_usd"], Decimal)

    def test_json_safe_spans_leaves_non_decimal_spans_alone(self):
        spans = [{"node": "code", "status": "ok"}]
        safe = _json_safe_spans(spans)
        assert safe == spans


class TestRealLiveExecutionTypedFields:
    def test_a_real_run_produces_internally_consistent_typed_fields(self):
        agent_id = make_agent()
        try:
            r = client.post(f"/api/agents/{agent_id}/run", json={"input": "hello world", "invoking_user": "user:test"})
            assert r.status_code == 200, r.text
            body = r.json()

            with Session(engine) as s:
                run_row = s.exec(text(
                    "SELECT duration_ms, started_at, completed_at, cost_usd FROM run WHERE id = :r"
                ), params={"r": body["run_id"]}).first()
                trace_row = s.exec(text(
                    "SELECT duration_ms, started_at, completed_at, cost_usd FROM trace WHERE id = :t"
                ), params={"t": body["trace_id"]}).first()

            duration_ms, started_at, completed_at, cost_usd = run_row
            assert duration_ms is not None and duration_ms >= 0
            assert started_at is not None and completed_at is not None
            assert completed_at >= started_at
            computed_ms = (completed_at - started_at).total_seconds() * 1000
            assert abs(computed_ms - duration_ms) < 50

            assert isinstance(cost_usd, Decimal)
            assert cost_usd > 0

            assert trace_row[0] == duration_ms
            assert trace_row[3] == cost_usd
        finally:
            cleanup_agent(agent_id)

    def test_cost_usd_is_a_real_numeric_column_not_float(self):
        with Session(engine) as s:
            col_type = s.exec(text(
                "SELECT data_type FROM information_schema.columns WHERE table_name='run' AND column_name='cost_usd'"
            )).first()
        assert col_type[0] == "numeric"

    def test_duration_ms_and_timestamps_are_real_typed_columns(self):
        with Session(engine) as s:
            cols = s.exec(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='run' AND column_name IN ('duration_ms', 'started_at', 'completed_at')"
            )).all()
        by_name = {c[0]: c[1] for c in cols}
        assert by_name["duration_ms"] == "integer"
        assert by_name["started_at"] == "timestamp with time zone"
        assert by_name["completed_at"] == "timestamp with time zone"


class TestRealAggregationQueryNowPossible:
    def test_a_real_percentile_and_sum_query_runs_successfully(self):
        agent_id = make_agent()
        try:
            r = client.post(f"/api/agents/{agent_id}/run", json={"input": "test", "invoking_user": "user:test"})
            assert r.status_code == 200

            with Session(engine) as s:
                result = s.exec(text("""
                    SELECT count(*), avg(duration_ms), percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), sum(cost_usd)
                    FROM run WHERE agent_id = :a AND duration_ms IS NOT NULL
                """), params={"a": agent_id}).first()
            count, avg_ms, p95_ms, total_cost = result
            assert count == 1
            assert avg_ms is not None
            assert p95_ms is not None
            assert total_cost is not None and total_cost >= 0
        finally:
            cleanup_agent(agent_id)


class TestRealCanonicalTypedFieldsNoDualSourceOfTruth:
    """Real tests for section 0.116, GAP_REPORT.

    The real, reported bug: Run/Trace carried both real, typed fields
    (duration_ms, cost_usd, started_at, completed_at) AND legacy,
    independently-computed display strings (duration, cost, ts) —
    genuinely two separate computations that happened to agree today,
    with no real, structural guarantee of staying that way.

    The fix: duration/cost/ts are now real, computed Pydantic
    properties (models.py) derived from the typed fields at response-
    serialization time — never an independently-settable value again.
    Formatting moved to the API/UI layer, exactly as recommended.
    Run/Trace.status also gained a real, enumerated RunStatus/
    TraceStatus and a database-level CHECK constraint.
    """

    def test_a_real_live_run_produces_computed_display_fields_matching_the_typed_ones(self):
        agent_id = make_agent()
        try:
            r = client.post(f"/api/agents/{agent_id}/run", json={"input": "test", "invoking_user": "user:test"})
            assert r.status_code == 200
            run_id = r.json()["run_id"]

            run_resp = client.get(f"/api/runs/{run_id}").json()
            # Real, computed fields present in the real API response,
            # matching the exact legacy shape every existing caller
            # (including this platform's own frontend) already expects.
            assert run_resp["duration"] == f"{run_resp['duration_ms'] / 1000:.1f}s"
            assert run_resp["cost"] == f"${float(run_resp['cost_usd']):.4f}"
            assert run_resp["ts"] is not None
            # The real, underlying legacy columns are genuinely NULL for
            # a brand new row — never independently set by any real
            # write path anymore.
            assert run_resp["duration_legacy"] is None
            assert run_resp["cost_legacy"] is None
            assert run_resp["ts_legacy"] is None
        finally:
            cleanup_agent(agent_id)

    def test_attribute_assignment_on_the_real_computed_property_genuinely_raises(self):
        """A real, direct regression guard for the most dangerous
        failure mode this fix could have shipped with: an old call
        site still doing `run.duration = "..."` must be caught by a
        real AttributeError (no setter), not silently accepted."""
        from app.models import Run
        row = Run(id="regression-test", agent_id="a1", agent_name="x", status="success", hops=1)
        with pytest.raises(AttributeError):
            row.duration = "1.8s"
        with pytest.raises(AttributeError):
            row.cost = "$0.01"
        with pytest.raises(AttributeError):
            row.ts = "12:00:00"

    def test_a_real_historical_row_with_no_typed_fields_falls_back_to_its_own_legacy_value(self):
        """Direct proof of the real, honest fallback for a row that
        predates the typed fields entirely — no fabricated value, the
        row's own real, original display string."""
        from sqlmodel import Session as S
        from app.models import Run
        import uuid
        row_id = f"legacy-test-{uuid.uuid4().hex[:8]}"
        with S(engine) as s:
            s.exec(text(
                "INSERT INTO run (id, agent_id, agent_name, status, hops, duration, cost, ts, "
                "input_tokens, output_tokens, cached_tokens, cost_usd) "
                "VALUES (:id, 'a1', 'x', 'success', 1, '1.8s', '$0.031', '14:22:08', 0, 0, 0, 0)"
            ), params={"id": row_id})
            s.commit()
            row = s.get(Run, row_id)
            assert row.duration == "1.8s"
            assert row.cost == "$0.031"
            assert row.ts == "14:22:08"
            s.exec(text("DELETE FROM run WHERE id = :id"), params={"id": row_id})
            s.commit()

    def test_a_genuinely_zero_cost_new_row_is_not_confused_with_a_historical_row(self):
        """The real, specific bug caught while building this fix:
        cost_usd's own non-nullable Decimal("0") default must not
        cause a genuinely zero-cost new row to be mistaken for a
        historical row with no real cost_usd at all."""
        from decimal import Decimal
        from app.models import Run
        row = Run(id="zero-cost-test", agent_id="a1", agent_name="x", status="success", hops=1, cost_usd=Decimal("0"))
        assert row.cost == "$0.0000"

    def test_the_real_database_check_constraint_rejects_an_invalid_run_status(self):
        with Session(engine) as s:
            with pytest.raises(Exception):
                s.exec(text(
                    "INSERT INTO run (id, agent_id, agent_name, status, hops, created_at, "
                    "input_tokens, output_tokens, cached_tokens, cost_usd) "
                    "VALUES ('bad-status-test-1', 'a1', 'x', 'not_a_real_status', 1, now(), 0, 0, 0, 0)"
                ))
                s.commit()
            s.rollback()

    def test_the_real_database_check_constraint_rejects_an_invalid_trace_status(self):
        with Session(engine) as s:
            with pytest.raises(Exception):
                s.exec(text(
                    "INSERT INTO trace (id, agent_id, agent_name, version, principal, plane, spans, "
                    "status, cost_usd) VALUES ('bad-status-test-2', 'a1', 'x', 'v1', 'p', 'App', 0, "
                    "'not_a_real_status', 0)"
                ))
                s.commit()
            s.rollback()

    def test_every_real_produced_run_status_is_a_member_of_the_real_enum(self):
        from app.models import RunStatus
        for value in ("success", "failed", "denied", "blocked", "paused", "running"):
            assert value in {s.value for s in RunStatus}, f"{value} is a real, produced Run.status but missing from RunStatus"

    def test_every_real_produced_trace_status_is_a_member_of_the_real_enum(self):
        from app.models import TraceStatus
        for value in ("ok", "ucs_denial", "harness_fail", "orch_fail", "paused", "moderation_blocked"):
            assert value in {s.value for s in TraceStatus}, f"{value} is a real, produced Trace.status but missing from TraceStatus"
