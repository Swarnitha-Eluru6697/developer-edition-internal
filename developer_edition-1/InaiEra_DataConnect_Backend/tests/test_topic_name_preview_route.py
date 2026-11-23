"""FE-BUG-08: GET /pipelines/topic-name-preview — the "New pipeline" wizard's
way to show the real, canonical topic name (see app/services/topic_naming.py,
BUG-01) as the identity fields are filled in, instead of reconstructing the
name itself and risking drift from the backend's own convention."""

from conftest import DEFAULT_GATEWAY_HEADERS, gateway_headers
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)

ORG_ID = "test_org_topic_preview"
ORG_HEADER = {"X-Tenant-Id": ORG_ID}


def test_topic_name_preview_matches_the_canonical_dash_separated_contract() -> None:
    response = client.get(
        "/api/v1/dataconnect/pipelines/topic-name-preview",
        params={
            "zone": "raw", "domain": "erp", "source_system": "sap",
            "entity": "customers", "mode": "cdc", "version": "v1",
        },
        headers=ORG_HEADER,
    )

    assert response.status_code == 200
    assert response.json() == {"topic_name": "raw-erp-sap-customers-cdc-v1"}


def test_topic_name_preview_rejects_an_invalid_version() -> None:
    response = client.get(
        "/api/v1/dataconnect/pipelines/topic-name-preview",
        params={
            "zone": "raw", "domain": "erp", "source_system": "sap",
            "entity": "customers", "mode": "cdc", "version": "not-a-version",
        },
        headers=ORG_HEADER,
    )

    assert response.status_code == 400


def test_topic_name_preview_requires_pipelines_view_permission() -> None:
    """Proves the route is open to a pipelines:view-only caller, like every
    other pipeline read route (and closed without it)."""
    response = client.get(
        "/api/v1/dataconnect/pipelines/topic-name-preview",
        params={
            "zone": "raw", "domain": "erp", "source_system": "sap",
            "entity": "customers", "mode": "cdc", "version": "v1",
        },
        headers={**gateway_headers(permissions=["dataconnect:pipelines:view"]), **ORG_HEADER},
    )

    assert response.status_code == 200
    denied = client.get(
        "/api/v1/dataconnect/pipelines/topic-name-preview",
        params={"zone": "raw", "domain": "erp", "source_system": "sap", "entity": "customers", "mode": "cdc", "version": "v1"},
        headers={**gateway_headers(permissions=["dataconnect:connections:view"]), **ORG_HEADER},
    )
    assert denied.status_code == 403
