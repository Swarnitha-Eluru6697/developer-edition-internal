from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


async def _noop_record_audit(**kwargs) -> None:
    return None


def test_health_endpoint():
    r = client.get("/api/v1/dataconnect/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "dataconnect"}


def test_catalog_create_list_get(monkeypatch):
    sample = {
        "id": "t1",
        "name": "Postgres CDC",
        "connector_type": "source",
        "category": "database",
        "connector_class": "io.debezium.connector.postgresql.PostgresConnector",
        "runtime_pool_type": "kafka-connect",
        "config_schema": {},
        "default_config": {},
        "default_transforms": [],
        "version": "v1",
        "is_active": True,
    }

    async def _create_template(payload):
        return sample

    async def _list_templates():
        return [sample]

    async def _get_template(tid):
        return sample if tid == "t1" else None

    monkeypatch.setattr(
        "app.api.routes.catalog.catalog_service.create_template",
        _create_template,
    )
    monkeypatch.setattr(
        "app.api.routes.catalog.catalog_service.list_templates",
        _list_templates,
    )
    monkeypatch.setattr(
        "app.api.routes.catalog.catalog_service.get_template",
        _get_template,
    )
    monkeypatch.setattr("app.api.routes.catalog.record_audit", _noop_record_audit)

    # create
    resp = client.post(
        "/api/v1/dataconnect/connector-templates",
        json={
            "name": "Postgres CDC",
            "connector_type": "source",
            "category": "database",
            "connector_class": "io.debezium.connector.postgresql.PostgresConnector",
            "runtime_pool_type": "kafka-connect",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "t1"
    assert body["name"] == "Postgres CDC"

    # list
    resp = client.get("/api/v1/dataconnect/connector-templates")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

    # get
    resp = client.get("/api/v1/dataconnect/connector-templates/t1")
    assert resp.status_code == 200
    assert resp.json()["connector_type"] == "source"


def test_connections_create_test_discover(monkeypatch):
    sample_conn = {
        "id": "c1",
        "name": "my-postgres",
        "source_type": "postgres",
        "connection_config": {"host": "db.local", "port": 5432},
        "org_id": "org-1",
    }

    async def _async_create(org_id, payload):
        return {**sample_conn, "org_id": org_id}

    async def _async_get_or_404(org_id, connection_id):
        return {**sample_conn, "org_id": org_id}

    monkeypatch.setattr(
        "app.api.routes.connections.connection_service.create_connection",
        _async_create,
    )
    monkeypatch.setattr(
        "app.api.routes.connections.connection_service.get_or_404",
        _async_get_or_404,
    )

    async def _async_test(conn):
        return {"status": "ok", "message": "connected", "details": {}}

    async def _async_probe_and_record(org_id, connection_id):
        return {"status": "ok", "message": "connected", "details": {}}

    monkeypatch.setattr(
        "app.api.routes.connections.connection_service.probe_and_record",
        _async_probe_and_record,
    )

    # discover is async now — submitted through operation_runner (queued
    # immediately, claimed and run by the operation worker; see app/services/
    # operation_runner.py) driving a DiscoveryRun (GAP-10), not awaited
    # directly and not returning a DiscoveryResult inline — so it's mocked
    # at the discovery_service/operation_runner boundary, not
    # connection_service (which no longer has a discover_objects method at
    # all: discovery moved to app/services/discovery_service.py entirely).
    async def _async_submit_operation(*, org_id, operation_type, resource_type, resource_id):
        return {
            "id": "op1", "org_id": org_id, "operation_type": operation_type,
            "resource_type": resource_type, "resource_id": resource_id,
            "status": "queued", "result": None, "error_message": None,
            "created_at": None, "started_at": None, "completed_at": None,
        }

    async def _async_create_run(org_id, connection_id):
        return {
            "id": "run1", "org_id": org_id, "connection_id": connection_id, "operation_id": None,
            "status": "queued", "object_count": 0, "continuation": None, "error_message": None,
            "created_at": None, "completed_at": None,
        }

    async def _async_set_operation_id(org_id, run_id, operation_id):
        pass

    monkeypatch.setattr(
        "app.api.routes.connections.connection_service.test_connection",
        _async_test,
    )
    monkeypatch.setattr(
        "app.api.routes.connections.operation_runner.create_and_run",
        _async_submit_operation,
    )
    monkeypatch.setattr("app.api.routes.connections.discovery_service.create_run", _async_create_run)
    monkeypatch.setattr("app.api.routes.connections.discovery_service.set_operation_id", _async_set_operation_id)
    monkeypatch.setattr("app.api.routes.connections.record_audit", _noop_record_audit)

    hdr = {"X-Tenant-Id": "org-1"}

    # create
    resp = client.post(
        "/api/v1/dataconnect/connections",
        headers=hdr,
        json={"name": "my-postgres", "source_type": "postgres", "connection_config": {"host": "db.local"}},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-postgres"
    assert data["org_id"] == "org-1"

    # test
    resp = client.post("/api/v1/dataconnect/connections/c1/test", headers=hdr)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # discover — async now: 202 with a pollable DiscoveryRun (GAP-10), not
    # the discovered objects inline. Poll GET /discovery/{id} for status/
    # object_count, then page through GET /discovery/{id}/objects.
    resp = client.post("/api/v1/dataconnect/connections/c1/discover", headers=hdr)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["connection_id"] == "c1"
    assert body["operation_id"] == "op1"


def test_pipelines_create_and_status(monkeypatch):
    sample_pipeline = {
        "id": "p1",
        "name": "ingest-orders",
        "template_id": "template-1",
        "connection_profile_id": "c1",
        "zone": "raw",
        "domain": "sales",
        "source_system": "orders_app",
        "entity": "orders",
        "mode": "cdc",
        "version": "v1",
        "org_id": "org-1",
        "topic_name": "sales.orders.raw.v1",
        "catalog_qualified_name": "sales__orders__raw__v1",
        "connector_name": "connector-p1",
        "generated_config": {"connector": "config"},
        "direction": "SOURCE",
    }

    async def _create_pipeline(org_id, payload):
        return {**sample_pipeline, "org_id": org_id}

    async def _get_or_404(org_id, pid):
        return {**sample_pipeline, "org_id": org_id}

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.create_pipeline", _create_pipeline)
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", _get_or_404)
    monkeypatch.setattr(
        "app.api.routes.pipelines.pipeline_service.build_status",
        lambda p, reconciliation=None: {
            "pipeline_id": p["id"],
            "status": "running",
            "connector_name": p["connector_name"],
            "topic_name": p["topic_name"],
            "runtime": {},
        },
    )
    async def _no_reconciliation_state(pid):
        return None

    monkeypatch.setattr(
        "app.api.routes.pipelines.pipeline_service.get_reconciliation_state",
        _no_reconciliation_state,
    )
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop_record_audit)

    hdr = {"X-Tenant-Id": "org-1"}

    resp = client.post(
        "/api/v1/dataconnect/pipelines",
        headers=hdr,
        json={
            "name": "ingest-orders",
            "template_id": "template-1",
            "connection_profile_id": "c1",
            "zone": "raw",
            "domain": "sales",
            "source_system": "orders_app",
            "entity": "orders",
            "mode": "cdc",
            "version": "v1",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "p1"
    assert body["topic_name"] == "sales.orders.raw.v1"

    # status
    resp = client.get("/api/v1/dataconnect/pipelines/p1/status", headers=hdr)
    assert resp.status_code == 200
    st = resp.json()
    assert st["status"] == "running"
    assert st["pipeline_id"] == "p1"


def test_transform_preview_and_validate(monkeypatch):
    policy = {
        "id": "tp1",
        "org_id": "org-1",
        "name": "mask-ssn",
        "transforms": [{"order": 1, "name": "mask_ssn", "type": "mask", "config": {"field": "ssn"}}],
    }

    # patch transform_service.get_policy and generate_smt_config
    async def _get_policy(org_id, pid):
        return policy

    monkeypatch.setattr("app.api.routes.transforms.transform_service.get_policy", _get_policy)
    monkeypatch.setattr("app.api.routes.transforms.generate_smt_config", lambda transforms: {"smt": "config"})

    hdr = {"X-Tenant-Id": "org-1"}

    # preview
    resp = client.post(
        "/api/v1/dataconnect/transform-policies/tp1/preview",
        headers=hdr,
        json={"input_record": {"id": "1", "ssn": "123-45-6789"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["input_record"]["id"] == "1"
    assert body["output_record"] == body["input_record"]
    assert body["generated_config"] == {"smt": "config"}

    # validate (should return valid True)
    resp = client.post("/api/v1/dataconnect/transform-policies/tp1/validate", headers=hdr)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True

