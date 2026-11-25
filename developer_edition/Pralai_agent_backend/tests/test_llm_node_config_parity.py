"""
Real tests for the LLM node configuration parity fix (section 0.64,
GAP_REPORT).

The real bug, exactly as reported: NodeConfigPanel.tsx's LLM node
offered temperature, top_p, frequency_penalty, presence_penalty,
agentic strategy, vision, vision detail, reasoning-tag separation,
structured output, allowed tools, timeout, guardrail profile, retry on
failure, and error handling - handle_llm only ever read model, mode,
step_limit, system_prompt, user_prompt, context_var, memory_enabled,
and call_llm hardcoded max_tokens=1024.

The fix wires up the fields that have a real, well-defined Anthropic
API equivalent and clear semantics: temperature, top_p (resolved via
the exact same preset-default logic as the builder's own UI), timeout,
max_tokens (no longer hardcoded), and retry_on_failure/error_handling
(the exact same real two-attempt-then-Default-value/Fail-branch
pattern handle_code already established, reusing the identical
_x_failed + compute_next_node_id routing mechanism).

A real, honest, confirmed-not-achievable finding, tested directly
against the real Anthropic SDK before any UI change: frequency_penalty
and presence_penalty have no real equivalent in the Anthropic Messages
API at all (removed from the config panel, not left as dead controls).
agentic_strategy, vision/vision_detail, structured_output,
allowed_tools, guardrail_profile, and reasoning_tag_separation remain
real, stated gaps - each needs its own separate design decision this
fix doesn't make on the person's behalf - not silently faked.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
from sqlmodel import Session, select

from app.harness import handle_llm, compute_next_node_id
from app.model_adapter import call_llm, LLMCallError
from app.models import Agent, AgentLog


@pytest.fixture()
def session():
    from app.db import engine
    with Session(engine) as s:
        yield s


@pytest.fixture()
def agent(session):
    a = Agent(
        id=f"llm-cfg-test-{uuid.uuid4().hex[:10]}", name="Test", type="App", subtype="Workflow", plane="App",
        owner="you", status="published", version="v1", app_type="workflow", flow={"nodes": [], "edges": []},
    )
    session.add(a)
    session.commit()
    yield a
    for log in session.exec(select(AgentLog).where(AgentLog.agent_id == a.id)).all():
        session.delete(log)
    session.commit()
    session.delete(a)
    session.commit()


def make_ctx(agent_id: str) -> dict:
    return {"input": "test", "vars": {}, "agent_id": agent_id, "agent_name": "test", "invoking_user": "user:test", "output": None}


class TestRealParametersReachTheActualApiCall:
    def test_temperature_top_p_max_tokens_timeout_are_all_genuinely_passed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                resp = MagicMock()
                resp.content = [MagicMock(type="text", text="ok")]
                resp.usage.input_tokens = 5
                resp.usage.output_tokens = 5
                return resp

        import anthropic
        with patch.object(anthropic, "Anthropic") as MockClient:
            MockClient.return_value.messages = FakeMessages()
            call_llm("hello", temperature=0.7, top_p=0.85, max_tokens=2048, timeout=45.0)

        assert captured["temperature"] == 0.7
        assert captured["max_tokens"] == 2048
        assert captured["timeout"] == 45.0
        # top_p is deliberately NOT sent alongside temperature. This
        # assertion used to require both, which is what the adapter did
        # — and every real Anthropic call therefore failed with
        # "`temperature` and `top_p` cannot both be specified for this
        # model". The parameters still genuinely reach the API (that is
        # what this test is for); Anthropic just accepts one of the two.
        # See TestAnthropicSamplingParameters in
        # test_multi_provider_models.py for the per-model rules,
        # measured against the real API.
        assert "top_p" not in captured

    def test_top_p_alone_still_reaches_the_api(self, monkeypatch):
        """The other half of the pair: a node that wants nucleus
        sampling and no temperature gets exactly that."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                resp = MagicMock()
                resp.content = [MagicMock(type="text", text="ok")]
                resp.usage.input_tokens = 5
                resp.usage.output_tokens = 5
                return resp

        import anthropic
        with patch.object(anthropic, "Anthropic") as MockClient:
            MockClient.return_value.messages = FakeMessages()
            call_llm("hello", temperature=None, top_p=0.85, max_tokens=2048, timeout=45.0)

        assert captured["top_p"] == 0.85
        assert "temperature" not in captured

    def test_max_tokens_default_is_still_1024_when_not_specified(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                resp = MagicMock()
                resp.content = [MagicMock(type="text", text="ok")]
                resp.usage.input_tokens = 5
                resp.usage.output_tokens = 5
                return resp

        import anthropic
        with patch.object(anthropic, "Anthropic") as MockClient:
            MockClient.return_value.messages = FakeMessages()
            call_llm("hello")

        assert captured["max_tokens"] == 1024

    def test_temperature_and_top_p_are_genuinely_omitted_when_not_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                resp = MagicMock()
                resp.content = [MagicMock(type="text", text="ok")]
                resp.usage.input_tokens = 5
                resp.usage.output_tokens = 5
                return resp

        import anthropic
        with patch.object(anthropic, "Anthropic") as MockClient:
            MockClient.return_value.messages = FakeMessages()
            call_llm("hello")

        assert "temperature" not in captured
        assert "top_p" not in captured
        assert "timeout" not in captured


class TestRealPresetResolutionMatchesTheFrontend:
    @pytest.mark.parametrize("preset,expected_temp,expected_top_p", [
        ("precise", 0, 0.85),
        ("balanced", 0.5, 0.9),
        ("creative", 1, 1),
    ])
    def test_each_real_preset_resolves_to_the_exact_frontend_values(self, session, agent, monkeypatch, preset, expected_temp, expected_top_p):
        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None, history=None,
                           temperature=None, top_p=None, max_tokens=1024, timeout=None):
            captured["temperature"] = temperature
            captured["top_p"] = top_p
            return {"text": "ok", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id)
        cfg = {"param_preset": preset}
        handle_llm(cfg, ctx, session)

        assert captured["temperature"] == expected_temp
        assert captured["top_p"] == expected_top_p

    def test_an_explicit_per_node_value_wins_over_the_preset(self, session, agent, monkeypatch):
        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None, history=None,
                           temperature=None, top_p=None, max_tokens=1024, timeout=None):
            captured["temperature"] = temperature
            return {"text": "ok", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id)
        cfg = {"param_preset": "precise", "temperature": 0.33}
        handle_llm(cfg, ctx, session)

        assert captured["temperature"] == 0.33

    def test_timeout_s_correctly_converts_to_a_real_float_timeout(self, session, agent, monkeypatch):
        import app.harness as h
        captured = {}

        def fake_call_llm(prompt, model=None, system=None, tools=None, tool_choice=None, history=None,
                           temperature=None, top_p=None, max_tokens=1024, timeout=None):
            captured["timeout"] = timeout
            return {"text": "ok", "model": model, "mock": False}
        monkeypatch.setattr(h, "call_llm", fake_call_llm)

        ctx = make_ctx(agent.id)
        cfg = {"timeout_s": "45"}
        handle_llm(cfg, ctx, session)

        assert captured["timeout"] == 45.0


class TestRealRetryAndErrorHandling:
    def test_retry_on_failure_genuinely_attempts_a_real_second_call(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def failing_call_llm(prompt, **kwargs):
            call_count["n"] += 1
            raise LLMCallError("simulated real provider failure")
        monkeypatch.setattr(h, "call_llm", failing_call_llm)

        ctx = make_ctx(agent.id)
        cfg = {"retry_on_failure": True, "error_handling": "Default value", "default_value": "fallback"}
        handle_llm(cfg, ctx, session)

        assert call_count["n"] == 2

    def test_without_retry_only_one_real_attempt_is_made(self, session, agent, monkeypatch):
        import app.harness as h
        call_count = {"n": 0}

        def failing_call_llm(prompt, **kwargs):
            call_count["n"] += 1
            raise LLMCallError("simulated real provider failure")
        monkeypatch.setattr(h, "call_llm", failing_call_llm)

        ctx = make_ctx(agent.id)
        cfg = {"error_handling": "Fail branch"}
        handle_llm(cfg, ctx, session)

        assert call_count["n"] == 1

    def test_default_value_error_handling_sets_the_real_configured_fallback(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda prompt, **kw: (_ for _ in ()).throw(LLMCallError("fail")))

        ctx = make_ctx(agent.id)
        cfg = {"error_handling": "Default value", "default_value": "a real fallback answer"}
        result = handle_llm(cfg, ctx, session)

        assert result["output"] == "a real fallback answer"
        assert result["vars"]["_llm_failed"] is False

    def test_fail_branch_error_handling_sets_the_real_flag_and_does_not_raise(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda prompt, **kw: (_ for _ in ()).throw(LLMCallError("fail")))

        ctx = make_ctx(agent.id)
        cfg = {"error_handling": "Fail branch"}
        result = handle_llm(cfg, ctx, session)

        assert result["vars"]["_llm_failed"] is True

    def test_no_error_handling_configured_still_genuinely_raises(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda prompt, **kw: (_ for _ in ()).throw(LLMCallError("fail")))

        ctx = make_ctx(agent.id)
        cfg = {}
        with pytest.raises(LLMCallError):
            handle_llm(cfg, ctx, session)

    def test_a_real_successful_call_leaves_llm_failed_false(self, session, agent, monkeypatch):
        import app.harness as h
        monkeypatch.setattr(h, "call_llm", lambda prompt, **kw: {"text": "real answer", "model": "x", "mock": False})

        ctx = make_ctx(agent.id)
        result = handle_llm({}, ctx, session)

        assert result["vars"]["_llm_failed"] is False
        assert result["output"] == "real answer"


class TestRealComputeNextNodeIdRoutingForLlm:
    def _edges(self):
        return {
            "llm1": [
                {"id": "e1", "source": "llm1", "target": "success_target", "source_handle": "success"},
                {"id": "e2", "source": "llm1", "target": "error_target", "source_handle": "error"},
            ]
        }

    def test_a_real_success_routes_to_the_success_port(self):
        node = {"id": "llm1", "type": "llm"}
        ctx = {"vars": {"_llm_failed": False}}
        assert compute_next_node_id(node, ctx, self._edges()) == "success_target"

    def test_a_real_failure_routes_to_the_error_port(self):
        node = {"id": "llm1", "type": "llm"}
        ctx = {"vars": {"_llm_failed": True}}
        assert compute_next_node_id(node, ctx, self._edges()) == "error_target"

    def test_a_legacy_single_edge_with_no_handle_still_works_as_success(self):
        node = {"id": "llm1", "type": "llm"}
        edges = {"llm1": [{"id": "e1", "source": "llm1", "target": "next", "source_handle": None}]}
        ctx = {"vars": {"_llm_failed": False}}
        assert compute_next_node_id(node, ctx, edges) == "next"
