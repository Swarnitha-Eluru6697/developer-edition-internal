"""FE-BUG-24: POST /pipelines/{id}/deploy enforces the same readiness checks as the "Ready to
deploy" screen, so calling the API directly can't skip what the UI refuses. Refused BEFORE the
pipeline is claimed or an Operation created, so a refused deploy leaves nothing behind."""

import asyncio

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.errors import ValidationError
from app.main import app
from app.schemas.pipeline import PipelineCreate
from app.services.connection_service import connection_service
from app.services.pipeline_service import pipeline_service


async def _noop(*args, **kwargs) -> None:
    return None


client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
JDBC = "io.confluent.connect.jdbc.JdbcSourceConnector"


@pytest.fixture(autouse=True)
def _enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deploy_requires_readiness", True)

    async def _probe_keeps_it_failing(org_id, connection_id):
        return {"status": "error", "message": "unreachable", "details": {}}

    monkeypatch.setattr(connection_service, "probe_and_record", _probe_keeps_it_failing)


async def _pipeline(fake_postgres, org_id: str, *, verified: bool) -> dict:
    template = fake_postgres.create(
        "connector_templates", {"connector_class": JDBC, "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles",
        {"org_id": org_id, "name": "c", "connection_config": {}, "verification_status": "ok" if verified else "error"},
    )
    return await pipeline_service.create_pipeline(org_id, PipelineCreate(
        name="Orders", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    ))


@pytest.mark.asyncio
async def test_assert_ready_to_deploy_lists_every_failed_check(fake_postgres) -> None:
    created = await _pipeline(fake_postgres, "org_enf_1", verified=False)

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.assert_ready_to_deploy("org_enf_1", created["id"])

    assert "Connection verified" in str(exc_info.value)
    assert [f.field for f in exc_info.value.field_errors] == ["connection_verified"]


@pytest.mark.asyncio
async def test_assert_ready_to_deploy_passes_a_ready_pipeline(fake_postgres) -> None:
    created = await _pipeline(fake_postgres, "org_enf_2", verified=True)

    await pipeline_service.assert_ready_to_deploy("org_enf_2", created["id"])  # no exception


@pytest.mark.asyncio
async def test_the_setting_switches_enforcement_off(fake_postgres, monkeypatch) -> None:
    created = await _pipeline(fake_postgres, "org_enf_3", verified=False)
    monkeypatch.setattr(settings, "deploy_requires_readiness", False)

    await pipeline_service.assert_ready_to_deploy("org_enf_3", created["id"])  # not enforced


@pytest.mark.asyncio
async def test_a_deploy_over_http_is_refused_422_and_leaves_the_pipeline_untouched(fake_postgres) -> None:
    created = await _pipeline(fake_postgres, "org_enf_4", verified=False)

    response = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy", headers={"X-Tenant-Id": "org_enf_4"})

    assert response.status_code == 422
    body = response.json()
    assert "not ready to deploy" in body["message"]
    assert [f["field"] for f in body["field_errors"]] == ["connection_verified"]
    # Nothing was claimed or queued: still a draft, no operation recorded.
    assert (await pipeline_service.get_pipeline("org_enf_4", created["id"]))["status"] == "draft"
    assert not fake_postgres.operations


def test_a_ready_pipeline_is_deployed_normally(fake_postgres, monkeypatch) -> None:
    created = asyncio.run(_pipeline(fake_postgres, "org_enf_5", verified=True))
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop)

    response = client.post(f"/api/v1/dataconnect/pipelines/{created['id']}/deploy", headers={"X-Tenant-Id": "org_enf_5"})

    assert response.status_code == 202
    assert fake_postgres.operations  # the operation was created
