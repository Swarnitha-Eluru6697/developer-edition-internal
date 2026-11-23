"""Models deployed in ML Studio, in the Builder's model dropdown.

Asked for: "wherever the model catalog is shown while creating agents, the
ML models must also be shown". ML Studio LLM deployments are selectable as
``mlstudio/<model_id>`` and run through ML Studio's serving endpoint with the
endpoint's client secret; classical ML models are listed but disabled (they
take feature columns, not a prompt). Every Integrations LLM provider shows up
as a group even before it has models.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app import mlstudio_client
from app.db import engine
from app.main import app
from app.model_adapter import LLMCallError, call_llm
from app.models import MlStudioCredential, ModelProvider

client = TestClient(app)

DEPLOYMENTS = [
    {"modelId": "ticket-triage-qwen05b", "domain": "llm", "task": "Chat", "currentVersion": 2,
     "deployedBy": "neha", "endpointStatus": "active"},
    {"modelId": "churn-xgboost", "domain": "ml", "task": "Classification", "currentVersion": 3,
     "deployedBy": "neha", "endpointStatus": "active"},
]


@pytest.fixture(autouse=True)
def _clean_credentials():
    with Session(engine) as s:
        s.exec(delete(MlStudioCredential))
        s.commit()
    yield
    with Session(engine) as s:
        s.exec(delete(MlStudioCredential))
        s.commit()


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.setattr(mlstudio_client, "list_deployments", lambda timeout=5.0: DEPLOYMENTS)


def _mlstudio_options():
    r = client.get("/api/integrations/llm-models")
    assert r.status_code == 200, r.text
    return {m["name"]: m for m in r.json()["models"] if m["provider"] == "ML Studio"}


def test_llm_needs_its_client_secret_and_classical_ml_is_never_selectable(deployed):
    opts = _mlstudio_options()
    llm, ml = opts["mlstudio/ticket-triage-qwen05b"], opts["mlstudio/churn-xgboost"]
    assert llm["available"] is False and "client secret" in llm["note"]
    assert ml["available"] is False and "feature columns" in ml["note"]
    assert llm["supports_tool_calling"] is False

    r = client.put("/api/integrations/mlstudio-credentials/ticket-triage-qwen05b",
                   json={"client_secret": "cs_live_x"})
    assert r.status_code == 200, r.text
    opts = _mlstudio_options()
    assert opts["mlstudio/ticket-triage-qwen05b"]["available"] is True
    assert opts["mlstudio/churn-xgboost"]["available"] is False


def test_credentials_are_write_only_and_encrypted(deployed):
    client.put("/api/integrations/mlstudio-credentials/ticket-triage-qwen05b", json={"client_secret": "cs_live_x"})
    listed = client.get("/api/integrations/mlstudio-credentials").json()
    assert listed == {"models": ["ticket-triage-qwen05b"]}
    with Session(engine) as s:
        assert s.get(MlStudioCredential, "ticket-triage-qwen05b").client_secret != "cs_live_x"
    client.delete("/api/integrations/mlstudio-credentials/ticket-triage-qwen05b")
    assert client.get("/api/integrations/mlstudio-credentials").json() == {"models": []}


def test_ml_studio_down_is_shown_not_hidden(monkeypatch):
    def boom(timeout=5.0):
        raise mlstudio_client.MlStudioError("couldn't reach ML Studio")
    monkeypatch.setattr(mlstudio_client, "list_deployments", boom)
    opts = _mlstudio_options()
    assert len(opts) == 1
    (only,) = opts.values()
    assert only["placeholder"] is True and only["available"] is False


def test_every_llm_provider_appears_even_without_models():
    with Session(engine) as s:
        created = not s.get(ModelProvider, "Azure AI Studio")
        if created:
            s.add(ModelProvider(name="Azure AI Studio", version="1", status="needs_key", types=["LLM"]))
            s.commit()
    try:
        models = client.get("/api/integrations/llm-models").json()["models"]
        azure = [m for m in models if m["provider"] == "Azure AI Studio"]
        assert azure and all(m["available"] is False for m in azure)
    finally:
        if created:
            with Session(engine) as s:
                s.delete(s.get(ModelProvider, "Azure AI Studio"))
                s.commit()


def test_call_llm_runs_an_ml_studio_llm_with_its_stored_secret(monkeypatch):
    client.put("/api/integrations/mlstudio-credentials/ticket-triage-qwen05b", json={"client_secret": "cs_live_x"})
    seen = {}

    def fake_generate(model_id, secret, messages, **kw):
        seen.update(model_id=model_id, secret=secret, messages=messages, **kw)
        return {"text": "Route to billing.", "truncated": False, "stand_in": False}

    monkeypatch.setattr(mlstudio_client, "generate", fake_generate)
    out = call_llm("My invoice is wrong", model="mlstudio/ticket-triage-qwen05b", system="Triage tickets.",
                   history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                   max_tokens=64)
    assert out["text"] == "Route to billing."
    assert seen["model_id"] == "ticket-triage-qwen05b" and seen["secret"] == "cs_live_x"
    assert [m["role"] for m in seen["messages"]] == ["system", "user", "assistant", "user"]
    assert seen["max_new_tokens"] == 64


def test_call_llm_refuses_tools_and_missing_secret():
    with pytest.raises(LLMCallError, match="client secret"):
        call_llm("hi", model="mlstudio/ticket-triage-qwen05b")
    with pytest.raises(LLMCallError, match="tool-calling"):
        call_llm("hi", model="mlstudio/ticket-triage-qwen05b", tools=[{"name": "t"}])
