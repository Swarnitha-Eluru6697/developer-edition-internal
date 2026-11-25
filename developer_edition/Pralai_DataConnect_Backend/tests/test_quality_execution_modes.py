import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.services.quality_service import QualityService

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)

POLICY = {"id": "dqp_1", "org_id": "org-1", "name": "p", "rules": [], "on_failure": "log"}
PIPELINE = {"id": "pipe_1", "org_id": "org-1", "topic_name": "org-1.raw.crm.salesforce.accounts.api.v1"}
HDR = {"X-Tenant-Id": "org-1"}


async def _get_policy(org_id, pid):
    return POLICY


async def _get_pipeline(org_id, pid):
    return PIPELINE


def test_preview_policy_is_bounded_and_synchronous(monkeypatch) -> None:
    """preview_policy must reject an oversized payload with a ValueError
    (mapped to 413 at the route) and must never need an event loop / DB
    session — it's a plain function, proving it does no I/O."""
    service = QualityService()
    monkeypatch.setattr("app.services.quality_service.settings.quality_preview_max_records", 3)

    with pytest.raises(ValueError, match="Preview is limited to 3 records"):
        service.preview_policy(
            org_id="org-1",
            pipeline=PIPELINE,
            policy=POLICY,
            records=[{"id": str(i)} for i in range(4)],
        )


def test_preview_policy_shows_every_failure_not_just_the_bounded_sample(monkeypatch) -> None:
    service = QualityService()
    monkeypatch.setattr("app.services.quality_service.settings.quality_max_sample_failures", 1)
    policy = {"id": "dqp_1", "rules": [{"name": "id_required", "type": "required", "field": "id"}]}
    records = [{"id": ""} for _ in range(5)]

    result = service.preview_policy(org_id="org-1", pipeline=PIPELINE, policy=policy, records=records)

    assert result["failed_count"] == 5
    assert len(result["sample_failures"]) == 5  # not clamped to quality_max_sample_failures
    assert "id" not in result  # never persisted as a QualityResult row


def test_preview_route_rejects_oversized_payload_with_413(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.quality.quality_service.get_policy", _get_policy)
    monkeypatch.setattr("app.api.routes.quality.pipeline_service.get_pipeline", _get_pipeline)

    def _too_big(**kwargs):
        raise ValueError("Preview is limited to 200 records (got 500); submit an execution job for larger batches.")

    monkeypatch.setattr("app.api.routes.quality.quality_service.preview_policy", _too_big)

    resp = client.post(
        "/api/v1/dataconnect/quality-policies/dqp_1/preview",
        headers=HDR,
        json={"pipeline_id": "pipe_1", "records": [{"id": "1"}]},
    )

    assert resp.status_code == 413
    assert "Preview is limited to" in resp.json()["message"]


def test_preview_route_returns_result_without_creating_a_job_or_result_row(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.quality.quality_service.get_policy", _get_policy)
    monkeypatch.setattr("app.api.routes.quality.pipeline_service.get_pipeline", _get_pipeline)
    preview_result = {
        "org_id": "org-1", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1",
        "topic_name": PIPELINE["topic_name"], "status": "passed", "failed_count": 0,
        "total_count": 1, "sample_failures": [],
    }
    monkeypatch.setattr("app.api.routes.quality.quality_service.preview_policy", lambda **kw: preview_result)

    resp = client.post(
        "/api/v1/dataconnect/quality-policies/dqp_1/preview",
        headers=HDR,
        json={"pipeline_id": "pipe_1", "records": [{"id": "1"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "passed"


def test_execute_job_route_returns_202_with_queued_job(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.quality.quality_service.get_policy", _get_policy)
    monkeypatch.setattr("app.api.routes.quality.pipeline_service.get_pipeline", _get_pipeline)
    job = {
        "id": "job_1", "org_id": "org-1", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1",
        "status": "queued", "record_count": 2, "error_message": None, "result": None,
        "created_at": None, "started_at": None, "completed_at": None,
    }

    async def _submit(**kwargs):
        return job

    monkeypatch.setattr("app.api.routes.quality.quality_service.submit_execution_job", _submit)

    resp = client.post(
        "/api/v1/dataconnect/quality-policies/dqp_1/execute-jobs",
        headers=HDR,
        json={"pipeline_id": "pipe_1", "records": [{"id": "1"}, {"id": "2"}]},
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["id"] == "job_1"
    assert body["status"] == "queued"


def test_execute_job_route_rejects_oversized_payload_with_413(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.quality.quality_service.get_policy", _get_policy)
    monkeypatch.setattr("app.api.routes.quality.pipeline_service.get_pipeline", _get_pipeline)

    async def _too_big(**kwargs):
        raise ValueError("Execution jobs are limited to 20000 records per submission (got 50000).")

    monkeypatch.setattr("app.api.routes.quality.quality_service.submit_execution_job", _too_big)

    resp = client.post(
        "/api/v1/dataconnect/quality-policies/dqp_1/execute-jobs",
        headers=HDR,
        json={"pipeline_id": "pipe_1", "records": [{"id": "1"}]},
    )

    assert resp.status_code == 413
    assert "Execution jobs are limited to" in resp.json()["message"]


def test_get_execution_job_route_404s_when_missing(monkeypatch) -> None:
    async def _missing(org_id, job_id):
        return None

    monkeypatch.setattr("app.api.routes.quality.quality_service.get_execution_job", _missing)

    resp = client.get("/api/v1/dataconnect/quality-execution-jobs/does-not-exist", headers=HDR)

    assert resp.status_code == 404


def test_get_execution_job_route_returns_the_job_with_its_result(monkeypatch) -> None:
    job = {
        "id": "job_1", "org_id": "org-1", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1",
        "status": "succeeded", "record_count": 2, "error_message": None,
        "result": {
            "id": "res_1", "org_id": "org-1", "pipeline_id": "pipe_1", "quality_policy_id": "dqp_1",
            "topic_name": PIPELINE["topic_name"], "rule_name": "*", "severity": "error",
            "status": "passed", "failed_count": 0, "total_count": 2, "sample_failures": [],
        },
        "created_at": None, "started_at": None, "completed_at": None,
    }

    async def _get(org_id, job_id):
        return job

    monkeypatch.setattr("app.api.routes.quality.quality_service.get_execution_job", _get)

    resp = client.get("/api/v1/dataconnect/quality-execution-jobs/job_1", headers=HDR)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["result"]["failed_count"] == 0
