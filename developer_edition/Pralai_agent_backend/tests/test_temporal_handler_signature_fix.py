"""
Real tests for the Temporal handler-signature bug fix (section 0.51, GAP_REPORT).

The real bug: temporal_workflow.py's execute_node_activity kept its own,
separate hardcoded tuple of which node types need a real DB Session
passed to their handler. That tuple was missing "llm",
"variable_assigner", "parameter_extractor", and "agent" - all four
handlers genuinely require session as a real positional argument (see
their own real signatures in harness.py), so a durable-path flow
reaching any of them raised a real TypeError at runtime.

These tests call the REAL, exact function that had the bug -
execute_node_activity itself, directly, not a re-implementation of its
logic - for all four previously-broken node types, proving the actual
reported code path now works.
"""
import sys
import os
import uuid
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session

from app.harness import NODE_HANDLERS, SESSION_REQUIRED_NODE_TYPES, call_node_handler
from app.temporal_workflow import execute_node_activity
from app.models import Agent


@pytest.fixture()
def session():
    from app.db import engine
    with Session(engine) as s:
        yield s


@pytest.fixture()
def agent(session):
    a = Agent(
        id=f"test-temporal-fix-{uuid.uuid4().hex[:10]}", name="Test", type="App", subtype="Workflow", plane="App",
        owner="you", status="published", version="v1", app_type="workflow", flow={"nodes": [], "edges": []},
    )
    session.add(a)
    session.commit()
    return a


def run_async(coro):
    return asyncio.run(coro)


def make_ctx(agent_id: str) -> dict:
    return {"input": "hello", "vars": {}, "agent_id": agent_id, "invoking_user": "user:test", "output": None}


class TestRealSessionRequiredNodeTypes:
    def test_includes_all_nine_real_session_requiring_types(self):
        expected = {"gov", "tool", "memory", "retrieval", "subagent", "llm", "variable_assigner", "parameter_extractor", "agent"}
        assert SESSION_REQUIRED_NODE_TYPES == frozenset(expected)

    def test_the_four_types_the_real_bug_report_named_are_all_present(self):
        for t in ("llm", "agent", "variable_assigner", "parameter_extractor"):
            assert t in SESSION_REQUIRED_NODE_TYPES


class TestRealCallNodeHandlerDispatch:
    def test_a_session_required_type_genuinely_receives_the_real_session(self, session, agent):
        ctx = make_ctx(agent.id)
        cfg = {"assignments": [{"variable": "status", "operation": "set", "value": "active"}]}
        result = call_node_handler("variable_assigner", cfg, ctx, session)
        assert result["vars"]["conv_status"] == "active"

    def test_a_non_session_type_is_called_without_it_and_still_works(self):
        ctx = make_ctx("irrelevant-for-this-type")
        cfg = {"content": "Hello {{input}}"}
        result = call_node_handler("answer", cfg, ctx, None)
        assert result["output"] == "Hello hello"

    def test_an_unknown_node_type_returns_none_rather_than_raising(self, session):
        ctx = make_ctx("irrelevant")
        assert call_node_handler("not_a_real_type", {}, ctx, session) is None


class TestRealTemporalActivityForPreviouslyBrokenTypes:
    def test_llm_node_no_longer_raises_typeerror_on_the_durable_path(self, agent):
        ctx = make_ctx(agent.id)
        node = {"id": "n1", "type": "llm", "config": {"system_prompt": "Be brief.", "user_prompt": "{{input}}"}}
        result = run_async(execute_node_activity(node, ctx))
        assert result["_span"]["status"] == "ok"
        assert "llm_output" in result["vars"]

    def test_agent_node_no_longer_raises_typeerror_on_the_durable_path(self, agent):
        ctx = make_ctx(agent.id)
        node = {"id": "n1", "type": "agent", "config": {"strategy": "plan_execute", "query": "{{input}}"}}
        result = run_async(execute_node_activity(node, ctx))
        assert result["_span"]["status"] == "ok"

    def test_variable_assigner_node_no_longer_raises_typeerror_on_the_durable_path(self, agent):
        ctx = make_ctx(agent.id)
        node = {"id": "n1", "type": "variable_assigner", "config": {"assignments": [
            {"variable": "count", "operation": "set", "value": 1},
        ]}}
        result = run_async(execute_node_activity(node, ctx))
        assert result["_span"]["status"] == "ok"
        assert result["vars"]["conv_count"] == 1

    def test_parameter_extractor_node_no_longer_raises_typeerror_on_the_durable_path(self, agent):
        ctx = make_ctx(agent.id)
        node = {"id": "n1", "type": "parameter_extractor", "config": {
            "input_variable": "input",
            "parameters": [{"name": "topic", "type": "string", "description": "the topic", "required": False}],
        }}
        result = run_async(execute_node_activity(node, ctx))
        assert result["_span"]["status"] == "ok"

    def test_a_real_already_working_session_type_still_works_after_the_refactor(self, agent):
        ctx = make_ctx(agent.id)
        node = {"id": "n1", "type": "gov", "config": {}}
        result = run_async(execute_node_activity(node, ctx))
        assert result["_span"]["status"] == "ok"

    def test_a_real_non_session_type_still_works_after_the_refactor(self, agent):
        ctx = make_ctx(agent.id)
        node = {"id": "n1", "type": "answer", "config": {"content": "Hi {{input}}"}}
        result = run_async(execute_node_activity(node, ctx))
        assert result["_span"]["status"] == "ok"
        assert result["output"] == "Hi hello"


class TestRealDispatchTableCompleteness:
    def test_every_session_required_type_has_a_real_registered_handler(self):
        for t in SESSION_REQUIRED_NODE_TYPES:
            assert t in NODE_HANDLERS, f"{t} is in SESSION_REQUIRED_NODE_TYPES but has no real NODE_HANDLERS entry"

    def test_every_session_required_handler_genuinely_accepts_three_positional_args(self):
        import inspect
        for t in SESSION_REQUIRED_NODE_TYPES:
            fn = NODE_HANDLERS[t]
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            assert len(params) >= 3, f"handle_{t} is marked session-required but its real signature only takes {len(params)} params"
