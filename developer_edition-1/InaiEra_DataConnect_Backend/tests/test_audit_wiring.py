"""
Verifies every mutating route named in the audit ticket actually calls
record_audit() with the right org_id/actor/action/resource_type/resource_id —
by capturing the call (monkeypatching each route module's own `record_audit`
name) rather than hitting a real Postgres, consistent with how every other
Postgres-backed fix in this session is tested.
"""

from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": "org-1", "X-User-Name": "alice@example.com"}


def _capture():
    calls = []

    async def _record_audit(**kwargs):
        calls.append(kwargs)

    return calls, _record_audit


# ── connections ──────────────────────────────────────────────────────────────


def test_connection_create_update_delete_test_are_all_audited(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.connections.record_audit", fake)

    sample = {"id": "c1", "org_id": "org-1", "name": "conn", "source_type": "postgres", "connection_config": {}}

    async def _create(org_id, p):
        return sample

    async def _get_or_404(org_id, cid):
        return sample

    monkeypatch.setattr("app.api.routes.connections.connection_service.create_connection", _create)
    monkeypatch.setattr("app.api.routes.connections.connection_service.get_or_404", _get_or_404)

    async def _live_test(*args, **kwargs):
        return {"status": "ok", "message": "ok", "details": {}}

    async def _probe_and_record(*args, **kwargs):
        return {"status": "ok", "message": "ok", "details": {}}

    async def _update(*args, **kwargs):
        return sample

    async def _delete(*args, **kwargs):
        return None

    monkeypatch.setattr("app.api.routes.connections.connection_service.probe_and_record", _probe_and_record)
    monkeypatch.setattr("app.api.routes.connections.connection_service.update_connection", _update)
    monkeypatch.setattr("app.api.routes.connections.connection_service.test_connection", _live_test)
    monkeypatch.setattr("app.api.routes.connections.connection_service.delete_connection", _delete)

    client.post(
        "/api/v1/dataconnect/connections", headers=HDR,
        json={"name": "conn", "source_type": "postgres", "connection_config": {}},
    )
    client.put(
        "/api/v1/dataconnect/connections/c1", headers=HDR,
        json={"name": "conn", "source_type": "postgres", "connection_config": {}},
    )
    client.delete("/api/v1/dataconnect/connections/c1", headers=HDR)
    client.post("/api/v1/dataconnect/connections/c1/test", headers=HDR)

    actions = {(c["action"], c["resource_type"]) for c in calls}
    assert ("created", "connection") in actions
    assert ("updated", "connection") in actions
    assert ("deleted", "connection") in actions
    assert ("tested", "connection") in actions
    for c in calls:
        assert c["org_id"] == "org-1"
        assert c["actor"] == "alice@example.com"


def test_connection_payload_test_records_resource_id_only_when_known(monkeypatch) -> None:
    """POST /connections/test-connection probes whatever's currently typed in
    the create/edit form, not a saved row — so there's genuinely no id for a
    brand-new connection. But the edit form calls this same endpoint too
    (testing in-progress, possibly-unsaved edits rather than the last-saved
    row), and passes the connection's own id along purely so the audit trail
    can still say which connection was being edited, instead of always
    showing a blank resource_id."""
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.connections.record_audit", fake)

    async def _live_test(*args, **kwargs):
        return {"status": "ok", "message": "ok", "details": {}}

    monkeypatch.setattr("app.api.routes.connections.connection_service.live_test_connection", _live_test)

    body = {"name": "conn", "source_type": "postgres", "connection_config": {}}
    client.post("/api/v1/dataconnect/connections/test-connection", headers=HDR, json=body)
    client.post(
        "/api/v1/dataconnect/connections/test-connection?connection_id=c1", headers=HDR, json=body,
    )

    assert len(calls) == 2
    assert calls[0]["resource_id"] is None
    assert calls[1]["resource_id"] == "c1"


def test_connection_audit_actor_is_the_gateway_user_not_a_client_header(monkeypatch) -> None:
    """The actor is the gateway principal's username: a client-sent X-Actor is
    ignored, so nobody can write someone else's name into the audit trail."""
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.connections.record_audit", fake)
    sample = {"id": "c1", "org_id": "org-1", "name": "conn", "source_type": "postgres", "connection_config": {}}

    async def _create(org_id, p):
        return sample

    async def _probe_and_record(*args, **kwargs):
        return {"status": "ok", "message": "ok", "details": {}}

    async def _get_or_404(org_id, cid):
        return sample

    monkeypatch.setattr("app.api.routes.connections.connection_service.create_connection", _create)
    monkeypatch.setattr("app.api.routes.connections.connection_service.probe_and_record", _probe_and_record)
    monkeypatch.setattr("app.api.routes.connections.connection_service.get_or_404", _get_or_404)

    client.post(
        "/api/v1/dataconnect/connections", headers={"X-Tenant-Id": "org-1", "X-Actor": "mallory"},
        json={"name": "conn", "source_type": "postgres", "connection_config": {}},
    )

    assert calls[0]["actor"] == "test-harness"


# ── pipelines ────────────────────────────────────────────────────────────────


def test_pipeline_lifecycle_operations_are_all_audited(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", fake)

    pipeline = {
        "id": "p1", "org_id": "org-1", "name": "n", "template_id": "t1", "connection_profile_id": "c1",
        "zone": "raw", "domain": "sales", "source_system": "app", "entity": "orders", "mode": "cdc",
        "version": "v1", "status": "draft", "topic_name": "t", "catalog_qualified_name": "t",
        "connector_name": "cn", "generated_config": {"a": 1}, "direction": "SOURCE",
    }
    async def _create_pipeline(org_id, p):
        return pipeline

    async def _get_or_404(org_id, pid):
        return pipeline

    async def _update_pipeline(*a, **k):
        return pipeline

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.create_pipeline", _create_pipeline)
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", _get_or_404)
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.update_pipeline", _update_pipeline)

    async def _ok(*args, **kwargs):
        return {
            "pipeline_id": "p1", "status": "deployed", "runtime": {},
            "connector_name": "cn", "topic_name": "t",
        }

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.deploy", _ok)
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.pause", _ok)
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.resume", _ok)
    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.restart", _ok)

    # FE-BUG-25: _submit_lifecycle_operation now atomically claims the
    # pipeline before creating the Operation — mocked at that boundary too.
    async def _claim(*args, **kwargs):
        return pipeline

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.claim_lifecycle_transition", _claim)

    # deploy/pause/resume/restart now submit through operation_runner (queued
    # immediately, claimed and run by the operation worker — see
    # app/services/operation_runner.py) rather than awaiting pipeline_service's
    # call directly, so this is mocked at the same service boundary the four
    # pipeline_service methods above are, consistent with how
    # test_quality_execution_modes.py mocks quality_service.submit_execution_job
    # rather than exercising the real claim/lease + Postgres path.
    async def _submit(*, org_id, operation_type, resource_type, resource_id, operation_id=None):
        return {
            "id": "op1", "org_id": org_id, "operation_type": operation_type,
            "resource_type": resource_type, "resource_id": resource_id,
            "status": "queued", "result": None, "error_message": None,
            "created_at": None, "started_at": None, "completed_at": None,
        }

    monkeypatch.setattr("app.api.routes.pipelines.operation_runner.create_and_run", _submit)

    async def _delete(*args, **kwargs):
        return None

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.delete_pipeline", _delete)

    client.post(
        "/api/v1/dataconnect/pipelines", headers=HDR,
        json={"name": "n", "template_id": "t1", "connection_profile_id": "c1", "zone": "raw",
              "domain": "sales", "source_system": "app", "entity": "orders", "mode": "cdc", "version": "v1"},
    )
    client.patch("/api/v1/dataconnect/pipelines/p1", headers=HDR, json={"description": "x"})
    client.post("/api/v1/dataconnect/pipelines/p1/deploy", headers=HDR)
    client.post("/api/v1/dataconnect/pipelines/p1/pause", headers=HDR)
    client.post("/api/v1/dataconnect/pipelines/p1/resume", headers=HDR)
    client.post("/api/v1/dataconnect/pipelines/p1/restart", headers=HDR)
    client.delete("/api/v1/dataconnect/pipelines/p1", headers=HDR)
    client.get("/api/v1/dataconnect/pipelines/p1/generated-config", headers=HDR)

    # deploy/pause/resume/restart are async now (see the operation_runner mock
    # above) — audited as "<action>_requested" at submission time, the same
    # convention delete_pipeline already uses for "delete_requested": there's
    # no synchronous outcome to audit, only the request.
    actions = {(c["action"], c["resource_type"]) for c in calls}
    assert actions == {
        ("created", "pipeline"), ("updated", "pipeline"), ("deploy_requested", "pipeline"),
        ("pause_requested", "pipeline"), ("resume_requested", "pipeline"), ("restart_requested", "pipeline"),
        ("delete_requested", "pipeline"), ("accessed", "pipeline_generated_config"),
    }
    for c in calls:
        assert c["org_id"] == "org-1"
        assert c["resource_id"] == "p1"


def test_generated_config_access_is_a_read_not_a_diffed_mutation(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.pipelines.record_audit", fake)
    pipeline = {"id": "p1", "org_id": "org-1", "generated_config": {"password": "shhh"}}

    async def _get_or_404(org_id, pid):
        return pipeline

    monkeypatch.setattr("app.api.routes.pipelines.pipeline_service.get_or_404", _get_or_404)

    client.get("/api/v1/dataconnect/pipelines/p1/generated-config", headers=HDR)

    assert len(calls) == 1
    assert calls[0]["action"] == "accessed"
    assert calls[0].get("before_state") is None
    assert calls[0].get("after_state") is None


# ── transform policies ───────────────────────────────────────────────────────


def test_transform_policy_create_update_delete_are_audited(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.transforms.record_audit", fake)

    policy = {"id": "tp1", "org_id": "org-1", "name": "n", "transforms": []}

    async def _create_policy(org_id, data):
        return policy

    async def _update_policy(org_id, pid, data):
        return policy

    async def _get_policy(org_id, pid):
        return policy

    async def _delete_policy(org_id, pid):
        return None

    monkeypatch.setattr("app.api.routes.transforms.transform_service.create_policy", _create_policy)
    monkeypatch.setattr("app.api.routes.transforms.transform_service.update_policy", _update_policy)
    monkeypatch.setattr("app.api.routes.transforms.transform_service.get_policy", _get_policy)
    monkeypatch.setattr("app.api.routes.transforms.transform_service.delete_policy", _delete_policy)

    client.post("/api/v1/dataconnect/transform-policies", headers=HDR, json={"name": "n"})
    client.put("/api/v1/dataconnect/transform-policies/tp1", headers=HDR, json={"name": "n"})
    client.delete("/api/v1/dataconnect/transform-policies/tp1", headers=HDR)

    actions = {(c["action"], c["resource_type"]) for c in calls}
    assert actions == {
        ("created", "transform_policy"), ("updated", "transform_policy"), ("deleted", "transform_policy"),
    }


def test_transform_policy_delete_requires_a_valid_tenant(monkeypatch) -> None:
    """Previously this route deleted with no org/ownership check at all —
    now an invalid tenant in the gateway identity 400s rather than silently deleting."""
    resp = client.delete("/api/v1/dataconnect/transform-policies/tp1", headers={"X-Tenant-Id": "x"})

    assert resp.status_code == 400


def test_transform_policy_delete_404s_for_another_orgs_policy(monkeypatch) -> None:
    async def _get_policy(org_id, pid):
        return {"id": "tp1", "org_id": "org_other", "name": "n", "transforms": []}

    monkeypatch.setattr("app.api.routes.transforms.transform_service.get_policy", _get_policy)

    resp = client.delete("/api/v1/dataconnect/transform-policies/tp1", headers=HDR)

    assert resp.status_code == 404


# ── schema versions & drift approvals ────────────────────────────────────────


def test_schema_create_and_drift_decisions_are_audited(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.schemas.record_audit", fake)

    created = {"id": "s1", "org_id": "org-1", "domain": "d", "source_system": "s", "entity": "e", "version": "v1"}

    async def _create_schema(org_id, p):
        return created

    monkeypatch.setattr("app.api.routes.schemas.schema_service.create_schema", _create_schema)

    def _approval(approval_id: str, status_: str) -> dict:
        return {
            "id": approval_id, "org_id": "org-1", "schema_version_id": "s1",
            "pipeline_id": None, "run_id": None, "detected_fingerprint": None,
            "current_fingerprint": None, "detected_at": None,
            "observed_schema": {"fields": []}, "drift_events": [],
            "status": status_, "decision": status_, "resulting_schema_version_id": None,
            "requested_by": None, "reviewed_by": "bob", "review_comment": None, "reviewed_at": None,
        }

    async def _approve(*args, **kwargs):
        return _approval("approval_1", "approved")

    async def _reject(*args, **kwargs):
        return _approval("approval_2", "rejected")

    monkeypatch.setattr("app.api.routes.schemas.schema_service.approve_drift", _approve)
    monkeypatch.setattr("app.api.routes.schemas.schema_service.reject_drift", _reject)

    client.post(
        "/api/v1/dataconnect/schemas", headers=HDR,
        json={"domain": "d", "source_system": "s", "entity": "e", "version": "v1"},
    )
    client.post(
        "/api/v1/dataconnect/schemas/drift-approvals/approval_1/approve", headers=HDR,
        json={"reviewed_by": "bob"},
    )
    client.post(
        "/api/v1/dataconnect/schemas/drift-approvals/approval_2/reject", headers=HDR,
        json={"reviewed_by": "bob"},
    )

    actions = {(c["action"], c["resource_type"]) for c in calls}
    assert ("created", "schema_version") in actions
    assert ("approved", "schema_drift_approval") in actions
    assert ("rejected", "schema_drift_approval") in actions
    # The audit actor is the platform-supplied caller identity, independent
    # of the self-reported `reviewed_by` body field.
    approved_call = next(c for c in calls if c["action"] == "approved")
    assert approved_call["actor"] == "alice@example.com"


# ── quality policies ─────────────────────────────────────────────────────────


def test_quality_policy_create_update_delete_are_audited(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.quality.record_audit", fake)

    policy = {"id": "q1", "org_id": "org-1", "name": "n", "rules": []}

    async def _create(org_id, p):
        return policy

    async def _update(*a, **k):
        return policy

    async def _delete(org_id, pid):
        return None

    async def _get(org_id, pid):
        return policy

    monkeypatch.setattr("app.api.routes.quality.quality_service.create_policy", _create)
    monkeypatch.setattr("app.api.routes.quality.quality_service.update_policy", _update)
    monkeypatch.setattr("app.api.routes.quality.quality_service.delete_policy", _delete)
    monkeypatch.setattr("app.api.routes.quality.quality_service.get_policy", _get)

    client.post("/api/v1/dataconnect/quality-policies", headers=HDR, json={"name": "n"})
    client.put("/api/v1/dataconnect/quality-policies/q1", headers=HDR, json={"name": "n"})
    client.delete("/api/v1/dataconnect/quality-policies/q1", headers=HDR)

    actions = {(c["action"], c["resource_type"]) for c in calls}
    assert actions == {
        ("created", "quality_policy"), ("updated", "quality_policy"), ("deleted", "quality_policy"),
    }


# ── connector templates (platform-global, org_id=None) ──────────────────────


def test_connector_template_changes_are_audited_with_no_org(monkeypatch) -> None:
    calls, fake = _capture()
    monkeypatch.setattr("app.api.routes.catalog.record_audit", fake)

    template = {
        "id": "ct1", "name": "n", "connector_type": "source", "category": "database",
        "connector_class": "x.Y", "runtime_pool_type": "kafka-connect",
    }

    async def _create(payload):
        return template

    async def _update(tid, payload):
        return template

    async def _delete(tid):
        return None

    async def _get(tid):
        return template

    monkeypatch.setattr("app.api.routes.catalog.catalog_service.create_template", _create)
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.update_template", _update)
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.delete_template", _delete)
    monkeypatch.setattr("app.api.routes.catalog.catalog_service.get_template", _get)

    client.post(
        "/api/v1/dataconnect/connector-templates", headers=HDR,
        json={"name": "n", "connector_type": "source", "category": "database",
              "connector_class": "x.Y", "runtime_pool_type": "kafka-connect"},
    )
    client.put(
        "/api/v1/dataconnect/connector-templates/ct1", headers=HDR,
        json={"name": "n", "connector_type": "source", "category": "database",
              "connector_class": "x.Y", "runtime_pool_type": "kafka-connect"},
    )
    client.delete("/api/v1/dataconnect/connector-templates/ct1", headers=HDR)

    actions = {(c["action"], c["resource_type"]) for c in calls}
    assert actions == {
        ("created", "connector_template"), ("updated", "connector_template"), ("deleted", "connector_template"),
    }
    for c in calls:
        assert c["org_id"] is None  # global resource, never fabricated org context
