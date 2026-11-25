from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


def test_list_connection_config_schemas_returns_every_source_type() -> None:
    resp = client.get("/api/v1/dataconnect/connection-config-schemas")

    assert resp.status_code == 200
    body = resp.json()
    source_types = {entry["source_type"] for entry in body}
    # One canonical spelling is listed; "postgres" is only an accepted alias
    # (see SOURCE_TYPE_ALIASES), so the UI never offers the same type twice.
    assert "postgresql" in source_types
    assert "postgres" not in source_types
    assert "s3" in source_types


def test_get_connection_config_schema_for_postgres_exposes_field_metadata() -> None:
    resp = client.get("/api/v1/dataconnect/connection-config-schemas/postgres")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source_type"] == "postgres"
    password = next(f for f in body["fields"] if f["key"] == "password")
    assert password["type"] == "secret"
    assert password["persist"] is False
    assert password["required"] is True


def test_get_connection_config_schema_for_unknown_source_type_returns_empty_fields() -> None:
    resp = client.get("/api/v1/dataconnect/connection-config-schemas/no-such-type")

    assert resp.status_code == 200
    assert resp.json() == {"source_type": "no-such-type", "fields": []}


def test_the_postgres_alias_is_still_served_with_the_same_fields() -> None:
    """Old clients that ask for "postgres" keep working and get postgresql's fields."""
    alias = client.get("/api/v1/dataconnect/connection-config-schemas/postgres").json()
    canonical = client.get("/api/v1/dataconnect/connection-config-schemas/postgresql").json()
    assert [f["key"] for f in alias["fields"]] == [f["key"] for f in canonical["fields"]]
