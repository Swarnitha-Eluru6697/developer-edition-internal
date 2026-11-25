"""The LLM node's "Agentic strategy" control, genuinely wired.

Reported: the dropdown showed one option and was disabled. Both options
were in the markup, but the <select> was disabled — correctly, because
handle_llm never read `agentic_strategy`. It was registered in
node_wiring.UNWIRED_CONTROLS precisely so the Builder would disable it
rather than let it look functional, and handle_llm's docstring said
wiring it "would need this handler to replicate handle_agent's own real
multi-step tool-calling loop".

Replicating it is what these tests exist to prevent. The loop was lifted
out of handle_agent into run_react_loop / run_function_calling_loop, and
BOTH nodes call it — so enabling the control added a caller, not a second
copy that could drift.

The other half of the fix is backward compatibility: `mode: "agentic"`
has always meant ONE call in this node. A flow saved before this change
must keep making one call, not silently become a multi-round loop whose
cost is real tokens per round.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session

from app import harness
from app.db import engine
from app.harness import _llm_agentic_strategy, handle_llm
from app.node_wiring import unwired_controls_for


AGENT_ID = "a-strategy-test"


@pytest.fixture
def session():
    """A real Agent row, because the strategy path writes real AgentLog
    rows and those carry a foreign key to it — the same logging the
    Agent node has always done, which is where this loop came from."""
    from sqlalchemy import text as sql_text

    from app.models import Agent

    with Session(engine) as s:
        if not s.get(Agent, AGENT_ID):
            s.add(Agent(id=AGENT_ID, name="Strategy test", type="Workflow", subtype="Workflow",
                        plane="Control", owner="tester", app_type="workflow",
                        flow={"nodes": [], "edges": []}))
            s.commit()
    with Session(engine) as s:
        yield s
    with Session(engine) as s:
        s.exec(sql_text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": AGENT_ID})
        s.exec(sql_text("DELETE FROM agent WHERE id = :a"), params={"a": AGENT_ID})
        s.commit()


def _ctx():
    return {"input": "summarise the outage", "vars": {}, "agent_id": AGENT_ID,
            "agent_name": "Strategy test", "invoking_user": "tester", "output": None}


@pytest.fixture
def calls(monkeypatch):
    """Records every model call the node makes, and answers them."""
    recorded = []

    def fake(ctx, prompt=None, model=None, system=None, tools=None, tool_choice=None, **kw):
        recorded.append({"prompt": prompt, "system": system, "tools": tools,
                         "tool_choice": tool_choice, "model": model})
        # Concludes on the second round so ReAct terminates naturally
        # rather than running to the cap.
        text = "Thought: I have enough.\nFinal Answer: done" if len(recorded) >= 2 else "Thought: thinking"
        return {"text": text, "model": model or "test", "mock": False, "usage": {}}

    monkeypatch.setattr(harness, "_call_llm_tracked", fake)
    return recorded


class TestTheControlIsActuallyRead:
    def test_react_runs_a_real_multi_round_loop(self, session, calls):
        ctx = handle_llm({"mode": "agentic", "agentic_strategy": "react", "step_limit": 5},
                         _ctx(), session)
        assert len(calls) == 2, "ReAct did not loop — it made a single call"
        assert ctx["output"] == "done"
        assert ctx["vars"]["agent_strategy"] == "react"
        assert ctx["vars"]["agent_iteration_count"] == 2
        assert len(ctx["vars"]["agent_reasoning_trace"]) == 2
        # The run response and the Builder's trace view read agent_steps,
        # not agent_reasoning_trace — setting only the latter made a real
        # loop report "0 reasoning steps" in the live API.
        assert ctx["vars"]["agent_steps"] == ctx["vars"]["agent_reasoning_trace"]

    def test_function_calling_offers_the_configured_tools(self, session, calls):
        handle_llm({"mode": "agentic", "agentic_strategy": "function_calling",
                    "allowed_tools": ["tool:Trino:query"], "step_limit": 3}, _ctx(), session)
        assert calls, "no model call was made"
        assert calls[0]["tools"], "function calling sent no tool schema"
        assert calls[0]["tool_choice"] == {"type": "auto"}
        # Sanitised for the API: ":" is not a valid tool-name character.
        assert calls[0]["tools"][0]["name"] == "tool_Trino_query"

    def test_function_calling_with_no_tools_still_answers_honestly(self, session, calls):
        """Degrades to a direct answer rather than pretending a tool loop
        ran — and must not send an empty tool schema, which the API
        rejects."""
        handle_llm({"mode": "agentic", "agentic_strategy": "function_calling", "step_limit": 3},
                   _ctx(), session)
        assert calls[0]["tools"] is None
        assert calls[0]["tool_choice"] is None

    def test_the_step_limit_bounds_the_rounds(self, session, monkeypatch):
        """Every round is a real model call, so an unbounded loop is a
        real bill. ReAct that never concludes must stop at the cap."""
        recorded = []

        def never_concludes(ctx, prompt=None, model=None, **kw):
            recorded.append(prompt)
            return {"text": "Thought: still going", "model": "test", "mock": False, "usage": {}}

        monkeypatch.setattr(harness, "_call_llm_tracked", never_concludes)
        ctx = handle_llm({"mode": "agentic", "agentic_strategy": "react", "step_limit": 3},
                         _ctx(), session)
        assert len(recorded) == 3
        assert ctx["vars"]["agent_success"] is False


class TestBackwardCompatibility:
    def test_agentic_mode_without_a_strategy_still_makes_one_call(self, session, calls):
        """The behaviour every existing flow has. Turning this into a
        loop silently would multiply the token cost of flows nobody
        touched."""
        handle_llm({"mode": "agentic"}, _ctx(), session)
        assert len(calls) == 1

    def test_fixed_mode_ignores_a_strategy_entirely(self, session, calls):
        handle_llm({"mode": "fixed", "agentic_strategy": "react"}, _ctx(), session)
        assert len(calls) == 1

    def test_the_older_stored_label_form_still_resolves(self):
        """The dropdown used to store the option TEXT. A flow saved then
        must still select the strategy it shows, not fall back to a
        single call."""
        assert _llm_agentic_strategy({"agentic_strategy": "ReAct (reason + act loop)"}) == "react"
        assert _llm_agentic_strategy({"agentic_strategy": "Function calling"}) == "function_calling"
        assert _llm_agentic_strategy({"agentic_strategy": "react"}) == "react"

    def test_an_unrecognised_value_means_no_loop(self):
        assert _llm_agentic_strategy({"agentic_strategy": "magic"}) is None
        assert _llm_agentic_strategy({}) is None
        assert _llm_agentic_strategy({"agentic_strategy": ""}) is None


class TestTheUiIsNoLongerToldToDisableIt:
    def test_agentic_strategy_and_allowed_tools_are_no_longer_unwired(self):
        unwired = unwired_controls_for("llm")
        assert "agentic_strategy" not in unwired
        assert "allowed_tools" not in unwired

    def test_the_genuinely_unwired_controls_are_still_declared(self):
        """Removing the whole list would be the opposite error — controls
        that really do nothing must stay disabled."""
        unwired = unwired_controls_for("llm")
        for still_unwired in ("vision", "structured_output", "reasoning_tag_separation"):
            assert still_unwired in unwired


class TestOneLoopNotTwo:
    def test_both_nodes_call_the_same_functions(self):
        import inspect

        src = inspect.getsource(harness.handle_agent)
        assert "run_react_loop(" in src and "run_function_calling_loop(" in src
        llm_src = inspect.getsource(harness._run_llm_agentic_strategy)
        assert "run_react_loop(" in llm_src and "run_function_calling_loop(" in llm_src

    def test_the_loops_are_module_level_not_nested_in_a_handler(self):
        assert callable(harness.run_react_loop)
        assert callable(harness.run_function_calling_loop)
