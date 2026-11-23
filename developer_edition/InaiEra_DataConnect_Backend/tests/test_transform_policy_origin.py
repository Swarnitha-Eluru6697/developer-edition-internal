"""A transform policy records how it was made — "custom" (the Transforms page's "+ New SMT") or "auto" (the pipeline form) — and an edit never changes it."""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.transform import TransformPolicy, TransformPolicyCreate
from app.services.transform_service import _policy_to_dict

client = TestClient(app, headers={**DEFAULT_GATEWAY_HEADERS, "X-Tenant-Id": "org_test"})
BASE = "/api/v1/dataconnect/transform-policies"
STEP = {"order": 1, "name": "k", "type": "ValueToKey", "scope": "value", "config": {"fields": "id"}}


async def _noop_audit(**_kwargs) -> None:
    return None


def test_origin_defaults_to_custom_and_only_knows_custom_and_auto():
    assert TransformPolicyCreate(name="p").origin == "custom"
    assert TransformPolicyCreate(name="p", origin="auto").origin == "auto"
    with pytest.raises(ValidationError):
        TransformPolicyCreate(name="p", origin="imported")
    assert TransformPolicy(id="1", org_id="o", name="p").origin == "custom"


def test_the_service_returns_the_stored_origin():
    class Row:
        id, org_id, name, description, transforms, origin, created_at = "1", "o", "p", None, [], "auto", None

    assert _policy_to_dict(Row())["origin"] == "auto"


def test_create_passes_the_origin_through_and_audits_it(monkeypatch):
    seen: dict = {}
    audited: dict = {}

    async def create_policy(org_id, data):
        seen.update(data)
        return {"id": "tp1", "org_id": org_id, "name": data["name"], "description": None, "transforms": data["transforms"], "origin": data["origin"]}

    async def audit(**kwargs):
        audited.update(kwargs)

    monkeypatch.setattr("app.api.routes.transforms.transform_service.create_policy", create_policy)
    monkeypatch.setattr("app.api.routes.transforms.record_audit", audit)
    monkeypatch.setattr("app.api.routes.transforms.generate_smt_config", lambda transforms: {})

    r = client.post(BASE, json={"name": "from-pipeline", "origin": "auto", "transforms": [STEP]})
    assert r.status_code == 201
    assert r.json()["origin"] == "auto"
    assert seen["origin"] == "auto"
    assert audited["after_state"]["origin"] == "auto"            # the audit trail says which kind it was

    r = client.post(BASE, json={"name": "by-hand", "transforms": [STEP]})
    assert r.json()["origin"] == "custom"                         # not sent: made by hand


def test_create_refuses_an_unknown_origin():
    assert client.post(BASE, json={"name": "x", "origin": "imported", "transforms": []}).status_code == 422


def test_an_edit_never_changes_the_origin(monkeypatch):
    stored = {"id": "tp1", "org_id": "org_test", "name": "n", "description": "d", "transforms": [STEP], "origin": "auto"}
    sent: dict = {}

    async def get_policy(org_id, pid):
        return dict(stored)

    async def update_policy(org_id, pid, data):
        sent.update(data)
        return {**stored, **{k: v for k, v in data.items() if k in ("name", "description", "transforms", "origin")}}

    monkeypatch.setattr("app.api.routes.transforms.transform_service.get_policy", get_policy)
    monkeypatch.setattr("app.api.routes.transforms.transform_service.update_policy", update_policy)
    monkeypatch.setattr("app.api.routes.transforms.record_audit", _noop_audit)
    monkeypatch.setattr("app.api.routes.transforms.generate_smt_config", lambda transforms: {})

    # An edit that says nothing about origin (the model default is "custom") must not turn an "auto" policy into a "custom" one.
    r = client.put(f"{BASE}/tp1", json={"name": "renamed", "transforms": [STEP]})
    assert r.status_code == 200
    assert "origin" not in sent
    assert r.json()["origin"] == "auto"
    # And an edit cannot change it on purpose either.
    client.put(f"{BASE}/tp1", json={"name": "renamed", "origin": "custom", "transforms": [STEP]})
    assert "origin" not in sent


def test_the_migration_chain_has_one_head_and_labels_existing_policies():
    import importlib.util
    from pathlib import Path

    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    spec = importlib.util.spec_from_file_location("m0054", versions / "0054_transform_policy_origin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.down_revision == "0053_dead_letter_topic_unique"
    assert mod._AUTO_DESCRIPTION == "Auto-created from selected source transformations"   # the text the pipeline form writes
    revisions = [p.read_text() for p in versions.glob("00*.py")]
    assert sum('down_revision = "0054_transform_policy_origin"' in r for r in revisions) == 1   # one line of history: only 0055_alerts builds on it
