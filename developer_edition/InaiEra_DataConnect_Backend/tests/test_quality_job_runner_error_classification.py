"""
BUG-11 + GAP-13 + BUG-02: QualityExecutionJob's claim/lease runner
(app/services/quality_job_runner.py) — confirms every failure path (a raised exception
mid-execution, a job whose pipeline/policy/records are gone) persists a SafeError's fields instead
of `str(exc)[:2000]` (BUG-11), and that the job's `records` batch is cleaned up once it is terminal
(GAP-13). Lease/claim behaviour itself is in tests/test_quality_job_lease.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import patch_job_execution

import app.services.quality_job_runner as quality_job_runner
from app.services.kafka_runtime import InMemoryKafkaProducer


def _seed_records(fake_postgres, job_id: str, records: list[dict], *, expired: bool = False) -> None:
    offset = -3600 if expired else 3600
    fake_postgres.create(
        "quality_execution_job_records",
        {"job_id": job_id, "org_id": "org-1", "records": records, "expires_at": datetime.now(UTC) + timedelta(seconds=offset)},
    )


def _wire(monkeypatch, fake_postgres, *, pipeline: bool = True, policy: bool = True) -> None:
    """What a worker re-fetches by id when it claims a job: its pipeline, its policy, a producer."""
    if pipeline:
        fake_postgres.create("pipelines", {"id": "pipe-1", "org_id": "org-1", "topic_name": "t"})
    if policy:
        fake_postgres.create("quality_policies", {"id": "pol-1", "org_id": "org-1"})

    async def _producer():
        return InMemoryKafkaProducer()

    monkeypatch.setattr("app.services.kafka_runtime.get_kafka_producer", _producer)


def _queued_job(fake_postgres, **overrides) -> dict:
    return fake_postgres.create(
        "quality_execution_jobs",
        {"org_id": "org-1", "pipeline_id": "pipe-1", "quality_policy_id": "pol-1", "status": "queued", "record_count": 1, **overrides},
    )


@pytest.mark.asyncio
async def test_a_raised_exception_persists_safe_error_fields_not_raw_text(monkeypatch, fake_postgres) -> None:
    _wire(monkeypatch, fake_postgres)
    job = _queued_job(fake_postgres, record_count=2)
    _seed_records(fake_postgres, job["id"], [{"a": 1}])

    async def _leaky_execute_policy(**kwargs):
        raise ConnectionError("could not connect to postgres://dataconnect:hunter2@db.internal:5432/erp")

    patch_job_execution(monkeypatch, _leaky_execute_policy)

    await quality_job_runner.drain_once()

    finished = fake_postgres.get("quality_execution_jobs", job["id"])
    assert finished["status"] == "failed"
    assert finished["error_code"] == "SOURCE_CONNECTION_REFUSED"
    assert finished["retryable"] is True
    assert finished["correlation_id"] is not None
    assert finished["error_message"] == "Could not connect to the source system."
    assert "hunter2" not in finished["error_message"]
    assert "db.internal" not in finished["error_message"]
    # GAP-13: a FAILED job keeps its staged input until it expires, so it can be resumed (resume_execution_job) rather
    # than resubmitted; only a job that SUCCEEDS has it deleted.
    assert await quality_job_runner._load_records(job["id"]) is not None


@pytest.mark.asyncio
async def test_a_failed_job_is_not_retried_by_the_worker(monkeypatch, fake_postgres) -> None:
    """Only a job whose worker VANISHED is reclaimed; one that raised is terminal — the caller resubmits."""
    _wire(monkeypatch, fake_postgres)
    job = _queued_job(fake_postgres)
    _seed_records(fake_postgres, job["id"], [{"a": 1}])
    calls = 0

    async def _boom(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    patch_job_execution(monkeypatch, _boom)

    assert await quality_job_runner.drain_once() == 1
    assert await quality_job_runner.drain_once() == 0
    assert calls == 1


@pytest.mark.asyncio
async def test_a_successful_job_leaves_error_fields_null_and_deletes_its_records(monkeypatch, fake_postgres) -> None:
    _wire(monkeypatch, fake_postgres)
    job = _queued_job(fake_postgres)
    _seed_records(fake_postgres, job["id"], [{"a": 1}])

    async def _ok_execute_policy(**kwargs):
        return {"id": "result-1", "failed_count": 0}

    patch_job_execution(monkeypatch, _ok_execute_policy)

    await quality_job_runner.drain_once()

    finished = fake_postgres.get("quality_execution_jobs", job["id"])
    assert finished["status"] == "succeeded"
    assert finished["result_id"] == "result-1"
    assert finished.get("error_code") is None
    assert finished.get("correlation_id") is None
    assert await quality_job_runner._load_records(job["id"]) is None


@pytest.mark.asyncio
async def test_run_fails_cleanly_when_records_are_unexpectedly_missing(monkeypatch, fake_postgres) -> None:
    """Shouldn't happen (records are created atomically with the job row —
    see submit_execution_job) but must never crash the worker if it does."""
    _wire(monkeypatch, fake_postgres)
    job = _queued_job(fake_postgres)
    # No records row seeded.

    await quality_job_runner.drain_once()

    finished = fake_postgres.get("quality_execution_jobs", job["id"])
    assert finished["status"] == "failed"
    assert finished["error_code"] == "QUALITY_JOB_INPUT_MISSING"
    assert finished["retryable"] is False                      # BUG-06: retrying cannot recreate records that are gone


@pytest.mark.asyncio
async def test_a_job_whose_pipeline_or_policy_is_gone_fails_and_its_records_are_cleaned_up(monkeypatch, fake_postgres) -> None:
    _wire(monkeypatch, fake_postgres, pipeline=False)
    job = _queued_job(fake_postgres, pipeline_id="does-not-exist")
    _seed_records(fake_postgres, job["id"], [{"a": 1}])

    await quality_job_runner.drain_once()

    finished = fake_postgres.get("quality_execution_jobs", job["id"])
    assert finished["status"] == "failed"
    assert finished["error_code"] == "JOB_REFERENCE_MISSING"
    assert "pipeline or quality policy" in finished["error_message"].lower()
    assert finished["retryable"] is True
    assert finished["correlation_id"] is not None
    # Nothing left to run against — the now-unusable records are cleaned up too.
    assert await quality_job_runner._load_records(job["id"]) is None


@pytest.mark.asyncio
async def test_cleanup_expired_records_deletes_only_past_their_ttl(fake_postgres) -> None:
    job_a = fake_postgres.create(
        "quality_execution_jobs",
        {"org_id": "org-1", "pipeline_id": "pipe-1", "quality_policy_id": "pol-1", "status": "succeeded"},
    )
    job_b = fake_postgres.create(
        "quality_execution_jobs",
        {"org_id": "org-1", "pipeline_id": "pipe-1", "quality_policy_id": "pol-1", "status": "succeeded"},
    )
    _seed_records(fake_postgres, job_a["id"], [{"a": 1}], expired=True)
    _seed_records(fake_postgres, job_b["id"], [{"a": 1}], expired=False)

    deleted = await quality_job_runner.cleanup_expired_records()

    assert deleted == 1
    assert await quality_job_runner._load_records(job_a["id"]) is None
    assert await quality_job_runner._load_records(job_b["id"]) is not None
