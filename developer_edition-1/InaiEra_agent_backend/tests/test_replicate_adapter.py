"""Tests for the Replicate adapter.

Replicate had a key that could be saved and models that could be
registered, with nothing behind either: it does not expose
/chat/completions, so the shared OpenAI-compatible path could not serve
it and call_llm refused every Replicate model. It has its own
predictions API, which _call_replicate now speaks.

Verified against the live API with a real token: the token was accepted
and the model references resolved, and the calls then failed on account
credit (402, then 429 throttling) — so the request shape is right and
only billing stands between this and generated text.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import model_adapter
from app.model_adapter import LLMCallError, RUNTIME_SUPPORTED_PROVIDERS, _NOT_OPENAI_COMPATIBLE


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class _Client:
    """Stands in for the governed httpx client, capturing the request."""
    def __init__(self, resp):
        self.resp = resp
        self.seen = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None, headers=None):
        self.seen = {"url": url, "json": json, "headers": headers}
        return self.resp


@pytest.fixture
def stub(monkeypatch):
    """A configured Replicate provider and a controllable transport."""
    monkeypatch.setattr(model_adapter, "_stored_provider_config",
                        lambda name: ("tok-123", None, None))
    holder = {}

    def make(resp):
        client = _Client(resp)
        holder["client"] = client
        monkeypatch.setattr(model_adapter, "governed_httpx_client", lambda: client, raising=False)
        import app.egress as egress
        monkeypatch.setattr(egress, "governed_httpx_client", lambda: client)
        return client

    make.holder = holder
    return make


class TestProviderRegistration:
    def test_replicate_is_runtime_supported_now(self):
        assert "Replicate" in RUNTIME_SUPPORTED_PROVIDERS

    def test_the_incompatibility_reason_is_still_recorded(self):
        """It genuinely is not OpenAI-compatible; the UI shows that
        reason. Being runnable and being compatible are different
        claims, and removing the reason would misstate the second."""
        assert "Replicate" in _NOT_OPENAI_COMPATIBLE
        assert "predictions API" in _NOT_OPENAI_COMPATIBLE["Replicate"]

    def test_bedrock_remains_honestly_unwired(self):
        assert "Amazon Bedrock" not in RUNTIME_SUPPORTED_PROVIDERS


class TestRequestShape:
    def test_an_official_model_uses_the_models_endpoint(self, stub):
        client = stub(_Resp(payload={"status": "succeeded", "output": ["ok"], "id": "p1"}))
        model_adapter._call_replicate("hi", "llama", None, None, None, None, None, None, 64, 30,
                                      api_model_override="meta/meta-llama-3-8b-instruct")
        assert client.seen["url"].endswith("/v1/models/meta/meta-llama-3-8b-instruct/predictions")
        assert "version" not in client.seen["json"]
        # Prefer: wait makes it synchronous, so no polling loop is needed.
        assert client.seen["headers"]["Prefer"] == "wait"
        assert client.seen["headers"]["Authorization"] == "Bearer tok-123"

    def test_a_pinned_version_uses_the_predictions_endpoint(self, stub):
        client = stub(_Resp(payload={"status": "succeeded", "output": ["ok"]}))
        model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                      api_model_override="owner/name:abc123")
        assert client.seen["url"].endswith("/v1/predictions")
        assert client.seen["json"]["version"] == "abc123"

    def test_system_prompt_and_sampling_are_passed_through(self, stub):
        client = stub(_Resp(payload={"status": "succeeded", "output": ["ok"]}))
        model_adapter._call_replicate("hi", "m", "be terse", None, None, None, 0.2, 0.9, 128, 30,
                                      api_model_override="owner/name")
        sent = client.seen["json"]["input"]
        assert sent["system_prompt"] == "be terse"
        assert sent["temperature"] == 0.2 and sent["top_p"] == 0.9
        assert sent["max_tokens"] == 128

    def test_a_reference_without_an_owner_is_rejected_before_any_call(self, stub):
        stub(_Resp(payload={}))
        with pytest.raises(LLMCallError) as excinfo:
            model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                          api_model_override="just-a-name")
        assert "owner/name" in str(excinfo.value)


class TestResponseHandling:
    def test_streamed_fragments_are_joined_into_one_text(self, stub):
        """LLMs on Replicate return a list of string fragments; a caller
        expecting `text` must not receive a list."""
        stub(_Resp(payload={"status": "succeeded", "output": ["Hel", "lo ", "world"], "id": "p9"}))
        out = model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                            api_model_override="owner/name")
        assert out.text == "Hello world"
        assert out.mock is False
        assert out.provider_request_id == "p9"

    def test_a_single_string_output_is_handled(self, stub):
        stub(_Resp(payload={"status": "succeeded", "output": "done"}))
        out = model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                            api_model_override="owner/name")
        assert out.text == "done"

    def test_token_counts_are_reported_only_when_the_model_gives_them(self, stub):
        """Replicate bills by run time, so most models report none —
        fabricating a number would corrupt cost tracking."""
        stub(_Resp(payload={"status": "succeeded", "output": ["x"], "metrics": {}}))
        out = model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                            api_model_override="owner/name")
        assert out.usage == {}
        stub(_Resp(payload={"status": "succeeded", "output": ["x"],
                            "metrics": {"input_token_count": 7, "output_token_count": 3}}))
        out = model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                            api_model_override="owner/name")
        assert out.usage == {"input_tokens": 7, "output_tokens": 3}

    def test_an_unfinished_prediction_raises_rather_than_returning_empty_text(self, stub):
        """`Prefer: wait` can time out on a slow model. Returning "" would
        look like the model answered with nothing."""
        stub(_Resp(payload={"status": "processing", "id": "p2"}))
        with pytest.raises(LLMCallError) as excinfo:
            model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                          api_model_override="owner/name")
        assert "did not succeed" in str(excinfo.value)
        assert "processing" in str(excinfo.value)


class TestHonestErrors:
    def test_insufficient_credit_exonerates_the_key_and_model(self, stub):
        """The live account hit exactly this. Reporting it as a generic
        failure sends someone to re-check a key that works."""
        stub(_Resp(status=402, text='{"title":"Insufficient credit"}'))
        with pytest.raises(LLMCallError) as excinfo:
            model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                          api_model_override="owner/name")
        message = str(excinfo.value)
        assert "Nothing is wrong with the key or the model name" in message
        assert "billing" in message

    def test_throttling_is_linked_to_the_credit_case(self, stub):
        stub(_Resp(status=429, text='{"detail":"Request was throttled."}'))
        with pytest.raises(LLMCallError) as excinfo:
            model_adapter._call_replicate("hi", "m", None, None, None, None, None, None, 64, 30,
                                          api_model_override="owner/name")
        assert "without credit are limited" in str(excinfo.value)

    def test_tools_are_refused_rather_than_silently_dropped(self, stub):
        """Replicate has no normalised function-calling surface. Ignoring
        a caller's tools would look like the model chose not to use
        them."""
        stub(_Resp(payload={"status": "succeeded", "output": ["x"]}))
        with pytest.raises(LLMCallError) as excinfo:
            model_adapter._call_replicate(
                "hi", "m", None,
                [{"name": "f", "description": "d", "input_schema": {"type": "object"}}],
                None, None, None, None, 64, 30, api_model_override="owner/name")
        assert "no tool-calling contract" in str(excinfo.value)
