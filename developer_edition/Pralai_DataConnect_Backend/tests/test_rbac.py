"""
Permission-based access control, end to end (contract docs/auth/01-CONTRACT.md).

DataConnect checks permissions only. The role names below exist ONLY as test personas (tests/personas.py): the
permission lists the Admin Portal's seeded roles grant, sent the way Envoy forwards them (x-user-permissions).
These tests prove the behaviour those roles had before the move to permissions still holds, and that every
route really checks the permission it declares.

1. `test_persona_*` — properties of the seeded permission sets themselves.
2. `test_*_endpoint_*` — a representative HTTP request per gated route file through FastAPI's real TestClient,
   mocking only the service layer.
3. `test_get_route_enforces_its_permission` — each GET route 403s with every permission EXCEPT its own and
   200s with ONLY its own, so a route that checks nothing (or the wrong thing) fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import ALL_PERMISSIONS, gateway_headers
from fastapi.testclient import TestClient
from personas import ADMIN, INTEGRATOR, PERSONAS, VIEWER

from app.core.permissions import all_permissions, require
from app.db.session import get_session
from app.main import app

client = TestClient(app)

TENANT = "org-rbac-test"


def _role_header(persona: str | None) -> dict[str, str]:
    """Headers for a user holding the seeded role `persona` ("admin" / "integrator" / "viewer")."""
    if persona is None:
        return {}
    return gateway_headers(permissions=PERSONAS[persona], tenant_id=TENANT)


def _perm_header(*permissions: str) -> dict[str, str]:
    return gateway_headers(permissions=list(permissions), tenant_id=TENANT)


async def _noop_record_audit(**kwargs: Any) -> None:
    return None


# --------------------------------------------------------------------------
# 1. The seeded permission sets
# --------------------------------------------------------------------------


def test_every_seeded_permission_is_declared_in_permissions_json() -> None:
    assert ADMIN | INTEGRATOR | VIEWER <= all_permissions()


def test_permissions_json_ids_follow_the_contract_format() -> None:
    import re

    for pid in ALL_PERMISSIONS:
        assert re.fullmatch(r"dataconnect:[a-z][a-z-]*:[a-z][a-z-]*", pid), pid
    assert "dataconnect:access:view" in ALL_PERMISSIONS
    assert len(ALL_PERMISSIONS) == len(set(ALL_PERMISSIONS))


def test_require_refuses_an_undeclared_permission() -> None:
    with pytest.raises(ValueError, match="not declared"):
        require("dataconnect:connections:veiw")


def test_persona_admin_can_do_everything() -> None:
    assert PERSONAS["admin"] == all_permissions()


def test_persona_viewer_is_never_granted_a_mutating_permission() -> None:
    mutating = {p for p in all_permissions() if p.rsplit(":", 1)[1] not in {"view"}}
    assert not (PERSONAS["viewer"] & mutating)


def test_persona_integrator_matches_admin_except_templates_settings_and_alert_config() -> None:
    # Alert channels hold webhook URLs (credentials) and rules decide who gets paged: Admin only (Integrators may acknowledge / resolve).
    assert PERSONAS["admin"] - PERSONAS["integrator"] == {
        "dataconnect:templates:manage", "dataconnect:settings:edit", "dataconnect:alerts:manage",
    }
    assert PERSONAS["integrator"] <= PERSONAS["admin"]


# --------------------------------------------------------------------------
# 2. Representative end-to-end route checks
# --------------------------------------------------------------------------


def test_catalog_read_is_open_to_all_three_roles(monkeypatch: pytest.MonkeyPatch):
    """BUG-06: reading the connector catalog used to require
    templates:manage (Admin-only), which meant an Integrator
    couldn't even browse templates to build a pipeline. It's now gated on
    the separate dataconnect:templates:view permission (held by all three
    seeded roles) — see test_catalog_write_is_still_admin_only below for why
    mutating the catalog is unaffected by this and stays Admin-only."""
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", lambda: _async([]))

    for role in PERSONAS:
        resp = client.get("/api/v1/dataconnect/connector-templates", headers=_role_header(role))
        assert resp.status_code == 200, (role, resp.text)


def test_catalog_write_is_still_admin_only(monkeypatch: pytest.MonkeyPatch):
    payload = {
        "name": "t1", "connector_type": "source", "category": "database",
        "connector_class": "io.debezium.connector.postgresql.PostgresConnector",
        "runtime_pool_type": "shared",
    }
    created = {**payload, "id": "ct1", "current_revision_id": "rev1"}
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.create_template", lambda *a, **k: _async(created))
    monkeypatch.setattr("app.api.routes.catalog.record_audit", _noop_record_audit)

    resp = client.post("/api/v1/dataconnect/connector-templates", json=payload, headers=_role_header("viewer"))
    assert resp.status_code == 403

    resp = client.post("/api/v1/dataconnect/connector-templates", json=payload, headers=_role_header("integrator"))
    assert resp.status_code == 403

    resp = client.post("/api/v1/dataconnect/connector-templates", json=payload, headers=_role_header("admin"))
    assert resp.status_code == 201


def test_connection_delete_blocked_for_viewer_allowed_for_integrator(monkeypatch: pytest.MonkeyPatch):
    sample = {"id": "c1", "org_id": "org-rbac-test", "name": "n", "source_type": "postgres"}
    monkeypatch.setattr("app.api.routes.connections.connection_service.get_or_404", lambda *a, **k: _async(sample))
    monkeypatch.setattr("app.api.routes.connections.connection_service.delete_connection", lambda *a, **k: _async(None))
    monkeypatch.setattr("app.api.routes.connections.record_audit", _noop_record_audit)

    resp = client.delete("/api/v1/dataconnect/connections/c1", headers=_role_header("viewer"))
    assert resp.status_code == 403

    resp = client.delete("/api/v1/dataconnect/connections/c1", headers=_role_header("integrator"))
    assert resp.status_code == 204


def test_pipeline_pause_blocked_for_viewer_allowed_for_admin(monkeypatch: pytest.MonkeyPatch):
    before = {"id": "p1", "org_id": "org-rbac-test", "status": "deployed"}
    operation = {
        "id": "op1", "org_id": "org-rbac-test", "operation_type": "pipeline_pause",
        "resource_type": "pipeline", "resource_id": "p1", "status": "queued",
        "result": None, "error_message": None, "created_at": None, "started_at": None, "completed_at": None,
    }
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", lambda *a, **k: _async(before))
    # FE-BUG-25: _submit_lifecycle_operation now atomically claims the
    # pipeline before creating the Operation — mocked at that boundary too,
    # same reasoning as the operation_runner mock below.
    monkeypatch.setattr(
        "app.api.routes.pipelines.pipeline_service.claim_lifecycle_transition", lambda *a, **k: _async(before)
    )
    # pause is async now — submitted through operation_runner (queued
    # immediately, runs off the request thread) rather than awaited
    # directly, so it's mocked at that boundary, same as
    # test_audit_wiring.py's equivalent pipeline-lifecycle test.
    monkeypatch.setattr(
        "app.api.routes.pipelines.operation_runner.create_and_run", lambda *a, **k: _async(operation)
    )
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop_record_audit)

    resp = client.post("/api/v1/dataconnect/pipelines/p1/pause", headers=_role_header("viewer"))
    assert resp.status_code == 403

    resp = client.post("/api/v1/dataconnect/pipelines/p1/pause", headers=_role_header("admin"))
    assert resp.status_code == 202


def test_transform_policy_create_blocked_for_viewer_allowed_for_integrator(monkeypatch: pytest.MonkeyPatch):
    created = {"id": "tp1", "org_id": "org-rbac-test", "name": "t1", "transforms": []}
    monkeypatch.setattr("app.api.routes.transforms.transform_service.create_policy", lambda *a, **k: _async(created))
    monkeypatch.setattr("app.api.routes.transforms.record_audit", _noop_record_audit)

    resp = client.post(
        "/api/v1/dataconnect/transform-policies", json={"name": "t1"}, headers=_role_header("viewer")
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/dataconnect/transform-policies", json={"name": "t1"}, headers=_role_header("integrator")
    )
    assert resp.status_code == 201


def test_transform_policy_preview_still_blocked_for_viewer(monkeypatch: pytest.MonkeyPatch):
    """FE-BUG-19: preview stays transforms:edit-gated on purpose. See
    TransformViewModal (Transforms.tsx) for the actual read-only view a
    transforms:view-only user gets instead."""
    policy = {"id": "tp1", "org_id": "org-rbac-test", "name": "t1", "transforms": []}
    monkeypatch.setattr("app.api.routes.transforms.transform_service.get_policy", lambda *a, **k: _async(policy))

    resp = client.post(
        "/api/v1/dataconnect/transform-policies/tp1/preview",
        json={"input_record": {"id": "1"}},
        headers=_role_header("viewer"),
    )

    assert resp.status_code == 403


def test_topic_policy_create_blocked_for_viewer_allowed_for_admin(monkeypatch: pytest.MonkeyPatch):
    created = {"id": "tpc1", "org_id": "org-rbac-test", "name": "tp1"}
    monkeypatch.setattr(
        "app.api.routes.topic_policies.topic_policy_service.create_policy", lambda *a, **k: _async(created)
    )

    resp = client.post("/api/v1/dataconnect/topic-policies", json={"name": "tp1"}, headers=_role_header("viewer"))
    assert resp.status_code == 403

    resp = client.post("/api/v1/dataconnect/topic-policies", json={"name": "tp1"}, headers=_role_header("admin"))
    assert resp.status_code == 201


def test_schema_drift_approval_blocked_for_viewer_allowed_for_integrator(monkeypatch: pytest.MonkeyPatch):
    approved = {
        "id": "a1",
        "org_id": "org-rbac-test",
        "schema_version_id": "sv1",
        "observed_schema": {},
        "status": "approved",
    }
    monkeypatch.setattr("app.api.routes.schemas.schema_service.approve_drift", lambda *a, **k: _async(approved))
    monkeypatch.setattr("app.api.routes.schemas.record_audit", _noop_record_audit)

    resp = client.post(
        "/api/v1/dataconnect/schemas/drift-approvals/a1/approve",
        json={"reviewed_by": "someone"},
        headers=_role_header("viewer"),
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/dataconnect/schemas/drift-approvals/a1/approve",
        json={"reviewed_by": "someone"},
        headers=_role_header("integrator"),
    )
    assert resp.status_code == 200


def test_quality_policy_create_blocked_for_viewer_allowed_for_integrator(monkeypatch: pytest.MonkeyPatch):
    created = {"id": "q1", "org_id": "org-rbac-test", "name": "q1", "rules": []}
    monkeypatch.setattr("app.api.routes.quality.quality_service.create_policy", lambda *a, **k: _async(created))
    monkeypatch.setattr("app.api.routes.quality.record_audit", _noop_record_audit)

    resp = client.post(
        "/api/v1/dataconnect/quality-policies", json={"name": "q1"}, headers=_role_header("viewer")
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/dataconnect/quality-policies", json={"name": "q1"}, headers=_role_header("integrator")
    )
    assert resp.status_code == 201


def test_lineage_event_ingestion_blocked_for_viewer_allowed_for_admin(monkeypatch: pytest.MonkeyPatch):
    graph = {"nodes": [], "edges": []}
    monkeypatch.setattr(
        "app.api.routes.lineage.lineage_service.consume_runtime_event", lambda *a, **k: _async(graph)
    )
    payload = {
        "org_id": "org-rbac-test",
        "event_type": "created",
        "from_asset": {"id": "a"},
        "to_asset": {"id": "b"},
        "edge_type": "derives_from",
    }

    resp = client.post("/api/v1/dataconnect/lineage/events", json=payload, headers=_role_header("viewer"))
    assert resp.status_code == 403

    resp = client.post("/api/v1/dataconnect/lineage/events", json=payload, headers=_role_header("admin"))
    assert resp.status_code == 200



# --------------------------------------------------------------------------
# 3. GET routes actually enforce THEIR permission (BUG-05): every permission
#    except the route's own -> 403; only the route's own -> 200.
# --------------------------------------------------------------------------

_GET_ROUTES_ENFORCE_THEIR_PERMISSION = [
    pytest.param(
        "dataconnect:connections:view", "GET", "/connections",
        "app.api.routes.connections.connection_service.list_connections", [],
        id="connections",
    ),
    pytest.param(
        # lineage.router has no path prefix (see app/api/router.py) — list_assets
        # is registered at "/assets", not "/lineage/assets".
        "dataconnect:lineage:view", "GET", "/assets",
        "app.api.routes.lineage.lineage_service.list_assets", [],
        id="lineage",
    ),
    pytest.param(
        "dataconnect:monitoring:view", "GET", "/runtime/flow",
        "app.api.routes.monitoring.flow_summary",
        {"reachable": True, "connect_url": "", "bootstrap": "", "connectors": [], "topics": [], "sinks": [], "message": ""},
        id="monitoring",
    ),
    pytest.param(
        "dataconnect:pipelines:view", "GET", "/pipelines",
        "app.api.routes.pipelines.pipeline_service.list_pipelines", [],
        id="pipelines",
    ),
    pytest.param(
        "dataconnect:quality:view", "GET", "/quality-policies",
        "app.api.routes.quality.quality_service.list_policies", [],
        id="quality-policies",
    ),
    pytest.param(
        "dataconnect:quality:view", "GET", "/quality-results",
        "app.api.routes.quality.quality_service.list_results", [],
        id="quality-results",
    ),
    pytest.param(
        "dataconnect:schemas:view", "GET", "/schemas",
        "app.api.routes.schemas.schema_service.list_schemas", [],
        id="schemas",
    ),
    pytest.param(
        "dataconnect:pipelines:view", "GET", "/topic-policies",
        "app.api.routes.topic_policies.topic_policy_service.list_policies", [],
        id="topic-policies",
    ),
    pytest.param(
        "dataconnect:transforms:view", "GET", "/transform-policies",
        "app.api.routes.transforms.transform_service.list_policies", [],
        id="transforms",
    ),
    pytest.param(
        "dataconnect:templates:view", "GET", "/connector-templates",
        "app.api.routes.catalog.catalog_service.list_templates", [],
        id="templates",
    ),
]


@pytest.mark.parametrize(
    ("permission", "method", "path", "mock_target", "mock_return"),
    _GET_ROUTES_ENFORCE_THEIR_PERMISSION,
)
def test_get_route_enforces_its_permission(
    monkeypatch: pytest.MonkeyPatch, permission: str, method: str, path: str,
    mock_target: str, mock_return: Any,
) -> None:
    monkeypatch.setattr(mock_target, lambda *a, **k: _async(mock_return))
    everything_else = [p for p in ALL_PERMISSIONS if p != permission]

    resp = client.request(method, f"/api/v1/dataconnect{path}", headers=_perm_header(*everything_else))
    assert resp.status_code == 403
    assert permission in resp.json()["message"]

    resp = client.request(method, f"/api/v1/dataconnect{path}", headers=_perm_header(permission))
    assert resp.status_code == 200, resp.text


def test_get_operation_enforces_pipelines_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """operations.py's GET /{operation_id} requires dataconnect:pipelines:view
    (see that module's docstring) — its own case, since reusing a permission
    doesn't mean this specific route also depends on it (BUG-05)."""
    operation = {
        "id": "op1", "org_id": "org-rbac-test", "operation_type": "pipeline_deploy",
        "resource_type": "pipeline", "resource_id": "p1", "status": "succeeded",
        "result": None, "error_message": None, "created_at": None, "started_at": None, "completed_at": None,
    }
    monkeypatch.setattr("app.api.routes.operations.operation_runner.get_operation", lambda *a, **k: _async(operation))
    everything_else = [p for p in ALL_PERMISSIONS if p != "dataconnect:pipelines:view"]

    resp = client.get("/api/v1/dataconnect/operations/op1", headers=_perm_header(*everything_else))
    assert resp.status_code == 403

    resp = client.get("/api/v1/dataconnect/operations/op1", headers=_perm_header("dataconnect:pipelines:view"))
    assert resp.status_code == 200


def test_generated_config_blocked_for_viewer_allowed_for_integrator(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG-07: GET .../generated-config exposes source topology, connector
    class, and topic/transform wiring even after secret redaction — stricter
    than the plain pipeline view, so it needs dataconnect:pipelines:deploy
    (seeded Admin/Integrator), not pipelines:view."""
    pipeline = {
        "id": "p1", "org_id": "org-rbac-test", "status": "deployed",
        "generated_config": {"connector.class": "io.debezium.connector.postgresql.PostgresConnector"},
    }
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", lambda *a, **k: _async(pipeline))
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", _noop_record_audit)

    resp = client.get("/api/v1/dataconnect/pipelines/p1/generated-config", headers=_role_header("viewer"))
    assert resp.status_code == 403

    resp = client.get("/api/v1/dataconnect/pipelines/p1/generated-config", headers=_role_header("integrator"))
    assert resp.status_code == 200


def test_audit_log_view_is_open_to_all_three_roles():
    class _FakeScalars:
        def all(self) -> list[Any]:
            return []

    class _FakeResult:
        def scalars(self) -> _FakeScalars:
            return _FakeScalars()

    class _FakeSession:
        async def execute(self, *a: Any, **k: Any) -> _FakeResult:
            return _FakeResult()

    async def _override_get_session():
        yield _FakeSession()

    app.dependency_overrides[get_session] = _override_get_session
    try:
        for role in PERSONAS:
            resp = client.get("/api/v1/dataconnect/audit", headers=_role_header(role))
            assert resp.status_code == 200, (role, resp.text)
            assert resp.json() == []
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_missing_gateway_headers_is_rejected_not_defaulted(monkeypatch: pytest.MonkeyPatch):
    # Gate 1 fails closed (see app/core/gateway.py): a caller who sends no
    # identity at all is rejected, never given a default.
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", lambda: _async([]))
    resp = client.get("/api/v1/dataconnect/connector-templates")
    assert resp.status_code == 401


def test_a_permission_with_the_wrong_app_prefix_grants_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", lambda: _async([]))
    resp = client.get("/api/v1/dataconnect/connector-templates", headers=_perm_header("ucs:templates:view", "templates:view"))
    assert resp.status_code == 403


async def _async(value: Any) -> Any:
    return value
