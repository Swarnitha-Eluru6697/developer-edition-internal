"""The ⌘K assistant can be switched (Settings → Codegen agent) from the in-perimeter gateway to OpenAI or Anthropic with the admin's own key.

What must hold:
  * only a workspace admin can verify/change it; everyone else sees which agent is active, never a key;
  * the key lives in the secrets store only — not in Redis, not in a response, not in the audit record;
  * once chosen, EVERY completion goes to that provider (right URL, right key, its configured model) and a failure is reported, never
    quietly answered by another model;
  * the open file's content is governed by the provider actually used, and an external provider is always outside the perimeter.
The providers are faked at the HTTP layer (httpx.MockTransport); everything else is the real code.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.codegen_agent.model_client import ModelClient, ModelProviderError
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.deps import get_db
from app.main import app
from app.services import codegen_governance as gov
from app.services import codegen_provider as cp
from conftest import _headers

ADMIN = {"roles": ["workspace_admin"]}
ANALYST = {"roles": ["analyst"]}
KEY = "sk-live-0123456789abcdefghijWXYZ"


class SecretsStore:
    def __init__(self, fail_get=False):
        self.data: dict = {}
        self.fail_get = fail_get

    async def get(self, path):
        if self.fail_get:
            raise RuntimeError("backend down")
        return self.data.get(path)

    async def put(self, path, data):
        self.data[path] = data

    async def delete(self, path):
        self.data.pop(path, None)


class Redis:
    """Records every key written, to prove the API key never goes there."""
    def __init__(self):
        self.writes: list[tuple] = []
        self.deleted: list[str] = []

    async def set(self, *a, **k):
        self.writes.append(a)

    async def delete(self, *keys):
        self.deleted += list(keys)


class Providers:
    """A fake OpenAI, Anthropic and gateway, answering at the HTTP layer; keeps every request it receives."""
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.valid_keys = {KEY}
        self.status_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if self.status_override:
            return httpx.Response(self.status_override, json={"error": {"message": "the model `gpt-9` does not exist"}})
        if host == "api.openai.com":
            if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid_keys:
                return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})
            return httpx.Response(200, json={"model": "gpt-4o-2024", "choices": [{"message": {"content": "from openai"}}],
                                             "usage": {"prompt_tokens": 11, "completion_tokens": 7}})
        if host == "api.anthropic.com":
            if request.headers.get("x-api-key") not in self.valid_keys:
                return httpx.Response(401, json={"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5-5"}]})
            return httpx.Response(200, json={"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "from claude"}],
                                             "usage": {"input_tokens": 13, "output_tokens": 5}})
        # the in-perimeter gateway
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"model": "codegen-generation", "choices": [{"message": {"content": "from gateway"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def sent_to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


@pytest.fixture
def world(monkeypatch):
    """Secrets store + Redis + providers faked; every cache cleared."""
    import app.storage.secrets as secrets_mod
    import app.services.execution_router as router_mod

    store, redis, providers = SecretsStore(), Redis(), Providers()
    monkeypatch.setattr(secrets_mod, "get_secrets_backend", lambda: store)
    monkeypatch.setattr(router_mod, "get_redis", lambda: redis)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(providers.handler), **kw))
    cp.reset_cache()
    yield SimpleNamespace(store=store, redis=redis, providers=providers)
    cp.reset_cache()


def _complete(**kw):
    async def go():
        client = ModelClient()
        try:
            return await client.complete(model="codegen-generation", system="sys", messages=[{"role": "user", "content": "hi"}], **kw)
        finally:
            await client.aclose()
    return asyncio.run(go())


@pytest.fixture
def api(world):
    from app.services import audit_service, workspace_service

    audits = []

    async def ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4())

    async def record(self, **kw):
        audits.append(kw)
    monkeypatch_targets = [(workspace_service.WorkspaceService, "ensure_user", ensure_user), (audit_service.AuditService, "record", record)]
    originals = [(c, n, getattr(c, n)) for c, n, _ in monkeypatch_targets]
    for c, n, f in monkeypatch_targets:
        setattr(c, n, f)

    async def _db():
        yield SimpleNamespace()
    app.dependency_overrides[get_db] = _db
    world.audits = audits
    yield TestClient(app)
    app.dependency_overrides.clear()
    for c, n, f in originals:
        setattr(c, n, f)


def _put(api, provider, key=None, who=ADMIN):
    return api.put("/api/v1/settings/codegen/agent", json={"provider": provider, "api_key": key}, headers=_headers(**who))


# ── who may change it, and what anyone sees ──────────────────────────────────────────────────────────────────────────────
def test_only_a_workspace_admin_can_change_or_check_the_agent(api, world):
    assert _put(api, "openai", KEY, ANALYST).status_code == 403
    assert api.post("/api/v1/settings/codegen/verify", json={"provider": "openai", "api_key": KEY}, headers=_headers(**ANALYST)).status_code == 403
    assert world.store.data == {} and world.providers.requests == []              # nothing saved, no key sent anywhere


def test_everyone_sees_the_active_agent_but_only_an_admin_sees_even_a_masked_key(api, world):
    assert _put(api, "openai", KEY).status_code == 200
    admin = api.get("/api/v1/settings/codegen/agent", headers=_headers(**ADMIN)).json()
    analyst = api.get("/api/v1/settings/codegen/agent", headers=_headers(**ANALYST)).json()
    assert admin["provider"] == analyst["provider"] == "openai" and admin["has_key"] and analyst["has_key"]
    assert admin["can_edit"] is True and analyst["can_edit"] is False
    assert admin["masked_key"] == "sk-l••••••••WXYZ" and analyst["masked_key"] is None
    for body in (admin, analyst):
        assert KEY not in json.dumps(body)


def test_a_rejected_key_is_not_saved_and_the_old_agent_stays(api, world):
    r = _put(api, "openai", "sk-wrong")
    assert r.status_code == 400 and "rejected the API key" in r.json()["detail"]["message"]
    assert world.store.data == {}
    assert api.get("/api/v1/settings/codegen/agent", headers=_headers(**ADMIN)).json()["provider"] == "codegen"


def test_a_key_is_required_for_a_provider_unless_the_same_one_is_already_saved(api, world):
    assert _put(api, "anthropic", None).status_code == 400
    assert _put(api, "anthropic", KEY).status_code == 200
    assert _put(api, "anthropic", None).status_code == 200                           # re-verify the saved key
    assert _put(api, "openai", None).status_code == 400                              # another provider's key is not reused


def test_the_key_is_stored_in_the_secrets_store_only_never_redis_or_the_audit_trail(api, world):
    assert _put(api, "openai", KEY).status_code == 200
    assert world.store.data[cp.CONFIG_SECRET_PATH]["api_key"] == KEY
    assert world.redis.writes == []                                                  # nothing written to Redis at all
    assert cp.LEGACY_REDIS_KEY in world.redis.deleted                                # and an old plain-text copy is removed
    assert len(world.audits) == 1 and world.audits[0]["action"] == "codegen.agent_updated"
    assert world.audits[0]["metadata"] == {"provider": "openai", "previous_provider": "codegen", "key_saved": True}
    assert KEY not in json.dumps(world.audits, default=str)


def test_choosing_the_default_again_removes_the_saved_key(api, world):
    _put(api, "openai", KEY)
    assert _put(api, "codegen").status_code == 200
    assert world.store.data[cp.CONFIG_SECRET_PATH]["api_key"] is None
    assert api.get("/api/v1/settings/codegen/agent", headers=_headers(**ADMIN)).json()["has_key"] is False


def test_an_unknown_provider_is_refused(api):
    assert _put(api, "gemini", KEY).status_code == 422


# ── every answer comes from the chosen provider ──────────────────────────────────────────────────────────────────────────
def test_with_nothing_chosen_the_in_perimeter_gateway_answers(world):
    assert _complete().text == "from gateway"
    assert world.providers.sent_to("api.openai.com") == [] and world.providers.sent_to("api.anthropic.com") == []


def test_openai_answers_with_its_configured_model_and_the_saved_key(world):
    asyncio.run(cp.save_codegen_agent_config("openai", KEY))
    out = _complete(json_mode=True, max_tokens=300)
    assert (out.text, out.input_tokens, out.output_tokens) == ("from openai", 11, 7)
    (req,) = world.providers.sent_to("api.openai.com")
    body = json.loads(req.content)
    assert req.url.path == "/v1/chat/completions" and req.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == get_settings().codegen_openai_model == "gpt-4o"
    assert body["messages"][0] == {"role": "system", "content": "sys"} and body["max_completion_tokens"] == 300
    assert body["response_format"] == {"type": "json_object"}
    assert [r.url.host for r in world.providers.requests] == ["api.openai.com"]      # the gateway was never asked


def test_anthropic_answers_with_its_configured_model_and_the_saved_key(world):
    asyncio.run(cp.save_codegen_agent_config("anthropic", KEY))
    out = _complete(json_mode=True)
    assert (out.text, out.input_tokens, out.output_tokens) == ("from claude", 13, 5)
    (req,) = world.providers.sent_to("api.anthropic.com")
    body = json.loads(req.content)
    assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == get_settings().codegen_anthropic_model == "claude-sonnet-5-5"
    assert body["messages"] == [{"role": "user", "content": "hi"}] and "sys" in body["system"] and "JSON" in body["system"]
    assert [r.url.host for r in world.providers.requests] == ["api.anthropic.com"]


def test_the_models_are_settings_not_code(world, monkeypatch):
    asyncio.run(cp.save_codegen_agent_config("anthropic", KEY))
    monkeypatch.setattr(get_settings(), "codegen_anthropic_model", "claude-future-1")
    _complete()
    assert json.loads(world.providers.sent_to("api.anthropic.com")[0].content)["model"] == "claude-future-1"


def test_a_provider_failure_is_reported_with_its_reason_and_never_falls_back_to_the_gateway(world):
    asyncio.run(cp.save_codegen_agent_config("openai", KEY))
    world.providers.status_override = 404
    with pytest.raises(ModelProviderError) as err:
        _complete()
    assert "OpenAI returned HTTP 404" in str(err.value) and "does not exist" in str(err.value) and KEY not in str(err.value)
    assert [r.url.host for r in world.providers.requests] == ["api.openai.com"]


def test_a_revoked_key_fails_the_turn_with_a_clear_reason(world):
    asyncio.run(cp.save_codegen_agent_config("anthropic", KEY))
    world.providers.valid_keys = set()
    with pytest.raises(ModelProviderError, match="the API key was rejected"):
        _complete()


def test_an_unreadable_setting_fails_the_turn_instead_of_guessing_a_provider(world):
    world.store.fail_get = True
    cp.reset_cache()
    with pytest.raises(ModelProviderError, match="could not be read"):
        _complete()
    assert world.providers.requests == []


def test_a_saved_provider_without_a_key_fails_instead_of_using_the_gateway(world):
    world.store.data[cp.CONFIG_SECRET_PATH] = {"provider": "openai", "api_key": None}
    with pytest.raises(ModelProviderError, match="no API key is saved"):
        _complete()
    assert world.providers.requests == []


def test_a_change_takes_effect_for_the_next_answer(world):
    assert _complete().text == "from gateway"
    asyncio.run(cp.save_codegen_agent_config("openai", KEY))
    assert _complete().text == "from openai"
    asyncio.run(cp.save_codegen_agent_config("codegen"))
    assert _complete().text == "from gateway"


# ── the assistant's model list follows the agent ─────────────────────────────────────────────────────────────────────────
def test_with_an_external_agent_the_model_list_has_one_entry_naming_it(api, world):
    default = api.get("/api/v1/codegen/models", headers=_headers(**ANALYST)).json()["models"]
    assert [m["name"] for m in default] == get_settings().codegen_generation_models
    # the labels must name the checkpoints docker/litellm/config.yaml really serves behind those logical names
    assert [m["label"] for m in default] == ["Default (inai-codegen)", "Alternate (Qwen3-Coder 30B)"]
    _put(api, "anthropic", KEY)
    (only,) = api.get("/api/v1/codegen/models", headers=_headers(**ANALYST)).json()["models"]
    assert only["label"] == "Anthropic · claude-sonnet-5-5" and only["is_default"] is True


# ── the open file's content follows the provider that will read it ───────────────────────────────────────────────────────
PRINCIPAL = Principal(user_id="u", org_id="o", roles=[], email="u@x", issued_at=0, expires_at=0)


@pytest.fixture
def policy(monkeypatch):
    row = SimpleNamespace(allow_external_context=True, allowed_providers=["codegen_gateway"], redact_secrets=True)
    ucs_calls = []

    async def get_org_settings(db):
        return row

    async def authorize(principal, *, action, object_name):
        ucs_calls.append(object_name)
    monkeypatch.setattr(gov, "get_org_settings", get_org_settings)
    monkeypatch.setattr(gov, "ucs_authorize", authorize)
    monkeypatch.setattr(get_settings(), "codegen_gateway_in_perimeter", True)
    monkeypatch.setattr(get_settings(), "codegen_ucs_model_object", None)
    return SimpleNamespace(row=row, ucs=ucs_calls)


def _decide(provider):
    return asyncio.run(gov.decide_context_inclusion(None, PRINCIPAL, workspace_id=uuid.uuid4(), raw_context="select 1",
                                                    user_opted_in=True, provider=provider))


def test_an_org_that_allowed_the_gateway_has_not_allowed_openai(policy):
    assert _decide("codegen_gateway")[1].include_context is True
    assert _decide("openai")[1].reason == "provider_not_allowlisted"
    assert _decide("anthropic")[1].reason == "provider_not_allowlisted"


def test_an_external_provider_is_outside_the_perimeter_even_when_the_gateway_is_inside(policy):
    policy.row.allowed_providers = ["codegen_gateway", "openai"]
    assert _decide("openai")[1].reason == "ucs_object_not_configured"              # fail closed: no governed model object
    get_settings().codegen_ucs_model_object = "ml_models.codegen.openai"
    content, decision = _decide("openai")
    assert decision.reason == "allowed_external" and content == "select 1"
    assert policy.ucs == ["ml_models.codegen.openai"]                                # UCS was asked
    assert _decide("codegen_gateway")[1].reason == "allowed_in_perimeter"            # the gateway is unchanged


def test_an_unknown_provider_sends_nothing(policy):
    assert _decide(None) == (None, gov.ContextDecision(include_context=False, reason="provider_unknown"))


def test_the_api_names_the_provider_that_will_answer(world):
    from app.api.codegen import _active_governance_provider

    assert asyncio.run(_active_governance_provider()) == "codegen_gateway"
    asyncio.run(cp.save_codegen_agent_config("openai", KEY))
    assert asyncio.run(_active_governance_provider()) == "openai"
    world.store.fail_get = True
    cp.reset_cache()
    assert asyncio.run(_active_governance_provider()) is None
