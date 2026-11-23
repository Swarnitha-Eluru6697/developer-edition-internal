"""FE-BUG-14: PATCH /pipelines/{id} with a stale expected_revision, exercised
through the real HTTP route so the 409's error envelope shape (code, status,
details.current) is proven end-to-end, not just at the service layer — see
tests/test_pipeline_update.py for the service-level behavior itself."""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)

ORG_ID = "test_org"
ORG_HEADER = {"X-Tenant-Id": ORG_ID}


async def _noop_record_audit(**kwargs) -> None:
    pass


async def _seed_pipeline(fake_postgres) -> dict:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": ORG_ID, "connection_config": {}})
    payload = PipelineCreate(
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    )
    return await pipeline_service.create_pipeline(ORG_ID, payload)


@pytest.mark.asyncio
async def test_patch_with_current_revision_succeeds_over_http(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop_record_audit)
    created = await _seed_pipeline(fake_postgres)

    resp = client.patch(
        f"/api/v1/dataconnect/pipelines/{created['id']}", headers=ORG_HEADER,
        json={"name": "Renamed", "expected_revision": created["revision"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["revision"] == created["revision"] + 1


@pytest.mark.asyncio
async def test_patch_with_stale_revision_returns_409_with_latest_state(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop_record_audit)
    created = await _seed_pipeline(fake_postgres)

    # Integrator B saves first, through the real route.
    b_resp = client.patch(
        f"/api/v1/dataconnect/pipelines/{created['id']}", headers=ORG_HEADER,
        json={"name": "B's name", "expected_revision": created["revision"]},
    )
    assert b_resp.status_code == 200

    # Integrator A, still on the pre-edit revision, tries to save.
    a_resp = client.patch(
        f"/api/v1/dataconnect/pipelines/{created['id']}", headers=ORG_HEADER,
        json={"name": "A's name", "expected_revision": created["revision"]},
    )

    assert a_resp.status_code == 409
    body = a_resp.json()
    assert body["code"] == "PIPELINE_VERSION_CONFLICT"
    assert body["retryable"] is False
    assert body["details"]["current"]["name"] == "B's name"

    # B's save is genuinely intact.
    reloaded = client.get(f"/api/v1/dataconnect/pipelines/{created['id']}", headers=ORG_HEADER)
    assert reloaded.json()["name"] == "B's name"
