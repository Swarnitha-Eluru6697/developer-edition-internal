"""
Real tests for section 0.114, GAP_REPORT — true multi-provider model
execution.

The real, reported bug: model_adapter.py's call_llm was entirely,
hard-codedly Anthropic-only — every real model name, regardless of
what was configured, silently resolved through the same Anthropic-only
lookup. Meanwhile, routers/integrations.py's own seeded catalog listed
10 real "LLM"-type providers (Anthropic, OpenAI, Gemini, Amazon
Bedrock, Cohere, Azure OpenAI, Azure AI Studio, Hugging Face Hub,
Replicate, DeepSeek), all configurable through the same real UI, with
only Anthropic having any real execution behind it at all — a real
product/runtime mismatch the moment the UI implied any of the others
were usable for real Agent execution.

The fix: a canonical ModelResult dataclass every real provider adapter
returns; a real, second, genuinely-wired provider (OpenAI, using the
real `openai` SDK); honest model-name validation (an unrecognized
model raises rather than silently falling back to a different model's
own output); and a real, computed `runtime_supported` field on every
provider row the API returns, so a builder can never be shown a
catalog-only provider as if it were equally usable.

These tests exercise the real functions directly — mocking only the
third-party SDK client itself (since this sandbox has no real OpenAI
credentials to call the live API with), never model_adapter.py's own
logic.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.model_adapter import call_llm, LLMCallError, ModelResult, _MODEL_PROVIDERS, RUNTIME_SUPPORTED_PROVIDERS

client = TestClient(app)


class TestRealModelResultAbstraction:
    def test_model_result_has_every_field_the_report_names(self):
        r = ModelResult(text="hello")
        assert r.text == "hello"
        assert r.tool_calls == []
        assert r.usage == {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        assert r.finish_reason is None
        assert r.provider_request_id is None


class TestRealModelNameValidation:
    """§0.114, GAP_REPORT — the actual fix for the report's own core
    concern: an unrecognized model name must never silently route to
    a different model's own real output."""

    def test_an_unrecognized_model_raises_honestly(self):
        with pytest.raises(LLMCallError, match="Unrecognized model"):
            call_llm("test", model="Totally Made Up Model")

    def test_every_real_catalog_model_has_a_real_assigned_provider(self):
        for model in ("Claude Sonnet 5", "Claude Opus 4.8", "Claude Haiku 4.5", "GPT-4o", "GPT-4o mini"):
            assert model in _MODEL_PROVIDERS


class TestRealAnthropicMockModeUnchanged:
    """Regression — the real refactor into _call_anthropic must not
    change Anthropic's own existing, established mock-mode behavior."""

    def test_anthropic_mock_mode_still_works(self, monkeypatch, no_stored_provider_key):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        no_stored_provider_key("Anthropic")
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = call_llm("Hello", model="Claude Sonnet 5")
        assert result["mock"] is True
        assert "ANTHROPIC_API_KEY" in result["text"]


class TestRealOpenAiMockMode:
    """§0.114, GAP_REPORT — mock mode for the new, second, real
    provider must work the same, honest way Anthropic's own already
    does — a missing key with ALLOW_MOCK_MODELS=true produces an
    honestly-labeled mock, naming the CORRECT env var for the provider
    that was actually configured, not Anthropic's."""

    def test_openai_mock_mode_names_the_correct_env_var(self, monkeypatch, no_stored_provider_key):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        no_stored_provider_key("OpenAI")
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = call_llm("Hello", model="GPT-4o")
        assert result["mock"] is True
        assert "OPENAI_API_KEY" in result["text"]
        assert "ANTHROPIC_API_KEY" not in result["text"]

    def test_openai_missing_key_and_mocks_not_allowed_raises_honestly(self, monkeypatch, no_stored_provider_key):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        no_stored_provider_key("OpenAI")
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "false")
        # Matches the part that carries the meaning — which provider,
        # and which env var to set — rather than the exact sentence, so
        # rewording the message does not fail this again.
        with pytest.raises(LLMCallError, match=r"OPENAI_API_KEY.*OpenAI provider config"):
            call_llm("Hello", model="GPT-4o")

    def test_openai_mock_tool_call_reads_the_anthropic_shaped_schema(self, monkeypatch, no_stored_provider_key):
        """A caller (handle_agent) always builds an Anthropic-shaped
        tool schema — mock mode for OpenAI must still read it
        correctly, since the real schema translation only happens on
        the real, live API-call path."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        no_stored_provider_key("OpenAI")
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        tools = [{"name": "get_weather", "description": "Get weather",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
        result = call_llm("weather?", model="GPT-4o", tools=tools)
        assert result["tool_call"]["name"] == "get_weather"
        assert "city" in result["tool_input"]


class TestRealOpenAiLiveApiPath:
    """§0.114, GAP_REPORT — proves the real schema translation and
    response normalization against a real, mocked SDK client (the
    exact shape the real `openai` SDK returns) — not merely that mock
    mode works, which says nothing about the real API-call path."""

    def _fake_response(self, content=None, tool_calls=None, finish_reason="stop"):
        message = MagicMock()
        message.content = content
        message.tool_calls = tool_calls
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = finish_reason
        resp = MagicMock()
        resp.choices = [choice]
        resp.id = "resp_test123"
        resp.usage.prompt_tokens = 20
        resp.usage.completion_tokens = 8
        resp.usage.prompt_tokens_details = None
        return resp

    def test_a_real_anthropic_shaped_tool_schema_is_translated_to_openais_own_real_shape(self, monkeypatch, no_stored_provider_key):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
        tools = [{"name": "get_weather", "description": "Get weather",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
        tool_call = MagicMock()
        tool_call.id = "call_abc"
        tool_call.function.name = "get_weather"
        tool_call.function.arguments = '{"city": "Tokyo"}'
        fake_resp = self._fake_response(content=None, tool_calls=[tool_call], finish_reason="tool_calls")

        captured = {}
        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake_resp

        with patch("openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            mock_client.chat.completions.create = fake_create
            MockOpenAI.return_value = mock_client
            result = call_llm("weather in Tokyo?", model="GPT-4o", tools=tools)

        assert captured["tools"] == [{
            "type": "function",
            "function": {"name": "get_weather", "description": "Get weather",
                         "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
        }]
        assert result["tool_input"] == {"city": "Tokyo"}
        assert result["tool_call"] == {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Tokyo"}}

    def test_a_real_plain_text_response_is_normalized_correctly(self, monkeypatch, no_stored_provider_key):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
        fake_resp = self._fake_response(content="The weather in Tokyo is sunny.", tool_calls=None, finish_reason="stop")
        with patch("openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = fake_resp
            MockOpenAI.return_value = mock_client
            result = call_llm("weather in Tokyo?", model="GPT-4o")
        assert result["text"] == "The weather in Tokyo is sunny."
        assert "tool_call" not in result
        assert result["usage"] == {"input_tokens": 20, "output_tokens": 8, "cached_tokens": 0}

    def test_a_real_openai_api_failure_raises_honestly(self, monkeypatch, no_stored_provider_key):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
        with patch("openai.OpenAI") as MockOpenAI:
            mock_client = MagicMock()
            mock_client.chat.completions.create.side_effect = RuntimeError("real connection refused")
            MockOpenAI.return_value = mock_client
            with pytest.raises(LLMCallError, match="Real OpenAI API call failed"):
                call_llm("test", model="GPT-4o")


class TestRealRuntimeSupportedDisclosure:
    """§0.114, GAP_REPORT — the actual fix for the report's own core,
    reported concern: a builder must never be shown a catalog-only
    provider as if it were equally usable for real Agent execution."""

    def test_anthropic_and_openai_are_marked_runtime_supported(self):
        assert "Anthropic" in RUNTIME_SUPPORTED_PROVIDERS
        assert "OpenAI" in RUNTIME_SUPPORTED_PROVIDERS

    def test_the_openai_compatible_providers_are_runtime_supported(self):
        """These gained a real execution path when _call_openai was
        pointed at each vendor's own OpenAI-compatible base URL, so the
        set is no longer just Anthropic + OpenAI. This test used to
        assert the opposite and had gone stale against the code."""
        for name in ("Gemini", "Cohere", "Azure OpenAI", "Azure AI Studio",
                     "Hugging Face Hub", "DeepSeek"):
            assert name in RUNTIME_SUPPORTED_PROVIDERS, name

    def test_a_provider_with_no_adapter_may_not_claim_to_be_runnable(self):
        """The report's core concern, still enforced — but now only for
        Bedrock. SigV4 request signing is genuinely not implemented.
        Replicate WAS here for the same reason and has since been given
        its own adapter (_call_replicate speaks its predictions API), so
        asserting it is unsupported would now assert something false."""
        assert "Amazon Bedrock" not in RUNTIME_SUPPORTED_PROVIDERS

    def test_replicate_is_runnable_through_its_own_adapter(self):
        """Not OpenAI-compatible and yet runnable: those are separate
        claims, and the provider row carries both."""
        from app.model_adapter import _NOT_OPENAI_COMPATIBLE
        assert "Replicate" in RUNTIME_SUPPORTED_PROVIDERS
        assert "Replicate" in _NOT_OPENAI_COMPATIBLE

    def test_the_real_api_endpoint_reports_this_honestly_for_every_seeded_provider(self):
        r = client.get("/api/integrations/model-providers")
        assert r.status_code == 200
        by_name = {p["name"]: p["runtime_supported"] for p in r.json()}
        assert by_name.get("Anthropic") is True
        assert by_name.get("OpenAI") is True
        assert by_name.get("Gemini") is True
        assert by_name.get("DeepSeek") is True
        # Replicate has its own adapter now, so it reports runnable
        # while still carrying its "not OpenAI-compatible" reason.
        assert by_name.get("Replicate") is True
        # Bedrock genuinely cannot run and must still say so.
        assert by_name.get("Amazon Bedrock") is False


class TestAnthropicSamplingParameters:
    """Anthropic's sampling parameters are per-model, and what a model
    accepts changes between generations. Measured against the real API
    with a real key:

        claude-haiku-4-5   temperature OR top_p — both together is a 400
                           "`temperature` and `top_p` cannot both be
                           specified for this model"
        claude-sonnet-5    NEITHER — each is rejected on its own with
                           "`temperature` is deprecated for this model"

    This was not a theoretical risk. The LLM node's Precise/Balanced/
    Creative presets each define BOTH values, so every preset-configured
    node sent both, and every Anthropic call failed — three real agents
    in this workspace, all reporting "failed: orchestration" while the
    actual message was about parameters nobody had typed.

    A hardcoded per-model table would rot exactly the way pinned Gemini
    model ids did, so the adapter sends one parameter and, if the model
    refuses it, sends the request again without any.
    """

    def _client(self, monkeypatch, refusals):
        """A stand-in Anthropic client that refuses the way the real API
        does, and records what each attempt actually sent."""
        attempts = []

        class _Usage:
            input_tokens = 5
            output_tokens = 2
            cache_read_input_tokens = 0

        class _Block:
            type = "text"
            text = "OK"

        class _Response:
            id = "msg_test"
            stop_reason = "end_turn"
            content = [_Block()]
            usage = _Usage()

        class _Messages:
            def create(self_inner, **kwargs):
                attempts.append(dict(kwargs))
                sent = {k for k in ("temperature", "top_p") if k in kwargs}
                for trigger, message in refusals:
                    if trigger(sent):
                        raise RuntimeError(message)
                return _Response()

        class _Client:
            def __init__(self_inner, **kwargs):
                self_inner.messages = _Messages()

        import app.model_adapter as ma
        import types
        fake_module = types.SimpleNamespace(Anthropic=_Client)
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "anthropic":
                return fake_module
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        monkeypatch.setattr(ma, "_stored_provider_config", lambda name: ("sk-test", None, None))
        return attempts

    def test_both_parameters_are_never_sent_together(self, monkeypatch, no_stored_provider_key):
        from app.model_adapter import _call_anthropic
        attempts = self._client(monkeypatch, refusals=[
            (lambda sent: sent == {"temperature", "top_p"},
             "Error code: 400 - `temperature` and `top_p` cannot both be specified for this model."),
        ])
        result = _call_anthropic("hi", "Claude Haiku 4.5", None, None, None, None,
                                 temperature=0.5, top_p=0.9, max_tokens=100, timeout=None)
        assert result.text == "OK"
        # One attempt, carrying temperature alone — not a failed call
        # followed by a retry.
        assert len(attempts) == 1
        assert attempts[0]["temperature"] == 0.5 and "top_p" not in attempts[0]

    def test_a_model_that_deprecates_both_gets_neither(self, monkeypatch, no_stored_provider_key):
        """claude-sonnet-5's real behaviour: temperature alone is a 400,
        top_p alone is a 400, and the request only succeeds with no
        sampling parameters at all."""
        from app.model_adapter import _call_anthropic
        attempts = self._client(monkeypatch, refusals=[
            (lambda sent: bool(sent), "Error code: 400 - `temperature` is deprecated for this model."),
        ])
        result = _call_anthropic("hi", "Claude Sonnet 5", None, None, None, None,
                                 temperature=0.5, top_p=0.9, max_tokens=100, timeout=None)
        assert result.text == "OK"
        assert len(attempts) == 2, "expected one rejected attempt and one without the parameters"
        assert "temperature" in attempts[0]
        assert "temperature" not in attempts[1] and "top_p" not in attempts[1]

    def test_an_unrelated_400_is_not_retried(self, monkeypatch, no_stored_provider_key):
        """The retry exists for one specific refusal. Anything else must
        surface as the failure it is, on the first attempt."""
        from app.model_adapter import _call_anthropic, LLMCallError
        attempts = self._client(monkeypatch, refusals=[
            (lambda sent: True, "Error code: 400 - max_tokens: must be greater than 0"),
        ])
        with pytest.raises(LLMCallError) as e:
            _call_anthropic("hi", "Claude Haiku 4.5", None, None, None, None,
                            temperature=0.5, top_p=None, max_tokens=0, timeout=None)
        assert "max_tokens" in str(e.value)
        assert len(attempts) == 1, "an unrelated error must not trigger a second call"

    def test_a_call_with_no_sampling_parameters_is_left_alone(self, monkeypatch, no_stored_provider_key):
        from app.model_adapter import _call_anthropic
        attempts = self._client(monkeypatch, refusals=[])
        _call_anthropic("hi", "Claude Haiku 4.5", None, None, None, None,
                        temperature=None, top_p=None, max_tokens=100, timeout=None)
        assert len(attempts) == 1
        assert "temperature" not in attempts[0] and "top_p" not in attempts[0]
