"""BUG-02: quality execution jobs run on a claim/lease worker, so a job whose process died is
reclaimed by a surviving worker — and a job a live worker is still running is never taken twice.
Mirrors tests/test_operation_runner.py's coverage of the same model for Operation."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import patch_job_execution

import app.services.quality_job_runner as quality_job_runner
from app.core.config import settings
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quality_service import quality_service


@pytest.fixture(autouse=True)
def _wired(monkeypatch, fake_postgres) -> None:
    fake_postgres.create("pipelines", {"id": "pipe-1", "org_id": "org-1", "topic_name": "t"})
    fake_postgres.create("quality_policies", {"id": "pol-1", "org_id": "org-1"})

    async def _producer():
        return InMemoryKafkaProducer()

    monkeypatch.setattr("app.services.kafka_runtime.get_kafka_producer", _producer)


def _job(fake_postgres, **overrides) -> dict:
    job = fake_postgres.create(
        "quality_execution_jobs",
        {"org_id": "org-1", "pipeline_id": "pipe-1", "quality_policy_id": "pol-1", "status": "queued", "record_count": 1, **overrides},
    )
    fake_postgres.create(
        "quality_execution_job_records",
        {"job_id": job["id"], "org_id": "org-1", "records": [{"a": 1}], "expires_at": datetime.now(UTC) + timedelta(hours=1)},
    )
    return job


def _ok(monkeypatch, calls: list | None = None) -> None:
    async def _execute(**kwargs):
        if calls is not None:
            calls.append(1)
        return {"id": "result-1", "failed_count": 0}

    patch_job_execution(monkeypatch, _execute)


@pytest.mark.asyncio
async def test_claiming_a_queued_job_marks_it_running_with_a_lease_and_an_attempt(fake_postgres) -> None:
    job = _job(fake_postgres)
    before = datetime.now(UTC)

    claimed = await quality_job_runner.claim_next_job()

    assert claimed["id"] == job["id"]
    row = fake_postgres.get("quality_execution_jobs", job["id"])
    assert row["status"] == "running"
    assert row["attempt_count"] == 1
    assert row["lease_expires_at"] > before + timedelta(seconds=settings.quality_job_lease_seconds - 5)


@pytest.mark.asyncio
async def test_a_job_can_only_be_claimed_once_while_its_lease_is_live(fake_postgres) -> None:
    _job(fake_postgres)

    first = await quality_job_runner.claim_next_job()
    second = await quality_job_runner.claim_next_job()

    assert first is not None
    assert second is None  # another worker polling right now gets nothing: the lease is live


@pytest.mark.asyncio
async def test_concurrent_claims_never_hand_the_same_job_to_two_workers(fake_postgres) -> None:
    for _ in range(3):
        _job(fake_postgres)

    results = await asyncio.gather(*(quality_job_runner.claim_next_job() for _ in range(8)))

    claimed_ids = [r["id"] for r in results if r is not None]
    assert len(claimed_ids) == 3
    assert len(set(claimed_ids)) == 3  # three jobs, three different claims, nothing doubled


@pytest.mark.asyncio
async def test_a_job_whose_worker_died_is_reclaimed_once_its_lease_lapses_and_finishes(monkeypatch, fake_postgres) -> None:
    """The BUG-02 scenario: DB says running, the process that was running it is gone."""
    calls: list = []
    _ok(monkeypatch, calls)
    job = _job(
        fake_postgres, status="running", attempt_count=1, started_at=datetime.now(UTC) - timedelta(minutes=10),
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    ran = await quality_job_runner.drain_once()

    assert ran == 1
    row = fake_postgres.get("quality_execution_jobs", job["id"])
    assert row["status"] == "succeeded"
    assert row["attempt_count"] == 2  # visible evidence it was reclaimed, not first-run
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_running_job_with_a_live_lease_is_left_alone(monkeypatch, fake_postgres) -> None:
    """The old startup 'resume everything running' would have re-run this on a second replica."""
    calls: list = []
    _ok(monkeypatch, calls)
    _job(fake_postgres, status="running", attempt_count=1, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1))

    assert await quality_job_runner.drain_once() == 0
    assert calls == []


@pytest.mark.asyncio
async def test_a_legacy_running_job_with_no_lease_is_treated_as_abandoned(monkeypatch, fake_postgres) -> None:
    _ok(monkeypatch)
    job = _job(fake_postgres, status="running")  # claimed before leases existed: NULL lease

    assert await quality_job_runner.drain_once() == 1
    assert fake_postgres.get("quality_execution_jobs", job["id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_a_job_that_keeps_losing_its_worker_ends_stale_not_retried_forever(monkeypatch, fake_postgres) -> None:
    calls: list = []
    _ok(monkeypatch, calls)
    job = _job(
        fake_postgres, status="running", attempt_count=settings.quality_job_max_attempts,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    await quality_job_runner.drain_once()

    row = fake_postgres.get("quality_execution_jobs", job["id"])
    assert row["status"] == "stale"
    assert row["error_code"] == "ATTEMPTS_EXHAUSTED"
    assert row["retryable"] is True
    assert calls == []  # never executed again
    assert await quality_job_runner._load_records(job["id"]) is not None  # kept: a stale job can be resumed


@pytest.mark.asyncio
async def test_shutting_down_mid_job_leaves_it_running_with_its_records_for_the_next_worker(monkeypatch, fake_postgres) -> None:
    started = asyncio.Event()

    async def _slow(**kwargs):
        started.set()
        await asyncio.sleep(3600)

    patch_job_execution(monkeypatch, _slow)
    job = _job(fake_postgres)

    claimed = await quality_job_runner.claim_next_job()
    task = asyncio.create_task(quality_job_runner._execute(claimed))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = fake_postgres.get("quality_execution_jobs", job["id"])
    assert row["status"] == "running"  # not failed: its lease lapses and someone reclaims it
    assert await quality_job_runner._load_records(job["id"]) == [{"a": 1}]  # input kept for the retry


@pytest.mark.asyncio
async def test_the_heartbeat_keeps_pushing_the_lease_forward_while_a_job_runs(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "quality_job_lease_seconds", 0.2)
    job = _job(fake_postgres)
    claimed = await quality_job_runner.claim_next_job()
    first_lease = fake_postgres.get("quality_execution_jobs", job["id"])["lease_expires_at"]

    heartbeat = asyncio.create_task(quality_job_runner._heartbeat(claimed["id"]))
    await asyncio.sleep(0.35)
    heartbeat.cancel()

    assert fake_postgres.get("quality_execution_jobs", job["id"])["lease_expires_at"] > first_lease


@pytest.mark.asyncio
async def test_the_heartbeat_stops_once_the_job_is_no_longer_running(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "quality_job_lease_seconds", 0.1)
    job = _job(fake_postgres)
    claimed = await quality_job_runner.claim_next_job()
    fake_postgres.quality_execution_jobs[job["id"]].status = "succeeded"

    await asyncio.wait_for(quality_job_runner._heartbeat(claimed["id"]), timeout=2)  # returns by itself


@pytest.mark.asyncio
async def test_the_worker_loop_picks_up_a_submitted_job_by_itself(monkeypatch, fake_postgres) -> None:
    _ok(monkeypatch)
    monkeypatch.setattr(settings, "quality_job_poll_interval_seconds", 0.05)
    await quality_job_runner.start_worker()
    try:
        job = await quality_service.submit_execution_job(
            org_id="org-1", pipeline={"id": "pipe-1", "topic_name": "t"}, policy={"id": "pol-1"}, records=[{"a": 1}],
        )
        for _ in range(100):
            if fake_postgres.get("quality_execution_jobs", job["id"])["status"] == "succeeded":
                break
            await asyncio.sleep(0.05)
    finally:
        await quality_job_runner.stop_worker()

    assert fake_postgres.get("quality_execution_jobs", job["id"])["status"] == "succeeded"
