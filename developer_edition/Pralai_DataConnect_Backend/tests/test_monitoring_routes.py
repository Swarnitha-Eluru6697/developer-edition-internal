from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": "org-1"}


def test_connector_status_route_404s_for_unowned_connector(monkeypatch) -> None:
    async def _status(org_id, connector_name):
        return None

    monkeypatch.setattr("app.api.routes.monitoring.monitoring_service.connector_status", _status)

    resp = client.get("/api/v1/dataconnect/runtime/connectors/some-other-apps-connector/status", headers=HDR)

    assert resp.status_code == 404


def test_connector_status_route_returns_the_mapped_status(monkeypatch) -> None:
    payload = {
        "name": "org-1--crm--salesforce--accounts--cdc--v1",
        "pipeline_id": "pipe_1",
        "topic_name": "org-1.raw.crm.salesforce.accounts.cdc.v1",
        "connector": {"state": "RUNNING"},
        "tasks": [],
        "observed_state": "running",
        "observed_at": "2026-01-01T00:00:00Z",
        "runtime_endpoint": "http://connect",
        "error": None,
    }

    async def _status(org_id, connector_name):
        assert org_id == "org-1"
        return payload

    monkeypatch.setattr("app.api.routes.monitoring.monitoring_service.connector_status", _status)

    resp = client.get(
        "/api/v1/dataconnect/runtime/connectors/org-1--crm--salesforce--accounts--cdc--v1/status",
        headers=HDR,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["pipeline_id"] == "pipe_1"
    assert body["topic_name"] == "org-1.raw.crm.salesforce.accounts.cdc.v1"


def test_connector_tasks_route_404s_for_unowned_connector(monkeypatch) -> None:
    async def _tasks(org_id, connector_name):
        return None

    monkeypatch.setattr("app.api.routes.monitoring.monitoring_service.connector_tasks", _tasks)

    resp = client.get("/api/v1/dataconnect/runtime/connectors/some-other-apps-connector/tasks", headers=HDR)

    assert resp.status_code == 404


def test_monitoring_routes_require_a_valid_tenant() -> None:
    resp = client.get("/api/v1/dataconnect/runtime/metrics/summary", headers={"X-Tenant-Id": "x"})

    assert resp.status_code == 400


def test_list_cluster_connectors_route_passes_org_id_through(monkeypatch) -> None:
    async def _list_cluster_connectors(org_id, cluster_id):
        assert org_id == "org-1"
        assert cluster_id == "connect-cdc-pool"
        return [
            {
                "pipeline_id": "pipe_1", "connector_name": "org-1--crm--salesforce--accounts--cdc--v1",
                "topic_name": "org-1.raw.crm.salesforce.accounts.cdc.v1", "runtime_pool_id": "connect-cdc-pool",
                "observed_state": "running", "observed_at": "2026-01-01T00:00:00Z", "runtime_error": None,
            }
        ]

    monkeypatch.setattr(
        "app.api.routes.monitoring.monitoring_service.list_cluster_connectors", _list_cluster_connectors
    )

    resp = client.get(
        "/api/v1/dataconnect/runtime/connect-clusters/connect-cdc-pool/connectors", headers=HDR
    )

    assert resp.status_code == 200
    assert resp.json()[0]["pipeline_id"] == "pipe_1"
