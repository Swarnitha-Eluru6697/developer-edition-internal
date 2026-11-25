from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.services.schema_service import SchemaService

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": "org-1"}


def test_detect_drift_marks_approval_required_for_required_field() -> None:
    service = SchemaService()

    result = service.detect_drift(
        {
            "schema_definition": {
                "fields": [{"name": "id", "type": "string", "nullable": False, "primary_key": True}]
            }
        },
        [{"id": "1", "new_required": "x"}],
    )

    assert result["approval_required"] is True


def test_detect_drift_includes_fingerprints_and_detected_at() -> None:
    service = SchemaService()
    # primary_key=False to match what inference always produces (it never
    # infers a primary key) — so a same-shape sample fingerprints identically.
    approved_fields = [{"name": "id", "type": "string", "nullable": False, "primary_key": False}]

    result = service.detect_drift(
        {"schema_definition": {"fields": approved_fields}},
        [{"id": "1"}],
    )

    assert result["detected_fingerprint"]
    assert result["current_fingerprint"]
    assert result["detected_at"] is not None
    # Compatible sample (same shape as approved) — fingerprints match.
    assert result["detected_fingerprint"] == result["current_fingerprint"]


def test_detect_drift_route_passes_pipeline_and_run_id_into_the_approval(monkeypatch) -> None:
    schema = {
        "id": "schema_1", "org_id": "org-1", "domain": "crm", "source_system": "sf",
        "entity": "accounts", "version": "v1", "schema_format": "json_schema",
        "schema_definition": {"fields": [{"name": "id", "type": "string", "nullable": False, "primary_key": True}]},
        "compatibility": "BACKWARD", "status": "active",
    }
    async def _get_schema(org_id, sid):
        return schema

    monkeypatch.setattr("app.api.routes.schemas.schema_service.get_schema", _get_schema)

    captured = {}

    async def _create_drift_approval(org_id, payload):
        captured.update(payload)
        return {"id": "approval_1", **payload}

    monkeypatch.setattr("app.api.routes.schemas.schema_service.create_drift_approval", _create_drift_approval)

    resp = client.post(
        "/api/v1/dataconnect/schemas/schema_1/detect-drift",
        headers=HDR,
        json={
            "records": [{"id": "1", "new_required": "x"}],
            "pipeline_id": "pipe_1",
            "run_id": "run_42",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["approval_request_id"] == "approval_1"
    assert captured["pipeline_id"] == "pipe_1"
    assert captured["run_id"] == "run_42"
    assert captured["detected_fingerprint"]
    assert captured["current_fingerprint"]


def test_approve_drift_route_passes_decision_and_resulting_version(monkeypatch) -> None:
    async def _approve(org_id, approval_id, reviewer, comment, resulting_schema_version_id=None):
        return {
            "id": approval_id, "org_id": org_id, "schema_version_id": "schema_1",
            "pipeline_id": None, "run_id": None, "detected_fingerprint": "d", "current_fingerprint": "c",
            "detected_at": None, "observed_schema": {"fields": []}, "drift_events": [],
            "status": "approved", "decision": "approved",
            "resulting_schema_version_id": resulting_schema_version_id,
            "requested_by": None, "reviewed_by": reviewer, "review_comment": comment, "reviewed_at": None,
        }

    monkeypatch.setattr("app.api.routes.schemas.schema_service.approve_drift", _approve)

    async def _noop_record_audit(**kwargs) -> None:
        return None

    monkeypatch.setattr("app.api.routes.schemas.record_audit", _noop_record_audit)

    resp = client.post(
        "/api/v1/dataconnect/schemas/drift-approvals/approval_1/approve",
        headers=HDR,
        json={"reviewed_by": "architect", "comment": "Accepted", "resulting_schema_version_id": "schema_2"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decision"] == "approved"
    assert body["reviewed_by"] == "architect"
    assert body["resulting_schema_version_id"] == "schema_2"


def test_approve_drift_route_404s_when_missing(monkeypatch) -> None:
    async def _approve(*args, **kwargs):
        return None

    monkeypatch.setattr("app.api.routes.schemas.schema_service.approve_drift", _approve)

    resp = client.post(
        "/api/v1/dataconnect/schemas/drift-approvals/does-not-exist/approve",
        headers=HDR,
        json={"reviewed_by": "architect"},
    )

    assert resp.status_code == 404
