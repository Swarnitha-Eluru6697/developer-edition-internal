"""FE-BUG-24: GET /pipelines/{id}/deploy-readiness exercised through the
real HTTP route, so the response envelope shape is proven end-to-end — see
tests/test_deploy_readiness.py for the service-level behavior itself."""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS, gateway_headers
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)

ORG_ID = "test_org_readiness"
ORG_HEADER = {"X-Tenant-Id": ORG_ID}


async def _seed_pipeline(fake_postgres) -> dict:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": ORG_ID, "connection_config": {}, "verification_status": "ok"},
    )
    payload = PipelineCreate(
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    )
    return await pipeline_service.create_pipeline(ORG_ID, payload)


@pytest.mark.asyncio
async def test_deploy_readiness_over_http(fake_postgres) -> None:
    created = await _seed_pipeline(fake_postgres)

    response = client.get(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy-readiness", headers=ORG_HEADER)

    assert response.status_code == 200
    body = response.json()
    assert body["pipeline_id"] == created["id"]
    assert body["topic_name"] == created["topic_name"]
    assert body["ready"] is True
    keys = {c["key"] for c in body["checks"]}
    assert keys == {
        "connection_verified", "connector_plugin_installed", "connector_compatibility",
        "source_table_exists", "topic_policy_valid", "kafka_brokers_sufficient", "topic_name_available", "schema_valid",
        "transform_configuration_valid", "quality_policy_valid", "secret_reference_resolved",
    }


def test_deploy_readiness_missing_pipeline_is_404_over_http(fake_postgres) -> None:
    response = client.get("/api/v1/dataconnect/pipelines/does-not-exist/deploy-readiness", headers=ORG_HEADER)
    assert response.status_code == 404


def test_deploy_readiness_requires_deploy_permission(fake_postgres) -> None:
    """A caller with dataconnect:pipelines:view but not pipelines:deploy —
    this route must reject them the same way POST .../deploy already does."""
    response = client.get(
        "/api/v1/dataconnect/pipelines/does-not-matter/deploy-readiness",
        headers={**gateway_headers(permissions=["dataconnect:pipelines:view"]), **ORG_HEADER},
    )
    assert response.status_code == 403
