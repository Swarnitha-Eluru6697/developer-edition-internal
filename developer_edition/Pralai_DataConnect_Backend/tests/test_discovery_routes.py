"""FE-BUG-12: GET /discovery/{id}/objects's object_type/schema_name filters
and the new GET /discovery/{id}/facets route, exercised through the actual
HTTP routes rather than the service layer directly (see
tests/test_discovery_service.py for that)."""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.services.discovery_service import discovery_service

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)

ORG_ID = "test_org"
ORG_HEADER = {"X-Tenant-Id": ORG_ID}


async def _seed_run(fake_postgres) -> str:
    run = await discovery_service.create_run(ORG_ID, "conn-1")
    rows = [
        {"discovery_run_id": run["id"], "org_id": ORG_ID, "object_type": "schema", "schema_name": None, "name": "public"},
        {"discovery_run_id": run["id"], "org_id": ORG_ID, "object_type": "schema", "schema_name": None, "name": "sales"},
        {"discovery_run_id": run["id"], "org_id": ORG_ID, "object_type": "table", "schema_name": "public", "name": "customers"},
        {"discovery_run_id": run["id"], "org_id": ORG_ID, "object_type": "table", "schema_name": "sales", "name": "leads"},
    ]
    for values in rows:
        fake_postgres.create("discovery_objects", values)
    return run["id"]


@pytest.mark.asyncio
async def test_list_discovery_objects_filters_by_object_type(fake_postgres) -> None:
    run_id = await _seed_run(fake_postgres)

    resp = client.get(f"/api/v1/dataconnect/discovery/{run_id}/objects", headers=ORG_HEADER, params={"object_type": "table"})

    assert resp.status_code == 200
    body = resp.json()
    assert {o["name"] for o in body["objects"]} == {"customers", "leads"}


@pytest.mark.asyncio
async def test_list_discovery_objects_filters_by_schema_name(fake_postgres) -> None:
    run_id = await _seed_run(fake_postgres)

    resp = client.get(f"/api/v1/dataconnect/discovery/{run_id}/objects", headers=ORG_HEADER, params={"schema_name": "sales"})

    assert resp.status_code == 200
    body = resp.json()
    # Only the table row belongs to schema_name="sales" — the "sales" schema
    # object itself has schema_name=None (see the model docstring: schema
    # rows aren't nested under a schema_name, they *are* one).
    assert {o["name"] for o in body["objects"]} == {"leads"}


@pytest.mark.asyncio
async def test_get_discovery_facets_returns_distinct_values(fake_postgres) -> None:
    run_id = await _seed_run(fake_postgres)

    resp = client.get(f"/api/v1/dataconnect/discovery/{run_id}/facets", headers=ORG_HEADER)

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_names"] == ["public", "sales"]
    assert body["object_types"] == ["schema", "table"]
