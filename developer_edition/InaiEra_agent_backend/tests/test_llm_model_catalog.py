"""The Builder's model dropdown must show the models this workspace has.

Reported: "why are all models not showing in model providers — most of
the types are configured with api keys?" The keys were fine. The
dropdown was a hardcoded list of five names with a single
"(default — not yet wired to a real distinct API call)" placeholder
standing in for every other provider, so a workspace with gemini-2.5-pro,
deepseek-chat and command-r registered could select none of them — from a
field labelled "Model (from Model catalog)" that never read the catalog.

Three defects, and the tests below pin each so it cannot come back:

  1. Registered models were invisible.
  2. The placeholder was false. DeepSeek/Cohere/Gemini/Hugging Face Hub
     all reach a real adapter via _OPENAI_COMPATIBLE_BASES and Replicate
     via _call_replicate; only Amazon Bedrock is genuinely unwired.
  3. The placeholder's own VALUE could not run: "DeepSeek (default)" is
     neither a built-in nor a registered model, so call_llm raised
     "Unrecognized model". The only option those providers offered was
     guaranteed to fail.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.db import engine
from app.main import app
from app.model_adapter import _MODEL_MAP, _MODEL_PROVIDERS, RUNTIME_SUPPORTED_PROVIDERS
from app.models import ModelProvider, ProviderModel

client = TestClient(app)


def _models():
    r = client.get("/api/integrations/llm-models")
    assert r.status_code == 200, r.text
    return r.json()["models"]


def _by_name():
    return {m["name"]: m for m in _models()}


def _ensure_provider(provider_name: str) -> bool:
    """ProviderModel has a real foreign key to ModelProvider, and a
    scratch database has not been through the seeder, so a test cannot
    assume the provider catalog exists. Returns True if it created the
    row, so teardown removes only what the test added."""
    with Session(engine) as s:
        if s.get(ModelProvider, provider_name):
            return False
        s.add(ModelProvider(name=provider_name, version="1", status="credits",
                            types=["LLM"], api_key_configured=True))
        s.commit()
        return True


def _drop_model(name: str, provider_name: str, created_provider: bool) -> None:
    with Session(engine) as s:
        s.exec(delete(ProviderModel).where(ProviderModel.name == name))
        if created_provider:
            s.exec(delete(ModelProvider).where(ModelProvider.name == provider_name))
        s.commit()


@pytest.fixture
def registered_chat_model():
    """A chat model registered the way the Integrations page registers
    one, under a provider that has a real adapter."""
    name = f"catalog-test-chat-{uuid.uuid4().hex[:8]}"
    created = _ensure_provider("DeepSeek")
    with Session(engine) as s:
        s.add(ProviderModel(name=name, provider_name="DeepSeek",
                            api_model_id="deepseek-chat", types=["LLM"]))
        s.commit()
    yield name, True
    _drop_model(name, "DeepSeek", created)


@pytest.fixture
def registered_embedding_model():
    """An embedding model left at the Add-model form's default type of
    "LLM" — the exact shape of intfloat/e5-base-v2 in this workspace."""
    name = f"catalog-test-embed-{uuid.uuid4().hex[:8]}"
    created = _ensure_provider("Hugging Face Hub")
    with Session(engine) as s:
        s.add(ProviderModel(name=name, provider_name="Hugging Face Hub",
                            api_model_id="intfloat/e5-base-v2", types=["LLM"]))
        s.commit()
    yield name
    _drop_model(name, "Hugging Face Hub", created)


class TestTheReportedGap:
    def test_a_registered_model_appears_in_the_catalog(self, registered_chat_model):
        name, _had_key = registered_chat_model
        assert name in _by_name(), "a model registered on the Integrations page is not offered"

    def test_the_built_in_models_are_still_there(self):
        names = _by_name()
        for builtin in _MODEL_PROVIDERS:
            assert builtin in names, f"{builtin} disappeared from the catalog"
            assert names[builtin]["builtin"] is True

    def test_every_offered_model_is_one_call_llm_can_actually_resolve(self):
        """The heart of the bug: an option that cannot be executed must
        never be offered. Every name here is either a built-in or a real
        registered row — never a synthesised "<Provider> (default)"."""
        from app.mlstudio_client import model_id_of
        with Session(engine) as s:
            registered = {r.name for r in s.exec(select(ProviderModel)).all()}
        for model in _models():
            if model.get("placeholder"):
                # A provider with no models yet (or ML Studio unreachable) is
                # listed so it isn't silently missing — but never selectable.
                assert model["available"] is False, f"placeholder {model['name']!r} must not be selectable"
                continue
            if model_id_of(model["name"]):
                continue  # mlstudio/<id> — call_llm routes it to ML Studio
            assert model["name"] in _MODEL_PROVIDERS or model["name"] in registered, (
                f"{model['name']!r} is offered but is neither a built-in nor a registered model — "
                "call_llm would reject it as an unrecognized model"
            )
            assert not model["name"].endswith("(default)")

    def test_no_placeholder_option_is_produced_for_any_provider(self):
        for model in _models():
            assert "not yet wired" not in (model["note"] or "")
            assert "(default" not in model["name"]

    def test_a_provider_with_a_real_adapter_is_not_reported_as_unwired(self):
        """DeepSeek, Cohere, Gemini and Hugging Face Hub reach
        _call_openai through their own base URLs; Replicate has its own
        adapter. Labelling them "not wired" was the original falsehood."""
        for provider in ("DeepSeek", "Cohere", "Gemini", "Hugging Face Hub", "Replicate"):
            assert provider in RUNTIME_SUPPORTED_PROVIDERS
        for model in _models():
            if model["provider"] in RUNTIME_SUPPORTED_PROVIDERS and model["available"]:
                assert model["note"] is None


class TestWhatMustNotBeOffered:
    def test_an_embedding_model_is_not_offered_as_a_chat_model(self, registered_embedding_model):
        """It is registered as types=["LLM"] because that is the form's
        default, but calling it for chat fails in a way that looks like a
        provider outage. The embedding list claims it instead."""
        assert registered_embedding_model not in _by_name()

        r = client.get("/api/integrations/embedding-models")
        assert r.status_code == 200
        assert registered_embedding_model in {m["name"] for m in r.json()["models"]}, (
            "the model vanished from BOTH lists — excluded here, not claimed there"
        )

    def test_a_provider_with_no_adapter_is_listed_but_flagged(self):
        """Amazon Bedrock genuinely has no adapter (AWS SigV4 signing is
        not implemented). Its models stay visible with the reason —
        hiding them is what produced the original "why is it missing?"."""
        assert "Amazon Bedrock" not in RUNTIME_SUPPORTED_PROVIDERS
        for model in _models():
            if model["provider"] == "Amazon Bedrock":
                assert model["available"] is False
                assert "wired" in (model["note"] or "")


class TestAvailabilityIsReportedNotHidden:
    def test_a_model_whose_provider_has_no_key_is_listed_with_what_to_do(self):
        name = f"catalog-test-nokey-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(ModelProvider(name="Catalog Test Provider", version="1", status="needs_key",
                                types=["LLM"], api_key_configured=False))
            s.commit()
            s.add(ProviderModel(name=name, provider_name="Catalog Test Provider",
                                api_model_id="whatever", types=["LLM"]))
            s.commit()
        try:
            entry = _by_name().get(name)
            assert entry is not None, "a configured-but-keyless model must still be visible"
            assert entry["available"] is False
            assert entry["note"]
        finally:
            with Session(engine) as s:
                s.exec(delete(ProviderModel).where(ProviderModel.name == name))
                s.exec(delete(ModelProvider).where(ModelProvider.name == "Catalog Test Provider"))
                s.commit()

    def test_available_models_sort_ahead_of_unavailable_ones(self):
        models = _models()
        availability = [m["available"] for m in models]
        assert availability == sorted(availability, reverse=True), (
            "unusable models are mixed in among usable ones"
        )

    def test_every_entry_carries_what_the_dropdown_needs(self):
        for model in _models():
            assert set(model) >= {"name", "provider", "api_model_id", "builtin",
                                  "available", "types", "note"}
            assert model["name"]


class TestItCannotDriftFromTheRuntimeAgain:
    def test_the_endpoint_reads_the_adapter_list_rather_than_restating_it(self):
        import inspect

        from app.routers.integrations import list_llm_models

        src = inspect.getsource(list_llm_models)
        assert "RUNTIME_SUPPORTED_PROVIDERS" in src
        assert "_MODEL_PROVIDERS" in src
        # A second hardcoded list here would be the original bug again.
        assert "GPT-4o" not in src and "Claude Sonnet" not in src

    def test_built_in_api_ids_match_the_adapter_map(self):
        names = _by_name()
        for builtin, api_id in _MODEL_MAP.items():
            assert names[builtin]["api_model_id"] == api_id
