"""
GAP-13: QualityService.submit_execution_job persists a job's `records`
batch durably (app/models/quality.py's QualityExecutionJobRecords) in the
same transaction as the QualityExecutionJob row itself, instead of only
ever handing it to the in-process asyncio task running the job.
"""

from datetime import UTC, datetime

import pytest
from conftest import patch_job_execution

from app.core.config import settings
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quality_service import quality_service


def _wire_worker(monkeypatch, fake_postgres) -> None:
    """Gives a worker what it re-fetches by id (pipeline, policy, producer) — nothing live."""
    fake_postgres.create("pipelines", {"id": "pipe-1", "org_id": "org-1", "topic_name": "t"})
    fake_postgres.create("quality_policies", {"id": "pol-1", "org_id": "org-1"})

    async def _producer():
        return InMemoryKafkaProducer()

    monkeypatch.setattr("app.services.kafka_runtime.get_kafka_producer", _producer)


@pytest.mark.asyncio
async def test_submit_execution_job_persists_records_alongside_the_job_row(monkeypatch, fake_postgres) -> None:
    import app.services.quality_job_runner as quality_job_runner

    _wire_worker(monkeypatch, fake_postgres)

    async def _ok_execute_policy(**kwargs):
        return {"id": "result-1", "failed_count": 0}

    patch_job_execution(monkeypatch, _ok_execute_policy)

    records = [{"id": str(i)} for i in range(50)]
    job = await quality_service.submit_execution_job(
        org_id="org-1", pipeline={"id": "pipe-1", "topic_name": "t"}, policy={"id": "pol-1"},
        records=records,
    )

    # Persisted immediately, before the background task even gets a chance
    # to run — this is what actually survives a restart (GAP-13), not
    # anything in the asyncio task itself.
    persisted = await quality_job_runner._load_records(job["id"])
    assert persisted == records

    await quality_job_runner.drain_once()  # let a worker claim it and finish
    finished = fake_postgres.get("quality_execution_jobs", job["id"])
    assert finished["status"] == "succeeded"
    # And cleaned up immediately once the job completes — never retained
    # past the point it's actually needed.
    assert await quality_job_runner._load_records(job["id"]) is None


@pytest.mark.asyncio
async def test_submit_execution_job_sets_an_expiry_using_the_configured_ttl(monkeypatch, fake_postgres) -> None:

    monkeypatch.setattr(settings, "quality_job_records_ttl_seconds", 3600.0)

    before = datetime.now(UTC)
    job = await quality_service.submit_execution_job(
        org_id="org-1", pipeline={"id": "pipe-1", "topic_name": "t"}, policy={"id": "pol-1"},
        records=[{"a": 1}],
    )
    after = datetime.now(UTC)

    records_row = next(
        row for row in fake_postgres.quality_execution_job_records.values() if row.job_id == job["id"]
    )
    assert before.timestamp() + 3599 < records_row.expires_at.timestamp() < after.timestamp() + 3601


@pytest.mark.asyncio
async def test_submit_execution_job_rejects_a_batch_over_the_configured_limit(fake_postgres) -> None:
    with pytest.raises(ValueError, match="limited to"):
        await quality_service.submit_execution_job(
            org_id="org-1", pipeline={"id": "pipe-1", "topic_name": "t"}, policy={"id": "pol-1"},
            records=[{"a": i} for i in range(settings.quality_job_max_records + 1)],
        )
