from uuid import uuid4

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

import app.services.schema_service as schema_service_module
from app.main import app
from app.services.schema_service import SchemaService, strip_inference_metadata

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": "org-1"}

# schema_service.create_schema/get_schema/list_schemas are now Postgres-backed
# (see schema_service_module's AsyncSessionLocal/SchemaVersionRepository
# usage) — same limitation as every other Postgres-backed write introduced
# this session: there is no live Postgres in this sandbox, so a test that
# calls create_schema directly is exercised against a fake session/repository
# rather than a real one, following tests/test_connection_service_validation.py.


class _FakeSchemaRow:
    def __init__(self, values: dict) -> None:
        self.id = None
        self.org_id = None
        self.domain = None
        self.source_system = None
        self.entity = None
        self.version = "v1"
        self.schema_format = "json_schema"
        self.created_at = None
        self.schema_definition = None
        self.compatibility = "BACKWARD"
        self.status = "active"
        self.__dict__.update(values)


class _FakeSchemaVersionRepository:
    def __init__(self, session) -> None:
        pass

    async def create(self, values: dict) -> _FakeSchemaRow:
        values = dict(values)
        values.setdefault("id", str(uuid4()))
        return _FakeSchemaRow(values)


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        pass


def test_strip_inference_metadata_drops_observational_keys() -> None:
    fields = [
        {
            "name": "id", "type": "string", "nullable": False, "primary_key": True,
            "confidence": 1.0, "null_only": False, "fields": None, "items": None,
            "precision": None, "scale": None,
        }
    ]

    stripped = strip_inference_metadata(fields)

    assert "confidence" not in stripped[0]
    assert "null_only" not in stripped[0]
    assert stripped[0]["name"] == "id"
    assert stripped[0]["type"] == "string"
    assert stripped[0]["primary_key"] is True


def test_strip_inference_metadata_recurses_into_nested_fields_and_items() -> None:
    fields = [
        {
            "name": "address", "type": "object", "nullable": True, "primary_key": False,
            "confidence": 1.0, "null_only": False, "items": None, "precision": None, "scale": None,
            "fields": [
                {
                    "name": "city", "type": "string", "nullable": True, "primary_key": False,
                    "confidence": 0.9, "null_only": False, "fields": None, "items": None,
                    "precision": None, "scale": None,
                }
            ],
        },
        {
            "name": "tags", "type": "array", "nullable": True, "primary_key": False,
            "confidence": 1.0, "null_only": False, "fields": None, "precision": None, "scale": None,
            "items": {
                "name": "items", "type": "string", "nullable": True, "primary_key": False,
                "confidence": 1.0, "null_only": False, "fields": None, "items": None,
                "precision": None, "scale": None,
            },
        },
    ]

    stripped = strip_inference_metadata(fields)

    assert "confidence" not in stripped[0]
    assert "confidence" not in stripped[0]["fields"][0]
    assert "confidence" not in stripped[1]["items"]


@pytest.mark.asyncio
async def test_create_schema_strips_inference_metadata_even_if_caller_copies_it_verbatim(monkeypatch) -> None:
    monkeypatch.setattr(schema_service_module, "SchemaVersionRepository", _FakeSchemaVersionRepository)
    monkeypatch.setattr(schema_service_module, "AsyncSessionLocal", lambda: _FakeSession())

    service = SchemaService()
    payload = {
        "domain": "crm", "source_system": "sf", "entity": "accounts", "version": "v1",
        "schema_definition": {
            "fields": [
                {
                    "name": "id", "type": "string", "nullable": False, "primary_key": False,
                    "confidence": 0.42, "null_only": False, "fields": None, "items": None,
                    "precision": None, "scale": None,
                }
            ]
        },
    }

    created = await service.create_schema("org-1", payload)

    stored_field = created["schema_definition"]["fields"][0]
    assert "confidence" not in stored_field
    assert "null_only" not in stored_field


@pytest.mark.asyncio
async def test_infer_rejects_too_many_records(monkeypatch) -> None:
    from app.core.config import settings

    service = SchemaService()
    monkeypatch.setattr(settings, "schema_infer_max_records", 2)

    with pytest.raises(ValueError, match="limited to 2 records"):
        await service.infer("org-1", [{"a": 1}, {"a": 2}, {"a": 3}])


@pytest.mark.asyncio
async def test_infer_rejects_oversized_content_length(monkeypatch) -> None:
    from app.core.config import settings

    service = SchemaService()
    monkeypatch.setattr(settings, "schema_infer_max_payload_bytes", 100)

    with pytest.raises(ValueError, match="exceeds the 100-byte limit"):
        await service.infer("org-1", [{"a": 1}], content_length=1000)


def test_infer_route_rejects_oversized_payload_with_413(monkeypatch) -> None:
    async def _too_big(*args, **kwargs):
        raise ValueError("Schema inference is limited to 500 records per request (got 501).")

    monkeypatch.setattr("app.api.routes.schemas.schema_service.infer", _too_big)

    resp = client.post(
        "/api/v1/dataconnect/schemas/infer",
        headers=HDR,
        json={"records": [{"id": "1"}]},
    )

    assert resp.status_code == 413
    assert "limited to 500 records" in resp.json()["message"]


def test_infer_route_returns_result_with_run_id(monkeypatch) -> None:
    inferred = {
        "fields": [{"name": "id", "type": "string", "nullable": False, "primary_key": False,
                    "confidence": 1.0, "null_only": False, "fields": None, "items": None,
                    "precision": None, "scale": None}],
        "confidence": 1.0,
        "sample_size": 1,
        "inference_run_id": "run_1",
    }

    async def _infer(*args, **kwargs):
        return inferred

    monkeypatch.setattr("app.api.routes.schemas.schema_service.infer", _infer)

    resp = client.post(
        "/api/v1/dataconnect/schemas/infer",
        headers=HDR,
        json={"records": [{"id": "1"}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["inference_run_id"] == "run_1"
    assert body["sample_size"] == 1


def test_infer_route_requires_a_valid_tenant() -> None:
    resp = client.post(
        "/api/v1/dataconnect/schemas/infer",
        json={"records": [{"id": "1"}]},
        headers={"X-Tenant-Id": "x"},
    )

    assert resp.status_code in (400, 401, 403, 422)
