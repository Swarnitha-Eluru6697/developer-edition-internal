"""
BUG-06: a quality job whose staged input is gone cannot be fixed by retrying — running it again cannot recreate the records. That
failure is QUALITY_JOB_INPUT_MISSING, non-retryable, and the job offers `resubmit`. And the retention sweep must never be what
deletes the input of a queued or running job in the first place.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS, patch_job_execution
from fastapi.testclient import TestClient
from test_quality_job_chunks import (  # noqa: F401 - `wired` is used via usefixtures
    ORG,
    _submit,
    wired,
)

import app.services.quality_job_runner as runner
from app.core.config import settings
from app.core.errors import QualityJobInputMissingError
from app.main import app
from app.services.quality_service import JobStateConflict, quality_service

pytestmark = pytest.mark.usefixtures("wired")

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": ORG}
PAST = datetime.now(UTC) - timedelta(days=3)


def _jobs(fake_postgres):
    return fake_postgres.quality_execution_jobs


def _chunks(fake_postgres, job_id: str):
    return [r for r in fake_postgres.quality_execution_job_records.values() if r.job_id == job_id]


def _expire(fake_postgres, job_id: str) -> None:
    for row in _chunks(fake_postgres, job_id):
        row.expires_at = PAST


def _drop_input(fake_postgres, job_id: str, only_index: int | None = None) -> None:
    for row in _chunks(fake_postgres, job_id):
        if only_index is None or row.chunk_index == only_index:
            fake_postgres.quality_execution_job_records.pop(row.id)


# ── cleanup never deletes the input of a queued or running job ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_the_sweep_never_deletes_the_input_of_a_queued_or_running_job(fake_postgres, status) -> None:
    job = await _submit([{"stock": 50}])
    _jobs(fake_postgres)[job["id"]].status = status
    _expire(fake_postgres, job["id"])                                # long past its TTL

    deleted = await runner.cleanup_expired_records()

    assert deleted == 0
    assert len(_chunks(fake_postgres, job["id"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "stale"])
async def test_the_sweep_still_removes_expired_input_of_a_finished_job(fake_postgres, status) -> None:
    job = await _submit([{"stock": 50}])
    _jobs(fake_postgres)[job["id"]].status = status
    _expire(fake_postgres, job["id"])

    assert await runner.cleanup_expired_records() == 1
    assert _chunks(fake_postgres, job["id"]) == []


@pytest.mark.asyncio
async def test_a_job_that_waited_in_the_queue_past_the_ttl_still_runs_to_success(fake_postgres, monkeypatch) -> None:
    """The failure this prevents: queued > TTL, the sweep deleted its input, the worker then found nothing."""
    job = await _submit([{"stock": 50}])
    _expire(fake_postgres, job["id"])

    await runner.cleanup_expired_records()
    assert len(_chunks(fake_postgres, job["id"])) == 1               # still there

    patch_job_execution(monkeypatch)
    await runner.drain_once()

    assert _jobs(fake_postgres)[job["id"]].status == "succeeded"


@pytest.mark.asyncio
async def test_a_job_that_fails_after_a_long_run_keeps_its_input_for_a_full_ttl_from_the_failure(fake_postgres, monkeypatch) -> None:
    job = await _submit([{"stock": 50}])
    _expire(fake_postgres, job["id"])                                # it has been running/queued far longer than the TTL

    async def _boom(**kwargs):
        raise RuntimeError("transient")

    patch_job_execution(monkeypatch, _boom)
    await runner.drain_once()

    assert _jobs(fake_postgres)[job["id"]].status == "failed"
    (chunk,) = _chunks(fake_postgres, job["id"])
    assert chunk.expires_at > datetime.now(UTC) + timedelta(seconds=settings.quality_job_records_ttl_seconds - 60)
    assert await runner.cleanup_expired_records() == 0               # so it is not swept the moment it fails


# ── missing input: non-retryable, offers resubmit ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_worker_that_finds_the_input_gone_fails_the_job_once_and_not_retryably(fake_postgres, monkeypatch) -> None:
    job = await _submit([{"stock": 50}])
    _drop_input(fake_postgres, job["id"])
    patch_job_execution(monkeypatch)

    await runner.drain_once()
    await runner.drain_once()                                        # nothing more is claimed: it is not retried

    row = _jobs(fake_postgres)[job["id"]]
    assert row.status == "failed"
    assert row.error_code == "QUALITY_JOB_INPUT_MISSING"
    assert row.retryable is False
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_such_a_job_offers_only_resubmit(fake_postgres, monkeypatch) -> None:
    job = await _submit([{"stock": 50}])
    _drop_input(fake_postgres, job["id"])
    patch_job_execution(monkeypatch)
    await runner.drain_once()

    fetched = await quality_service.get_execution_job(ORG, job["id"])

    assert fetched["available_actions"] == ["resubmit"]
    assert fetched["retryable"] is False


@pytest.mark.asyncio
async def test_resuming_a_job_whose_input_is_gone_is_refused_with_the_typed_error_and_marks_it_non_retryable(fake_postgres) -> None:
    job = await _submit([{"stock": 50}])
    row = _jobs(fake_postgres)[job["id"]]
    row.status, row.error_code, row.retryable = "failed", "OPERATION_FAILED", True     # an ordinary failure: looked resumable
    assert (await quality_service.get_execution_job(ORG, job["id"]))["available_actions"] == ["resume", "resubmit"]
    _drop_input(fake_postgres, job["id"])                                              # ... until its input expired

    with pytest.raises(QualityJobInputMissingError) as exc_info:
        await quality_service.resume_execution_job(ORG, job["id"])

    assert exc_info.value.code == "QUALITY_JOB_INPUT_MISSING"
    assert exc_info.value.retryable is False
    assert exc_info.value.details["action"] == "resubmit"
    assert row.status == "failed"                                                      # NOT put back in the queue
    assert (row.error_code, row.retryable) == ("QUALITY_JOB_INPUT_MISSING", False)
    assert (await quality_service.get_execution_job(ORG, job["id"]))["available_actions"] == ["resubmit"]


@pytest.mark.asyncio
async def test_resume_checks_only_the_chunks_the_job_still_needs(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 1)
    job = await _submit([{"stock": 50}, {"stock": 60}, {"stock": 70}])             # chunks 0, 1, 2
    row = _jobs(fake_postgres)[job["id"]]
    row.status, row.progress = "failed", {"chunks_completed": 1}
    _drop_input(fake_postgres, job["id"], only_index=0)                            # a chunk it already finished: not needed

    resumed = await quality_service.resume_execution_job(ORG, job["id"])
    assert resumed["status"] == "queued"

    row.status = "failed"
    _drop_input(fake_postgres, job["id"], only_index=2)                            # a chunk it still has to process
    with pytest.raises(QualityJobInputMissingError):
        await quality_service.resume_execution_job(ORG, job["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        ("queued", None, []),
        ("running", None, []),
        ("succeeded", None, []),
        ("failed", "OPERATION_FAILED", ["resume", "resubmit"]),
        ("stale", "ATTEMPTS_EXHAUSTED", ["resume", "resubmit"]),
        ("failed", "QUALITY_JOB_INPUT_MISSING", ["resubmit"]),
        ("failed", "JOB_REFERENCE_MISSING", ["resubmit"]),
    ],
)
async def test_available_actions_follow_the_job_state(fake_postgres, status, error_code, expected) -> None:
    job = await _submit([{"stock": 50}])
    row = _jobs(fake_postgres)[job["id"]]
    row.status, row.error_code = status, error_code

    assert (await quality_service.get_execution_job(ORG, job["id"]))["available_actions"] == expected


# ── resubmit ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resubmitting_creates_a_new_job_for_the_same_pipeline_and_policy_linked_to_the_original(fake_postgres) -> None:
    job = await _submit([{"stock": 50}])
    row = _jobs(fake_postgres)[job["id"]]
    row.status, row.error_code, row.retryable = "failed", "QUALITY_JOB_INPUT_MISSING", False
    _drop_input(fake_postgres, job["id"])

    new = await quality_service.resubmit_execution_job(ORG, job["id"], [{"stock": 1}, {"stock": 2}])

    assert new["id"] != job["id"]
    assert (new["pipeline_id"], new["quality_policy_id"]) == (row.pipeline_id, row.quality_policy_id)
    assert new["status"] == "queued"
    assert new["resubmitted_from"] == job["id"]
    assert new["record_count"] == 2
    assert len(_chunks(fake_postgres, new["id"])) == 1                  # the new input is staged
    assert row.status == "failed"                                       # the original keeps its own failure
    assert row.error_code == "QUALITY_JOB_INPUT_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
async def test_only_a_failed_or_stale_job_can_be_resubmitted(fake_postgres, status) -> None:
    job = await _submit([{"stock": 50}])
    _jobs(fake_postgres)[job["id"]].status = status

    with pytest.raises(JobStateConflict, match="Only a failed or stale job"):
        await quality_service.resubmit_execution_job(ORG, job["id"], [{"stock": 1}])
    assert len(_chunks(fake_postgres, job["id"])) == 1                  # and nothing of the active job was touched


@pytest.mark.asyncio
async def test_resubmitting_an_unknown_job_is_none(fake_postgres) -> None:
    assert await quality_service.resubmit_execution_job(ORG, "nope", [{"a": 1}]) is None


# ── the API ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_api_reports_410_with_a_typed_non_retryable_error_and_the_resubmit_path(fake_postgres) -> None:
    job = await _submit([{"stock": 50}])
    _jobs(fake_postgres)[job["id"]].status = "failed"
    _drop_input(fake_postgres, job["id"])

    response = client.post(f"/api/v1/dataconnect/quality-execution-jobs/{job['id']}/resume", headers=HDR)

    assert response.status_code == 410
    body = response.json()
    assert body["code"] == "QUALITY_JOB_INPUT_MISSING"
    assert body["retryable"] is False
    assert body["details"]["action"] == "resubmit"
    assert body["details"]["resubmit_path"] == f"/quality-execution-jobs/{job['id']}/resubmit"


@pytest.mark.asyncio
async def test_the_api_resubmit_action_queues_a_new_job(fake_postgres) -> None:
    job = await _submit([{"stock": 50}])
    _jobs(fake_postgres)[job["id"]].status = "failed"
    _drop_input(fake_postgres, job["id"])

    response = client.post(
        f"/api/v1/dataconnect/quality-execution-jobs/{job['id']}/resubmit", headers=HDR, json={"records": [{"stock": 5}]},
    )

    assert response.status_code == 202
    assert response.json()["resubmitted_from"] == job["id"]
    assert response.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_the_api_refuses_to_resubmit_an_active_job_and_a_missing_one(fake_postgres) -> None:
    job = await _submit([{"stock": 50}])                                # queued

    active = client.post(f"/api/v1/dataconnect/quality-execution-jobs/{job['id']}/resubmit", headers=HDR, json={"records": [{"a": 1}]})
    unknown = client.post("/api/v1/dataconnect/quality-execution-jobs/nope/resubmit", headers=HDR, json={"records": [{"a": 1}]})

    assert active.status_code == 409
    assert unknown.status_code == 404
