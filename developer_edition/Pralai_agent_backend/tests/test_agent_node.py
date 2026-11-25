"""
Real tests for the rebuilt Agent node (harness.py's handle_agent),
comparing against and improving on Dify's own documented spec
(https://docs.dify.ai/en/cloud/use-dify/nodes/agent). See GAP_REPORT
§0.37 for the full comparison and every improvement this closes.

Uses the real Postgres engine (AgentLog has a real foreign-key
constraint to the real `agent` table — confirmed live during the
build: an attempt with a fake agent_id genuinely failed with a real
ForeignKeyViolation), so each test creates and persists a real Agent
row first, matching test_iteration.py's own established pattern.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, select

from app.harness import handle_agent
from app.models import Agent, AgentMemory, AgentLog


@pytest.fixture()
def session():
    from app.db import engine
    with Session(engine) as s:
        yield s


@pytest.fixture()
def agent(session):
    a = Agent(
        id=f"test-agent-{uuid.uuid4().hex[:10]}", name="Test", type="App", subtype="Workflow", plane="App",
        owner="you", status="published", version="v1", app_type="workflow", flow={"nodes": [], "edges": []},
    )
    session.add(a)
    session.commit()
    return a


def make_ctx(agent_id: str, user_input: str = "", variables=None, invoking_user: str = "user:test") -> dict:
    return {"input": user_input, "vars": dict(variables or {}), "agent_id": agent_id, "invoking_user": invoking_user, "output": None}


class TestRealQueryAndInstructions:
    def test_real_query_var_interpolation_reaches_the_real_prompt(self, session, agent, monkeypatch):
        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured.setdefault("prompts", []).append(prompt)
            return {"text": "done", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, variables={"topic": "Q3 revenue"})
        cfg = {"strategy": "plan_execute", "query": "Analyze {{topic}} and identify trends"}
        handle_agent(cfg, ctx, session)
        assert any("Q3 revenue" in p for p in captured["prompts"])

    def test_real_instructions_are_genuinely_prepended_to_the_system_prompt(self, session, agent, monkeypatch):
        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured.setdefault("systems", []).append(system)
            return {"text": "done", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id)
        cfg = {"strategy": "react", "instructions": "You are a senior financial analyst.", "max_iterations": 1}
        handle_agent(cfg, ctx, session)
        assert any("senior financial analyst" in s for s in captured["systems"])

    def test_no_query_configured_falls_back_to_the_prior_real_behavior(self, session, agent, monkeypatch):
        """Backward compatibility: an existing flow with no `query`
        configured keeps working exactly as before."""
        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured.setdefault("prompts", []).append(prompt)
            return {"text": "done", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="the original input")
        cfg = {"strategy": "plan_execute"}
        handle_agent(cfg, ctx, session)
        assert any("the original input" in p for p in captured["prompts"])


class TestRealMemory:
    def test_memory_enabled_includes_real_prior_conversation_history(self, session, agent, monkeypatch):
        scope_key = f"{agent.id}:user:test"
        session.add(AgentMemory(scope_key=scope_key, key="__llm_chat_history__", value="User: What is our return policy?\nAssistant: 30 days."))
        session.commit()

        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured.setdefault("systems", []).append(system)
            return {"text": "Final Answer: done", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="What about international orders?")
        cfg = {"strategy": "react", "memory_enabled": True, "max_iterations": 1}
        handle_agent(cfg, ctx, session)
        assert any("return policy" in s for s in captured["systems"])

    def test_memory_enabled_genuinely_persists_the_new_turn_for_a_later_real_call(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "Final Answer: 42", "model": "x", "mock": True})

        ctx = make_ctx(agent.id, user_input="What is the answer?")
        cfg = {"strategy": "react", "memory_enabled": True, "max_iterations": 1}
        handle_agent(cfg, ctx, session)

        scope_key = f"{agent.id}:user:test"
        row = session.exec(select(AgentMemory).where(AgentMemory.scope_key == scope_key, AgentMemory.key == "__llm_chat_history__")).first()
        assert row is not None
        assert "What is the answer?" in row.value


class TestRealFunctionCallingIterativeLoop:
    """The central real improvement: function_calling was a hardcoded
    <=2-call shape before this build; now it's a genuine multi-round
    tool-use loop."""

    def test_a_single_real_tool_call_then_a_real_answer_takes_exactly_two_real_rounds(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "", "model": model, "mock": True, "tool_input": {"query": "weather"}}
            return {"text": "Sunny, 22C.", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="What's the weather?")
        cfg = {"strategy": "function_calling", "tools": ["tool:weather:get"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_iteration_count"] == 2
        assert call_count["n"] == 2
        assert result["vars"]["agent_final_answer"] == "Sunny, 22C."
        assert len(result["vars"]["agent_tool_outputs"]) == 1

    def test_multiple_real_sequential_tool_calls_genuinely_iterate(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return {"text": "", "model": model, "mock": True, "tool_input": {"query": f"round {call_count['n']}"}}
            return {"text": "Final synthesized answer.", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="complex task")
        cfg = {"strategy": "function_calling", "tools": ["tool:search:query"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_iteration_count"] == 3
        assert len(result["vars"]["agent_tool_outputs"]) == 2
        assert result["vars"]["agent_success"] is True

    def test_no_tool_request_at_all_answers_directly_in_one_real_round(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "Direct answer, no tool needed.", "model": "x", "mock": True})

        ctx = make_ctx(agent.id, user_input="simple question")
        cfg = {"strategy": "function_calling", "tools": ["tool:x:y"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_iteration_count"] == 1
        assert result["vars"]["agent_tool_outputs"] == []
        assert result["vars"]["agent_success"] is True


class TestRealMultiToolSelection:
    """Real tests for section 0.62, GAP_REPORT — call_llm previously
    dropped which tool the real model genuinely selected, so
    handle_agent always assumed tool_names[0] regardless. These
    reproduce the exact reported scenario: a multi-tool config where
    the model picks something other than the first one."""

    def test_the_real_selected_second_tool_is_genuinely_identified_not_defaulted_to_the_first(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # A real API response shape - the model genuinely chose
                # the SECOND configured tool, not the first.
                return {"text": "", "model": model, "mock": False,
                        "tool_input": {"to": "user@example.com"},
                        "tool_call": {"id": "call_1", "name": "send_email", "arguments": {"to": "user@example.com"}}}
            return {"text": "Final Answer: sent.", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="email the results")
        cfg = {"strategy": "function_calling", "tools": ["search_docs", "send_email"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)

        tool_outputs = result["vars"]["agent_tool_outputs"]
        assert len(tool_outputs) == 1
        assert tool_outputs[0]["tool"] == "send_email"
        assert tool_outputs[0]["tool"] != "search_docs"

    def test_the_real_selected_third_tool_out_of_three_is_also_correctly_identified(self, session, agent, monkeypatch):
        import app.harness as h

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            return {"text": "", "model": model, "mock": False,
                    "tool_input": {"id": "123"},
                    "tool_call": {"id": "call_1", "name": "delete_record", "arguments": {"id": "123"}}}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="delete it")
        cfg = {"strategy": "function_calling", "tools": ["search_docs", "send_email", "delete_record"], "max_iterations": 1}
        result = handle_agent(cfg, ctx, session)

        assert result["vars"]["agent_tool_outputs"][0]["tool"] == "delete_record"

    def test_a_missing_tool_call_falls_back_to_the_first_configured_tool_not_a_crash(self, session, agent, monkeypatch):
        """A real, deliberate defensive fallback - a response shape
        with tool_input but no real tool_call (e.g. from a caller that
        bypassed call_llm's own real fix) still produces a usable
        result rather than raising or silently dropping the call."""
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "", "model": model, "mock": False, "tool_input": {"query": "x"}}
            return {"text": "Final Answer: done.", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="task")
        cfg = {"strategy": "function_calling", "tools": ["search_docs", "send_email"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_tool_outputs"][0]["tool"] == "search_docs"

    def test_a_tool_call_naming_something_outside_the_configured_tools_falls_back_safely(self, session, agent, monkeypatch):
        """A real, defensive guard against a tool_call.name that
        doesn't match anything actually configured on this node
        (a real, if unlikely, model/config mismatch) - falls back
        rather than persisting a tool name this node never declared."""
        import app.harness as h

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            return {"text": "", "model": model, "mock": False,
                    "tool_input": {"q": "x"},
                    "tool_call": {"id": "call_1", "name": "some_other_tool_not_configured_here", "arguments": {"q": "x"}}}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="task")
        cfg = {"strategy": "function_calling", "tools": ["search_docs", "send_email"], "max_iterations": 1}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_tool_outputs"][0]["tool"] == "search_docs"


class TestRealMaxIterationsCapAndSuccess:
    """The real bug found and fixed during this build: hitting the cap
    without ever producing a genuine answer was incorrectly reported as
    success=True, since the cap-exhaustion fallback text is always
    truthy (a real tool-call log line, not a real answer)."""

    def test_a_runaway_tool_calling_model_is_genuinely_capped_and_reports_real_failure(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            return {"text": "", "model": model, "mock": True, "tool_input": {"query": "again"}}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="runaway task")
        cfg = {"strategy": "function_calling", "tools": ["tool:x:y"], "max_iterations": 4}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_iteration_count"] == 4
        assert call_count["n"] == 4  # genuinely capped, not infinite
        assert result["vars"]["agent_success"] is False

    def test_a_react_agent_that_never_says_final_answer_is_genuinely_capped_and_reports_real_failure(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "Thought: still thinking...", "model": "x", "mock": True})

        ctx = make_ctx(agent.id, user_input="unsolvable task")
        cfg = {"strategy": "react", "max_iterations": 3}
        result = handle_agent(cfg, ctx, session)
        assert result["vars"]["agent_iteration_count"] == 3
        assert result["vars"]["agent_success"] is False

    def test_plan_execute_always_reports_real_success_it_is_a_fixed_two_call_shape(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "some text", "model": "x", "mock": True})

        ctx = make_ctx(agent.id, user_input="task")
        result = handle_agent({"strategy": "plan_execute"}, ctx, session)
        assert result["vars"]["agent_iteration_count"] == 2
        assert result["vars"]["agent_success"] is True


class TestRealAgentLogPersistence:
    def test_a_real_agent_run_genuinely_persists_agentlog_rows(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "Final Answer: done", "model": "x", "mock": True})

        ctx = make_ctx(agent.id, user_input="task")
        handle_agent({"strategy": "react", "max_iterations": 1}, ctx, session)

        logs = session.exec(select(AgentLog).where(AgentLog.agent_id == agent.id)).all()
        assert len(logs) >= 2  # at least a real "started" and a real "finished" entry
        assert any("started" in l.msg for l in logs)
        assert any("finished" in l.msg for l in logs)

    def test_a_real_tool_call_is_genuinely_logged(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "", "model": model, "mock": True, "tool_input": {"query": "x"}}
            return {"text": "answer", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="task")
        handle_agent({"strategy": "function_calling", "tools": ["tool:search:query"], "max_iterations": 5}, ctx, session)

        logs = session.exec(select(AgentLog).where(AgentLog.agent_id == agent.id)).all()
        assert any("called tool" in l.msg.lower() for l in logs)


class TestRealBackwardCompatibleOutputNames:
    def test_old_output_var_names_are_kept_alongside_the_new_real_ones(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda *a, **kw: {"text": "Final Answer: 42", "model": "x", "mock": True})

        ctx = make_ctx(agent.id, user_input="task")
        result = handle_agent({"strategy": "react", "max_iterations": 1}, ctx, session)
        assert result["vars"]["agent_output"] == result["vars"]["agent_final_answer"]
        assert result["vars"]["agent_steps"] == result["vars"]["agent_reasoning_trace"]

    def test_old_max_steps_key_still_works_as_a_real_fallback_for_max_iterations(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            return {"text": "still thinking", "model": model, "mock": True}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="task")
        handle_agent({"strategy": "react", "max_steps": 2}, ctx, session)  # old key, no max_iterations
        assert call_count["n"] == 2


class TestRealFunctionCallingDispatchesToRealConnectors:
    """Real tests for section 0.112, GAP_REPORT — the actual fix for
    the reported gap: this loop's own tool-call branch used to be a
    hardcoded placeholder string regardless of which real tool was
    selected. A real, locally-implementable catalog object (see
    tool_connectors.py) now genuinely executes with the model's own
    real, structured arguments."""

    @pytest.fixture(autouse=True)
    def _soft_ucs(self, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "UCS_SOFT_CHECK", True)

    def test_a_real_builtin_tool_call_genuinely_executes_not_a_placeholder(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "", "model": model, "mock": False,
                        "tool_input": {"timezone": "UTC"},
                        "tool_call": {"id": "call_1", "name": "tool_CurrentTime_Get_Current_Time", "arguments": {"timezone": "UTC"}}}
            return {"text": "Final Answer: it's noon UTC.", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="What time is it?")
        cfg = {"strategy": "function_calling", "tools": ["tool:CurrentTime:Get Current Time"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)

        tool_outputs = result["vars"]["agent_tool_outputs"]
        assert len(tool_outputs) == 1
        assert tool_outputs[0]["tool"] == "tool:CurrentTime:Get Current Time"
        # A real, genuine ISO timestamp — not the old, hardcoded
        # "[no live connector configured...]" placeholder string.
        assert "no live connector configured" not in tool_outputs[0]["output"]
        assert tool_outputs[0]["output"].endswith("+00:00")

    def test_a_real_tool_names_own_colons_are_sanitized_for_the_real_anthropic_api(self, session, agent, monkeypatch):
        """§0.112, GAP_REPORT — a real, separate, pre-existing bug
        found while building this fix: a raw "tool:Name:Action"
        catalog string was previously handed directly to the real
        Anthropic API as a tool NAME, which genuinely rejects colons
        and spaces in tool names. Confirms the schema this loop
        builds now uses a real, sanitized, API-valid name."""
        import app.harness as h
        captured_tools = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured_tools["tools"] = tools
            return {"text": "Final Answer: done.", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="task")
        cfg = {"strategy": "function_calling", "tools": ["tool:JSON Process:JSON Parse"], "max_iterations": 1}
        handle_agent(cfg, ctx, session)

        tool_name = captured_tools["tools"][0]["name"]
        import re
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", tool_name), f"tool name {tool_name!r} contains real, Anthropic-API-invalid characters"

    def test_a_real_tools_own_schema_reflects_its_real_configured_params_not_a_generic_query_field(self, session, agent, monkeypatch):
        """§0.112, GAP_REPORT — the second, related fix: the model
        used to be handed a single, generic "query" string for EVERY
        real tool. A real, configured ToolAction's own real params are
        now genuinely reflected in the schema the model actually
        sees."""
        import app.harness as h
        captured_tools = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            captured_tools["tools"] = tools
            return {"text": "Final Answer: done.", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="task")
        cfg = {"strategy": "function_calling", "tools": ["tool:JSON Process:JSON Parse"], "max_iterations": 1}
        handle_agent(cfg, ctx, session)

        schema = captured_tools["tools"][0]["input_schema"]
        assert set(schema["properties"].keys()) == {"json_data", "json_filter"}
        assert "query" not in schema["properties"]

    def test_a_not_yet_connectable_tool_still_reports_an_honest_explicit_result(self, session, agent, monkeypatch):
        import app.harness as h

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            return {"text": "", "model": model, "mock": False,
                    "tool_input": {"query": "test"},
                    "tool_call": {"id": "call_1", "name": "tool_Jina_AI_Search", "arguments": {"query": "test"}}}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="search the web")
        cfg = {"strategy": "function_calling", "tools": ["tool:Jina AI:Search"], "max_iterations": 1}
        result = handle_agent(cfg, ctx, session)

        output = result["vars"]["agent_tool_outputs"][0]["output"]
        assert "genuinely requires real, external, per-deployment credentials" in output

    def test_a_real_connector_failure_does_not_crash_the_agent_loop(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"text": "", "model": model, "mock": False,
                        "tool_input": {"timezone": "Not/AZone"},
                        "tool_call": {"id": "call_1", "name": "tool_CurrentTime_Get_Current_Time", "arguments": {"timezone": "Not/AZone"}}}
            return {"text": "Final Answer: couldn't get the time.", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id, user_input="What time is it?")
        cfg = {"strategy": "function_calling", "tools": ["tool:CurrentTime:Get Current Time"], "max_iterations": 5}
        result = handle_agent(cfg, ctx, session)

        assert result["vars"]["agent_success"] is True
        assert "tool call failed" in result["vars"]["agent_tool_outputs"][0]["output"]
