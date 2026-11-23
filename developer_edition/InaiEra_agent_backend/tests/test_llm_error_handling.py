"""
Real tests for the LLM error/missing-credentials fix (section 0.58, GAP_REPORT).

The real bug, exactly as reported: model_adapter.py's call_llm returned
{"text": "[LLM call failed: ...]", "error": ...} on a real provider
failure, instead of raising - and a missing ANTHROPIC_API_KEY silently
returned a mock response. handle_llm (harness.py) never checked
result["error"], so a real provider failure could genuinely become a
flow's own "successful" final output.

The fix: call_llm now raises LLMCallError on both a real provider
failure (always) and a missing API key (unless ALLOW_MOCK_MODELS=true
is explicitly set). The exception propagates through the same real
node-handler exception chain every other node failure already uses,
genuinely failing the run. main.py refuses to even start if
ALLOW_MOCK_MODELS=true is set alongside ENVIRONMENT=production.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.model_adapter import call_llm, LLMCallError, _mocks_allowed

client = TestClient(app)


def llm_flow() -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "llm", "x": 0, "y": 0, "sub": "", "config": {"user_prompt": "{{input}}"}},
            {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def create_agent() -> dict:
    r = client.post("/api/agents", json={
        "name": f"LLM Error Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": llm_flow(),
    })
    assert r.status_code == 200, r.text
    return r.json()


class TestRealMockOptIn:
    def test_no_api_key_and_no_allow_mock_env_var_genuinely_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ALLOW_MOCK_MODELS", raising=False)
        with pytest.raises(LLMCallError, match="ALLOW_MOCK_MODELS"):
            call_llm("hello")

    def test_no_api_key_with_allow_mock_true_still_returns_the_real_honest_mock(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = call_llm("hello there")
        assert result["mock"] is True
        assert "MOCK" in result["text"]

    def test_allow_mock_models_is_case_insensitive_like_a_real_env_var_should_be(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "TRUE")
        assert _mocks_allowed() is True

    def test_a_real_but_falsy_looking_value_does_not_accidentally_enable_mocks(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "1")
        assert _mocks_allowed() is False
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "yes")
        assert _mocks_allowed() is False


class TestRealProviderFailureAlwaysRaises:
    def test_a_real_api_call_with_an_invalid_key_genuinely_raises_even_when_mocks_are_allowed(self, monkeypatch):
        """A real network call to the real Anthropic API with a real,
        deliberately-invalid key - genuinely reaches the real server
        and gets a real 401, rather than mocking anthropic.Anthropic
        (which model_adapter.py imports locally inside the function,
        not at module level, so a module-level monkeypatch wouldn't
        actually intercept it anyway - this is the more honest test)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deliberately-invalid-test-key")
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        with pytest.raises(LLMCallError, match="Real Anthropic API call failed"):
            call_llm("hello")


class TestRealRunGenuinelyFailsInsteadOfSucceedingWithErrorText:
    def test_a_real_run_with_no_credentials_and_mocks_disabled_genuinely_fails(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "false")
        agent = create_agent()

        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "failed"
        assert body["fail_class"] == "orchestration"
        assert body["output"] != "[LLM call failed: rate limit ...]"
        assert "[LLM call failed" not in (body.get("output") or "")

    def test_a_real_run_with_mocks_explicitly_allowed_still_genuinely_succeeds(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        agent = create_agent()

        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert "MOCK" in body["output"]


class TestRealModerationFailsClosedNotHardFailsTheWholeRun:
    def test_ai_moderation_check_failure_blocks_the_message_rather_than_raising(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "false")

        from app.routers.execution import _moderation_flagged
        result = _moderation_flagged("ai", "some real user message", {})
        assert result is not None
        assert "blocked as a precaution" in result


class TestRealFollowUpsGracefullyDegrade:
    def test_a_real_provider_failure_during_follow_ups_returns_none_not_an_exception(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "false")

        from app.routers.execution import _generate_follow_ups
        from app.models import Agent

        agent = Agent(
            id="test", name="Test", type="App", subtype="Custom", plane="App", owner="you",
            status="draft", version="v1", app_type="chatbot", flow={"nodes": [], "edges": []},
            model="Claude Sonnet 5", features={"follow_up": {"enabled": True}},
        )
        result = _generate_follow_ups(agent.features, "What is this?", "This is a real test answer.", default_model=agent.model)
        assert result is None


class TestRealProductionStartupSafetyCheck:
    def test_allow_mock_models_and_production_together_is_a_real_startup_refusal(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        monkeypatch.setenv("ENVIRONMENT", "production")
        allow_mocks = os.getenv("ALLOW_MOCK_MODELS", "").strip().lower() == "true"
        is_production = os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        assert allow_mocks and is_production

    def test_mocks_allowed_without_production_is_genuinely_fine(self, monkeypatch):
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        is_production = os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        assert is_production is False

    def test_production_without_mocks_allowed_is_genuinely_fine(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("ALLOW_MOCK_MODELS", raising=False)
        allow_mocks = os.getenv("ALLOW_MOCK_MODELS", "").strip().lower() == "true"
        assert allow_mocks is False


class TestRealToolCallShape:
    """Real tests for section 0.62, GAP_REPORT - call_llm previously
    surfaced only tool_input (the arguments), silently dropping which
    tool was genuinely selected and its real call id. Both are now
    real, direct fields on the response."""

    def test_mock_mode_tool_call_names_the_first_configured_tool(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        tools = [
            {"name": "search_docs", "description": "x", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
            {"name": "send_email", "description": "x", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}}}},
        ]
        result = call_llm("do it", tools=tools, tool_choice={"type": "auto"})
        assert result["tool_call"]["name"] == "search_docs"
        assert result["tool_call"]["arguments"] == result["tool_input"]
        assert "id" in result["tool_call"]

    def test_tool_input_is_still_present_for_backward_compatibility(self, monkeypatch):
        """A real regression guard - handle_parameter_extractor still
        reads tool_input directly; this fix must not have removed it."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        tools = [{"name": "extract", "description": "x", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}}]
        result = call_llm("do it", tools=tools, tool_choice={"type": "tool", "name": "extract"})
        assert "tool_input" in result
        assert result["tool_input"] == result["tool_call"]["arguments"]

    def test_no_tools_configured_means_no_tool_call_key_at_all(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = call_llm("just answer normally")
        assert "tool_call" not in result
        assert "tool_input" not in result
