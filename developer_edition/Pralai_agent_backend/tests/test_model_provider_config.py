"""
Real tests for the model provider configuration/execution disconnect
fix (section 0.63, GAP_REPORT).

The real bug, exactly as reported: routers/integrations.py's
configure_model_provider accepted a real api_key/organization/
api_base_url and stored none of them - it only ever flipped
api_key_configured to True and status to "credits". model_adapter.py's
call_llm only ever read os.getenv("ANTHROPIC_API_KEY"), completely
disconnected from anything in the ModelProvider table. The UI could
genuinely say "Provider configured" while the runtime still used mock
mode (or, since section 0.58's fix, genuinely failed the run).

The fix: ModelProvider now has real api_key/organization/api_base_url
columns, with api_key stored as real Fernet ciphertext at rest (the
same secrets_crypto.py mechanism already used for EnvVar/KnowledgeBase/
MCP secrets) and masked on every read via a real, centralized
field_serializer. call_llm now checks the real, stored, decrypted
Anthropic provider config as a genuine fallback when
ANTHROPIC_API_KEY isn't set in the environment - the env var still
takes priority, matching the existing, documented deployment
convention.

A real, honest, confirmed finding while building this: the real
Anthropic SDK client has no "organization" parameter (unlike OpenAI's)
- organization is stored for honest completeness but genuinely never
passed to anything in call_llm.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from app.db import engine
from app.models import ModelProvider
from app.model_adapter import call_llm, LLMCallError, _stored_provider_config

client = TestClient(app)


@pytest.fixture()
def clean_anthropic_provider():
    """An Anthropic row with no key, put back exactly as it was afterwards.

    This used to null the row on setup and null it AGAIN on teardown, so
    running the suite deleted whatever key an operator had saved through
    the Integrations page. There is no separate test database — these
    tests use app.db.engine directly — so the damage landed on the real
    install.

    The symptom was a key that appeared to remove itself: the six other
    providers stayed configured throughout, because no fixture targeted
    them, which is what made it look like something specific to
    Anthropic rather than something specific to the test suite.
    """
    def _write(api_key, organization, api_base_url, configured, status):
        with Session(engine) as s:
            mp = s.get(ModelProvider, "Anthropic")
            mp.api_key = api_key
            mp.organization = organization
            mp.api_base_url = api_base_url
            mp.api_key_configured = configured
            mp.status = status
            s.add(mp)
            s.commit()

    with Session(engine) as s:
        mp = s.get(ModelProvider, "Anthropic")
        saved = (mp.api_key, mp.organization, mp.api_base_url,
                 mp.api_key_configured, mp.status)

    _write(None, None, None, False, "needs_key")
    try:
        yield
    finally:
        # finally, so a failing test still hands the key back.
        _write(*saved)


class TestRealProviderConfigStorage:
    def test_configuring_a_provider_genuinely_encrypts_the_key_at_rest(self, clean_anthropic_provider):
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-real-test-key"})
        with Session(engine) as s:
            mp = s.get(ModelProvider, "Anthropic")
            assert mp.api_key is not None
            assert mp.api_key != "sk-ant-real-test-key"
            assert mp.api_key.startswith("enc:v1:")  # §0.89, GAP_REPORT: real encrypted values are now explicitly marked

    def test_the_one_time_response_genuinely_echoes_the_real_plaintext_key(self, clean_anthropic_provider):
        r = client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-real-test-key"})
        assert r.status_code == 200
        assert r.json()["api_key"] == "sk-ant-real-test-key"

    def test_every_later_read_masks_the_real_key(self, clean_anthropic_provider):
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-real-test-key"})
        r = client.get("/api/integrations/model-providers")
        anthropic = next(p for p in r.json() if p["name"] == "Anthropic")
        assert anthropic["api_key"] != "sk-ant-real-test-key"
        assert anthropic["api_key"] == "••••••••"

    def test_organization_and_api_base_url_are_genuinely_stored_too(self, clean_anthropic_provider):
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={
            "api_key": "sk-ant-real-test-key", "organization": "org-123", "api_base_url": "https://custom.example.com",
        })
        with Session(engine) as s:
            mp = s.get(ModelProvider, "Anthropic")
            assert mp.organization == "org-123"
            assert mp.api_base_url == "https://custom.example.com"

    def test_api_key_configured_and_status_still_flip_the_same_real_way_as_before(self, clean_anthropic_provider):
        r = client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-real-test-key"})
        assert r.json()["api_key_configured"] is True
        assert r.json()["status"] == "credits"


class TestRealStoredKeyRetrieval:
    def test_stored_anthropic_config_returns_none_when_nothing_is_configured(self, clean_anthropic_provider):
        key, org, base_url = _stored_provider_config("Anthropic")
        assert key is None

    def test_stored_anthropic_config_correctly_decrypts_the_real_stored_key(self, clean_anthropic_provider):
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-real-test-key"})
        key, org, base_url = _stored_provider_config("Anthropic")
        assert key == "sk-ant-real-test-key"

    def test_stored_anthropic_config_returns_the_real_organization_and_base_url_too(self, clean_anthropic_provider):
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={
            "api_key": "sk-ant-real-test-key", "organization": "org-123", "api_base_url": "https://custom.example.com",
        })
        key, org, base_url = _stored_provider_config("Anthropic")
        assert org == "org-123"
        assert base_url == "https://custom.example.com"


class TestRealCallLlmFallbackToStoredKey:
    def test_call_llm_genuinely_uses_the_stored_key_instead_of_mock_mode(self, clean_anthropic_provider, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-deliberately-invalid"})

        with pytest.raises(LLMCallError, match="Real Anthropic API call failed"):
            call_llm("hello")

    def test_the_environment_variable_still_takes_priority_over_the_stored_key(self, clean_anthropic_provider, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-real-env-var")
        client.post("/api/integrations/model-providers/Anthropic/api-key", json={"api_key": "sk-ant-from-stored-config"})

        with pytest.raises(LLMCallError, match="Real Anthropic API call failed"):
            call_llm("hello")
        with Session(engine) as s:
            from app.secrets_crypto import decrypt_secret
            mp = s.get(ModelProvider, "Anthropic")
            assert decrypt_secret(mp.api_key) == "sk-ant-from-stored-config"
        assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-from-real-env-var"
        import inspect
        from app import model_adapter
        # §0.114, GAP_REPORT — this real logic moved from call_llm
        # itself into the new, real, per-provider _call_anthropic
        # during the multi-provider refactor; call_llm is now purely a
        # real dispatch layer with no provider-specific logic of its
        # own left to inspect here.
        src = inspect.getsource(model_adapter._call_anthropic)
        env_check_pos = src.index('os.getenv("ANTHROPIC_API_KEY")')
        fallback_pos = src.index('_stored_provider_config("Anthropic")')
        assert env_check_pos < fallback_pos

    def test_no_key_anywhere_and_mocks_not_allowed_still_genuinely_raises(self, clean_anthropic_provider, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "false")
        with pytest.raises(LLMCallError, match="No ANTHROPIC_API_KEY is configured"):
            call_llm("hello")

    def test_no_key_anywhere_but_mocks_allowed_still_genuinely_falls_back_to_mock(self, clean_anthropic_provider, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ALLOW_MOCK_MODELS", "true")
        result = call_llm("hello")
        assert result["mock"] is True


@pytest.fixture()
def preserve_real_anthropic_key():
    """Hand the operator's real key back after a test that overwrites it.

    The tests below deliberately save a sentinel key to prove the
    fixture restores what it found. Without this they would leave the
    sentinel in place — committing the exact fault they exist to catch.
    """
    with Session(engine) as s:
        mp = s.get(ModelProvider, "Anthropic")
        saved = (mp.api_key, mp.api_key_configured, mp.status)
    try:
        yield
    finally:
        with Session(engine) as s:
            mp = s.get(ModelProvider, "Anthropic")
            mp.api_key, mp.api_key_configured, mp.status = saved
            s.add(mp)
            s.commit()


class TestTheFixtureGivesTheKeyBack:
    """The suite runs against the live database — app.db.engine, with no
    separate test database — so a fixture that leaves a provider row
    blank destroys a real operator's saved key.

    This is what actually happened: `clean_anthropic_provider` nulled the
    row on setup and nulled it AGAIN on teardown, so every run of the
    suite deleted the Anthropic key saved through the Integrations page.
    It read as the key removing itself, and it looked Anthropic-specific
    only because no fixture targeted the other six providers.
    """

    def test_a_key_saved_beforehand_is_still_there_afterwards(self, preserve_real_anthropic_key):
        sentinel = "sk-ant-sentinel-do-not-keep"
        client.post("/api/integrations/model-providers/Anthropic/api-key",
                    json={"api_key": sentinel})
        with Session(engine) as s:
            before = s.get(ModelProvider, "Anthropic").api_key
        assert before and before.startswith("enc:v1:")

        # Drive the fixture exactly as a test using it would.
        gen = clean_anthropic_provider.__wrapped__()
        next(gen)
        with Session(engine) as s:
            during = s.get(ModelProvider, "Anthropic")
            assert during.api_key is None, "the test should see a clean row"
            assert during.api_key_configured is False
        next(gen, None)   # teardown

        with Session(engine) as s:
            after = s.get(ModelProvider, "Anthropic")
        assert after.api_key == before, "the operator's key must be handed back"
        assert after.api_key_configured is True

    def test_the_key_is_returned_even_when_the_test_failed(self, preserve_real_anthropic_key):
        """A failing test must not take the key with it — the teardown
        runs in a finally block for exactly this reason."""
        client.post("/api/integrations/model-providers/Anthropic/api-key",
                    json={"api_key": "sk-ant-sentinel-2"})
        with Session(engine) as s:
            before = s.get(ModelProvider, "Anthropic").api_key

        gen = clean_anthropic_provider.__wrapped__()
        next(gen)
        with pytest.raises(RuntimeError):
            gen.throw(RuntimeError("the test blew up"))

        with Session(engine) as s:
            assert s.get(ModelProvider, "Anthropic").api_key == before
