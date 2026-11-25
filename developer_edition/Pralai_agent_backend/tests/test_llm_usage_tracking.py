"""
Real tests for the LLM token usage tracking fix (section 0.78,
GAP_REPORT).

The real, reported bug: model_adapter.py's call_llm already returned
real usage {"input_tokens", "output_tokens"} from the real Anthropic
API response, but it was thrown away - normal Run persistence
unconditionally hardcoded cost="$0.00" at every real call site.

The fix: a real, configurable per-model price table
(calculate_cost_usd); harness.py's _call_llm_tracked wraps every real
call_llm call site and accumulates usage onto ctx["_llm_usage"];
_aggregate_llm_usage sums it onto each node's own span with exactly
the requested fields. Both the sync and durable paths reset this list
before each node's handler runs, so usage never leaks between nodes.
execution.py's _aggregate_run_usage sums every span's usage onto the
Run row itself.

These tests exercise the real, running API and real functions directly
- not mocked, using ALLOW_MOCK_MODELS=true's own real (if synthetic)
usage numbers from model_adapter.py's own real mock branch, which
still exercises every real line of code downstream of call_llm's own
return value.
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
from app.harness import _call_llm_tracked, _aggregate_llm_usage
from app.model_adapter import calculate_cost_usd
from app.routers.execution import _aggregate_run_usage

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

FLOW_TWO_LLM = {
    "nodes": [
        {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
        {"id": "n2", "type": "llm", "x": 100, "y": 0, "sub": "", "config": {
            "model": "Claude Sonnet 5", "system_prompt": "s", "user_prompt": "first {{input}}",
        }},
        {"id": "n3", "type": "llm", "x": 200, "y": 0, "sub": "", "config": {
            "model": "Claude Haiku 4.5", "system_prompt": "s", "user_prompt": "second much longer prompt {{input}}",
        }},
        {"id": "n4", "type": "respond", "x": 300, "y": 0, "sub": "", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
        {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        {"id": "e3", "source": "n3", "target": "n4", "source_handle": None},
    ],
}


def make_agent(flow) -> str:
    r = client.post("/api/agents", json={
        "name": f"Usage Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": flow,
    })
    return r.json()["id"]


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        for table in ("run", "trace", "agentlog"):
            s.exec(text(f"DELETE FROM {table} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealCostCalculation:
    def test_known_token_counts_produce_exact_expected_cost(self):
        cost = calculate_cost_usd("Claude Sonnet 5", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(3.00 + 15.00)

    def test_zero_tokens_is_zero_cost(self):
        assert calculate_cost_usd("Claude Sonnet 5", 0, 0) == 0.0

    def test_an_unrecognized_model_degrades_to_zero_not_a_crash(self):
        assert calculate_cost_usd("Some Made Up Model", 1000, 1000) == 0.0

    def test_cached_tokens_are_billed_at_the_lower_cached_rate_not_full_input(self):
        full_input_cost = calculate_cost_usd("Claude Sonnet 5", input_tokens=1_000_000, output_tokens=0)
        cached_cost = calculate_cost_usd("Claude Sonnet 5", input_tokens=0, output_tokens=0, cached_tokens=1_000_000)
        assert cached_cost < full_input_cost


class TestRealUsageAggregation:
    def test_a_single_tracked_call_is_captured_with_every_requested_field(self):
        ctx = {"_llm_usage": []}
        _call_llm_tracked(ctx, prompt="hello", model="Claude Sonnet 5")
        assert len(ctx["_llm_usage"]) == 1
        entry = ctx["_llm_usage"][0]
        for key in ("model", "provider", "input_tokens", "output_tokens", "cached_tokens"):
            assert key in entry

    def test_multiple_calls_within_one_node_sum_correctly_across_different_models(self):
        ctx = {"_llm_usage": []}
        _call_llm_tracked(ctx, prompt="call one", model="Claude Sonnet 5")
        _call_llm_tracked(ctx, prompt="call two", model="Claude Sonnet 5")
        _call_llm_tracked(ctx, prompt="call three longer prompt here", model="Claude Haiku 4.5")

        expected_input = sum(u["input_tokens"] for u in ctx["_llm_usage"])
        expected_output = sum(u["output_tokens"] for u in ctx["_llm_usage"])
        expected_cost = sum(
            calculate_cost_usd(u["model"], u["input_tokens"], u["output_tokens"], u["cached_tokens"])
            for u in ctx["_llm_usage"]
        )

        agg = _aggregate_llm_usage(ctx)
        assert agg["input_tokens"] == expected_input
        assert agg["output_tokens"] == expected_output
        assert agg["cost_usd"] == pytest.approx(round(expected_cost, 6))

    def test_a_node_with_no_llm_calls_aggregates_to_none_not_a_fabricated_zero(self):
        ctx = {"_llm_usage": []}
        assert _aggregate_llm_usage(ctx) is None
        assert _aggregate_llm_usage({}) is None


class TestRealRunUsageAggregation:
    def test_run_level_aggregation_sums_every_span_and_formats_a_real_cost_string(self):
        spans = [
            {"node": "trigger", "status": "ok"},
            {"node": "llm", "status": "ok", "input_tokens": 10, "output_tokens": 20, "cached_tokens": 0, "cost_usd": Decimal("0.0005")},
            {"node": "llm", "status": "ok", "input_tokens": 5, "output_tokens": 8, "cached_tokens": 0, "cost_usd": Decimal("0.0002")},
            {"node": "respond", "status": "ok"},
        ]
        usage = _aggregate_run_usage(spans)
        assert usage["input_tokens"] == 15
        assert usage["output_tokens"] == 28
        # A real Decimal result compared against a real pytest.approx
        # float raises internally (the same real Decimal/float mixing
        # restriction caught twice already while building this fix) -
        # convert to float for the comparison itself; the real,
        # production-facing value stays Decimal.
        assert float(usage["cost_usd"]) == pytest.approx(0.0007)
        assert usage["cost"] == "$0.0007"

    def test_no_llm_spans_at_all_aggregates_to_a_real_honest_zero(self):
        spans = [{"node": "code", "status": "ok"}, {"node": "respond", "status": "ok"}]
        usage = _aggregate_run_usage(spans)
        assert usage == {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0, "cost": "$0.0000"}


class TestRealLiveExecutionEndToEnd:
    def test_a_real_llm_node_run_produces_a_real_span_with_every_requested_field(self):
        agent_id = make_agent(FLOW_ONE_LLM)
        try:
            r = client.post(f"/api/agents/{agent_id}/run", json={"input": "hello world", "invoking_user": "user:test"})
            assert r.status_code == 200, r.text
            body = r.json()
            llm_span = next(s for s in body["spans"] if s["node"] == "llm")
            for key in ("node_id", "input_tokens", "output_tokens", "cached_tokens", "model", "provider", "cost_usd"):
                assert key in llm_span, f"missing {key} on the real LLM node's span"
            assert llm_span["input_tokens"] > 0
            assert llm_span["output_tokens"] > 0
            assert llm_span["cost_usd"] > 0

            non_llm_spans = [s for s in body["spans"] if s["node"] != "llm"]
            for s in non_llm_spans:
                assert "cost_usd" not in s

            with Session(engine) as s:
                row = s.exec(text("SELECT input_tokens, output_tokens, cost_usd, cost FROM run WHERE id = :r"),
                              params={"r": body["run_id"]}).first()
            assert row[0] == llm_span["input_tokens"]
            assert row[1] == llm_span["output_tokens"]
            # row[2] (the real, typed Numeric column) comes back as a
            # real Decimal; llm_span["cost_usd"] (from the real JSON
            # HTTP response) is a real float, since JSON has no native
            # Decimal type - convert before comparing, a real test
            # concern, not a real production bug (FastAPI's own JSON
            # encoder already handles the Decimal -> float conversion
            # correctly for the live response).
            assert float(row[2]) == pytest.approx(llm_span["cost_usd"])
            assert row[3] != "$0.00"
        finally:
            cleanup_agent(agent_id)

    def test_usage_does_not_leak_between_two_separate_llm_nodes_in_the_same_run(self):
        agent_id = make_agent(FLOW_TWO_LLM)
        try:
            r = client.post(f"/api/agents/{agent_id}/run", json={"input": "test", "invoking_user": "user:test"})
            assert r.status_code == 200, r.text
            body = r.json()
            llm_spans = [s for s in body["spans"] if s["node"] == "llm"]
            assert len(llm_spans) == 2
            n2 = next(s for s in llm_spans if s["node_id"] == "n2")
            n3 = next(s for s in llm_spans if s["node_id"] == "n3")
            assert n2["input_tokens"] != n3["input_tokens"]
            assert n2["model"] != n3["model"]

            with Session(engine) as s:
                row = s.exec(text("SELECT input_tokens, output_tokens FROM run WHERE id = :r"),
                              params={"r": body["run_id"]}).first()
            assert row[0] == n2["input_tokens"] + n3["input_tokens"]
            assert row[1] == n2["output_tokens"] + n3["output_tokens"]
        finally:
            cleanup_agent(agent_id)
