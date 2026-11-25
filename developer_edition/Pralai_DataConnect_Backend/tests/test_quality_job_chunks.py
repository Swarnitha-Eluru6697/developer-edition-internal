"""
GAP-13: a quality job's input is staged durably, bounded in bytes, and streamed in chunks with a checkpoint.

  - the batch is cut into ordered chunks (count / byte limits), validated against per-record and whole-batch byte
    limits, and stored in the same transaction as the job row;
  - a worker holds ONE chunk in memory at a time and commits a checkpoint after each;
  - a job reclaimed after its worker died RESUMES after the last finished chunk, with the same running totals;
  - a failed job keeps its input and can be resumed; a successful one cleans up;
  - an oversized request body is refused before it is parsed (app/core/body_limit.py).
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS, patch_job_execution
from fastapi.testclient import TestClient

import app.services.quality_job_runner as runner
import app.services.quality_service as quality_service_module
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import settings
from app.main import app
from app.services.kafka_runtime import InMemoryKafkaProducer
from app.services.quality_job_staging import plan_chunks, record_size
from app.services.quality_service import quality_service

ORG = "org_1"
POLICY = {
    "id": "pol-1", "on_failure": "route_to_quarantine",
    "rules": [{"name": "stock_range", "type": "min_max_numeric", "field": "stock", "min": 10, "max": 100}],
}


# ── planning the chunks ────────────────────────────────────────────────────────────────────────────────────────────


def test_a_batch_is_cut_into_ordered_chunks_by_record_count(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 3)
    records = [{"i": i} for i in range(8)]

    chunks = plan_chunks(records)

    assert [len(c.records) for c in chunks] == [3, 3, 2]
    assert [c.index for c in chunks] == [0, 1, 2]
    assert [r for c in chunks for r in c.records] == records  # nothing lost, nothing reordered
    assert all(c.byte_size > 0 for c in chunks)


def test_a_chunk_also_closes_when_it_reaches_its_byte_budget(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 1000)
    monkeypatch.setattr(settings, "quality_job_chunk_bytes", 1200)
    records = [{"payload": "x" * 400} for _ in range(7)]  # ~420 bytes each -> 2 per chunk

    chunks = plan_chunks(records)

    assert all(len(c.records) <= 2 for c in chunks)
    assert sum(len(c.records) for c in chunks) == 7
    assert all(c.byte_size <= 1200 for c in chunks)


def test_a_single_record_larger_than_a_chunk_still_gets_its_own_chunk(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_bytes", 500)
    monkeypatch.setattr(settings, "quality_job_max_record_bytes", 5000)

    chunks = plan_chunks([{"a": 1}, {"big": "y" * 2000}, {"a": 2}])

    assert [len(c.records) for c in chunks] == [1, 1, 1]


def test_an_empty_batch_is_one_empty_chunk() -> None:
    chunks = plan_chunks([])

    assert len(chunks) == 1
    assert chunks[0].records == []


def test_a_record_over_the_per_record_limit_is_refused_and_named(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_max_record_bytes", 100)

    with pytest.raises(ValueError, match=r"Record 2 is .* bytes; a single record may be at most 100 bytes"):
        plan_chunks([{"a": 1}, {"a": 2}, {"blob": "z" * 500}])


def test_a_batch_over_the_total_limit_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_max_total_bytes", 1000)
    monkeypatch.setattr(settings, "quality_job_max_record_bytes", 400)

    with pytest.raises(ValueError, match="larger than the 1,000 bytes a job may stage"):
        plan_chunks([{"blob": "z" * 300} for _ in range(10)])


def test_record_size_counts_the_utf8_bytes_as_stored() -> None:
    assert record_size({"a": "é"}) == len('{"a":"é"}'.encode())


# ── staging ────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def wired(monkeypatch, fake_postgres):
    fake_postgres.create("pipelines", {
        "id": "pipe-1", "org_id": ORG, "topic_name": "t", "direction": "SOURCE", "domain": "d", "source_system": "s",
        "entity": "e", "version": "v1",
    })
    fake_postgres.create("quality_policies", {"id": "pol-1", "org_id": ORG, **{k: v for k, v in POLICY.items() if k != "id"}})
    producer = InMemoryKafkaProducer()

    async def _producer():
        return producer

    monkeypatch.setattr("app.services.kafka_runtime.get_kafka_producer", _producer)
    stored = []

    class _Results:
        def __init__(self, session):
            pass

        async def create(self, values):
            values = {**values, "id": str(uuid4())}
            stored.append(values)
            return type("Row", (), {**values, "created_at": None, "updated_at": None})()

        async def create_once_for_job(self, values, *, job_id):
            """The contract of the real one (NEW-05): the first call stores the job's result, a repeat returns that same row."""
            existing = next((v for v in stored if v.get("job_id") == job_id), None)
            if existing is not None:
                return type("Row", (), {**existing, "created_at": None, "updated_at": None})(), False
            values = {**values, "id": str(uuid4()), "job_id": job_id}
            stored.append(values)
            return type("Row", (), {**values, "created_at": None, "updated_at": None})(), True

    monkeypatch.setattr(quality_service_module, "QualityResultRepository", _Results)
    return producer, stored


async def _submit(records, **kw):
    return await quality_service.submit_execution_job(
        org_id=ORG, pipeline={"id": "pipe-1", "topic_name": "t", "direction": "SOURCE"}, policy={"id": "pol-1"},
        records=records,
    )


@pytest.mark.asyncio
async def test_submit_stages_every_chunk_with_the_job_row_and_reports_the_count(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 4)

    job = await _submit([{"stock": i} for i in range(10)])

    assert job["record_count"] == 10
    assert job["chunks_total"] == 3
    assert job["chunks_completed"] == 0
    rows = sorted(
        (r for r in fake_postgres.quality_execution_job_records.values() if r.job_id == job["id"]),
        key=lambda r: r.chunk_index,
    )
    assert [r.chunk_index for r in rows] == [0, 1, 2]
    assert [len(r.records) for r in rows] == [4, 4, 2]
    assert all(r.byte_size > 0 and r.expires_at for r in rows)


@pytest.mark.asyncio
async def test_a_batch_breaking_a_byte_limit_stages_nothing(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_max_record_bytes", 50)

    with pytest.raises(ValueError, match="single record"):
        await _submit([{"stock": 1}, {"blob": "x" * 200}])

    assert fake_postgres.quality_execution_jobs == {}
    assert fake_postgres.quality_execution_job_records == {}


# ── running: one chunk at a time, aggregated ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_worker_holds_at_most_one_chunk_and_never_loads_the_whole_batch(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    seen = []

    async def _work(**kwargs):
        seen.append(len(kwargs["records"]))

    patch_job_execution(monkeypatch, _work)

    async def _forbidden(job_id):
        raise AssertionError("the worker must stream chunks, not load the whole batch")

    monkeypatch.setattr(runner, "_load_records", _forbidden)
    job = await _submit([{"stock": i} for i in range(23)])

    await runner.drain_once()

    assert seen == [5, 5, 5, 5, 3]
    assert fake_postgres.get("quality_execution_jobs", job["id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_a_chunked_job_gives_the_same_result_as_evaluating_the_whole_batch_at_once(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 7)
    monkeypatch.setattr(settings, "quality_max_sample_failures", 4)
    records = [{"stock": 5 if i % 3 == 0 else 50} for i in range(40)]  # 14 failures
    _, stored = wired

    await _submit(records)
    await runner.drain_once()
    chunked = stored[-1]

    stored.clear()
    whole = await quality_service.execute_policy(
        org_id=ORG, pipeline={"id": "pipe-1", "org_id": ORG, "topic_name": "t", "domain": "d", "source_system": "s", "entity": "e", "version": "v1", "direction": "SOURCE"},
        policy={"id": "pol-1", **POLICY}, records=records, producer=InMemoryKafkaProducer(),
    )

    assert chunked["failed_count"] == whole["failed_count"] == 14
    assert chunked["total_count"] == whole["total_count"] == 40
    assert chunked["status"] == whole["status"] == "failed"
    assert len(chunked["sample_failures"]) == 4  # still bounded, not the sum of every chunk's sample
    assert [s["original_record"] for s in chunked["sample_failures"]] == [s["original_record"] for s in whole["sample_failures"]]


@pytest.mark.asyncio
async def test_exactly_one_result_is_written_for_a_chunked_job(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    _, stored = wired

    await _submit([{"stock": 50} for _ in range(22)])
    await runner.drain_once()

    assert len(stored) == 1
    assert stored[0]["status"] == "passed"
    assert stored[0]["total_count"] == 22


@pytest.mark.asyncio
async def test_a_succeeded_job_deletes_its_staged_chunks(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    job = await _submit([{"stock": 50} for _ in range(12)])

    await runner.drain_once()

    assert not [r for r in fake_postgres.quality_execution_job_records.values() if r.job_id == job["id"]]


@pytest.mark.asyncio
async def test_progress_is_visible_on_the_job_as_chunks_complete(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    snapshots = []

    async def _work(**kwargs):
        job = await quality_service.get_execution_job(ORG, kwargs["dedupe_prefix"].split(":")[0])
        snapshots.append((job["chunks_completed"], job["chunks_total"]))

    patch_job_execution(monkeypatch, _work)
    await _submit([{"stock": 50} for _ in range(12)])

    await runner.drain_once()

    assert snapshots == [(0, 3), (1, 3), (2, 3)]  # seen while each chunk starts


# ── resuming after a dead worker ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_job_whose_worker_died_resumes_after_its_last_finished_chunk_with_the_same_totals(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    _, stored = wired
    records = [{"stock": 5 if i % 2 == 0 else 50} for i in range(23)]  # 12 failures
    job = await _submit(records)
    real = quality_service.process_job_chunk
    processed: list[str] = []

    async def _dies_on_chunk_three(**kwargs):
        processed.append(kwargs["dedupe_prefix"])
        if kwargs["dedupe_prefix"].endswith(":3"):
            raise asyncio.CancelledError  # the process is gone
        return await real(**kwargs)

    monkeypatch.setattr(quality_service, "process_job_chunk", _dies_on_chunk_three)
    claimed = await runner.claim_next_job()
    with pytest.raises(asyncio.CancelledError):
        await runner._execute(claimed)

    row = fake_postgres.quality_execution_jobs[job["id"]]
    assert row.status == "running"  # left for the next worker
    assert row.progress["chunks_completed"] == 3
    assert stored == []  # nothing final was written yet

    # lease lapses; a surviving worker takes it over
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    processed.clear()
    monkeypatch.setattr(quality_service, "process_job_chunk", real)

    async def _record(**kwargs):
        processed.append(kwargs["dedupe_prefix"])
        return await real(**kwargs)

    monkeypatch.setattr(quality_service, "process_job_chunk", _record)
    await runner.drain_once()

    assert [p.rsplit(":", 1)[1] for p in processed] == ["3", "4"]  # chunks 0-2 were NOT evaluated again
    finished = fake_postgres.get("quality_execution_jobs", job["id"])
    assert finished["status"] == "succeeded"
    assert finished["attempt_count"] == 2
    assert stored[0]["total_count"] == 23
    assert stored[0]["failed_count"] == 12  # the earlier chunks' counts were carried over


@pytest.mark.asyncio
async def test_a_chunk_interrupted_midway_is_rewritten_with_the_same_dedupe_keys(monkeypatch, fake_postgres, wired) -> None:
    producer, _ = wired
    monkeypatch.setattr(settings, "quality_job_chunk_records", 10)
    job = await _submit([{"stock": 5} for _ in range(10)])  # every record fails -> 10 quarantine messages in chunk 0
    first = await quality_service.process_job_chunk(
        org_id=ORG, pipeline={"id": "pipe-1", "org_id": ORG, "topic_name": "t", "domain": "d", "source_system": "s", "entity": "e", "version": "v1", "direction": "SOURCE"},
        policy={"id": "pol-1", **POLICY}, records=[{"stock": 5}] * 10, producer=producer, dedupe_prefix=f"{job['id']}:0",
    )
    keys_attempt_1 = [r.headers["inai.dedupe_key"] for r in producer.records]
    producer.records.clear()

    await quality_service.process_job_chunk(  # the resumed worker redoes the chunk
        org_id=ORG, pipeline={"id": "pipe-1", "org_id": ORG, "topic_name": "t", "domain": "d", "source_system": "s", "entity": "e", "version": "v1", "direction": "SOURCE"},
        policy={"id": "pol-1", **POLICY}, records=[{"stock": 5}] * 10, producer=producer, dedupe_prefix=f"{job['id']}:0",
    )

    assert first["failed"] == 10
    assert [r.headers["inai.dedupe_key"] for r in producer.records] == keys_attempt_1
    assert len(set(keys_attempt_1)) == 10  # distinct within the chunk, identical across attempts


@pytest.mark.asyncio
async def test_a_worker_that_lost_the_job_stops_without_finalizing(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    _, stored = wired
    job = await _submit([{"stock": 50} for _ in range(12)])

    async def _loses_the_job(**kwargs):
        fake_postgres.quality_execution_jobs[job["id"]].status = "queued"  # reclaimed elsewhere

    patch_job_execution(monkeypatch, _loses_the_job)
    claimed = await runner.claim_next_job()

    await runner._execute(claimed)

    assert stored == []
    assert fake_postgres.get("quality_execution_jobs", job["id"])["status"] == "queued"  # untouched by the loser
    assert [r for r in fake_postgres.quality_execution_job_records.values() if r.job_id == job["id"]]  # input kept


@pytest.mark.asyncio
async def test_a_job_staged_before_chunking_existed_runs_as_one_legacy_chunk(monkeypatch, fake_postgres, wired) -> None:
    seen = []

    async def _work(**kwargs):
        seen.append(len(kwargs["records"]))

    patch_job_execution(monkeypatch, _work)
    job = fake_postgres.create("quality_execution_jobs", {
        "org_id": ORG, "pipeline_id": "pipe-1", "quality_policy_id": "pol-1", "status": "queued", "record_count": 3,
    })  # no chunk_count / progress: the old shape
    fake_postgres.create("quality_execution_job_records", {
        "job_id": job["id"], "org_id": ORG, "records": [{"stock": 1}] * 3, "expires_at": datetime.now(UTC) + timedelta(hours=1),
    })

    await runner.drain_once()

    assert seen == [3]
    assert fake_postgres.get("quality_execution_jobs", job["id"])["status"] == "succeeded"


# ── a failed job can be resumed ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_job_keeps_its_input_and_its_checkpoint_and_can_be_resumed(monkeypatch, fake_postgres, wired) -> None:
    monkeypatch.setattr(settings, "quality_job_chunk_records", 5)
    _, stored = wired
    job = await _submit([{"stock": 50} for _ in range(12)])
    real = quality_service.process_job_chunk
    broken = {"on": True}

    async def _flaky(**kwargs):
        if kwargs["dedupe_prefix"].endswith(":2") and broken["on"]:
            raise ConnectionError("kafka down")
        return await real(**kwargs)

    monkeypatch.setattr(quality_service, "process_job_chunk", _flaky)
    await runner.drain_once()
    failed = fake_postgres.get("quality_execution_jobs", job["id"])
    assert failed["status"] == "failed"
    assert failed["progress"]["chunks_completed"] == 2  # chunks 0 and 1 were done
    assert [r for r in fake_postgres.quality_execution_job_records.values() if r.job_id == job["id"]]
    assert await runner.drain_once() == 0  # a failed job is not retried by itself

    broken["on"] = False
    resumed = await quality_service.resume_execution_job(ORG, job["id"])
    assert resumed["status"] == "queued"
    assert resumed["chunks_completed"] == 2

    await runner.drain_once()

    done = fake_postgres.get("quality_execution_jobs", job["id"])
    assert done["status"] == "succeeded"
    assert done["error_code"] is None
    assert stored[0]["total_count"] == 12
    assert not [r for r in fake_postgres.quality_execution_job_records.values() if r.job_id == job["id"]]


@pytest.mark.asyncio
async def test_resuming_a_job_extends_the_lifetime_of_its_staged_input(monkeypatch, fake_postgres, wired) -> None:
    async def _boom(**kwargs):
        raise ConnectionError("x")

    patch_job_execution(monkeypatch, _boom)
    job = await _submit([{"stock": 50}])
    await runner.drain_once()
    for row in fake_postgres.quality_execution_job_records.values():
        row.expires_at = datetime.now(UTC) + timedelta(minutes=1)

    await quality_service.resume_execution_job(ORG, job["id"])

    assert all(
        r.expires_at > datetime.now(UTC) + timedelta(hours=1)
        for r in fake_postgres.quality_execution_job_records.values()
    )


@pytest.mark.asyncio
async def test_only_a_failed_or_stale_job_can_be_resumed(fake_postgres, wired) -> None:
    job = await _submit([{"stock": 50}])  # queued

    with pytest.raises(ValueError, match="Only a failed or stale job"):
        await quality_service.resume_execution_job(ORG, job["id"])

    assert await quality_service.resume_execution_job(ORG, "no-such-job") is None
    assert await quality_service.resume_execution_job("other-org", job["id"]) is None


@pytest.mark.asyncio
async def test_a_job_whose_input_has_expired_cannot_be_resumed(fake_postgres, wired) -> None:
    job = await _submit([{"stock": 50}])
    fake_postgres.quality_execution_jobs[job["id"]].status = "failed"
    for row_id in [r.id for r in fake_postgres.quality_execution_job_records.values()]:
        fake_postgres.quality_execution_job_records.pop(row_id)

    from app.core.errors import QualityJobInputMissingError

    with pytest.raises(QualityJobInputMissingError, match="no longer available"):   # BUG-06: typed, non-retryable
        await quality_service.resume_execution_job(ORG, job["id"])


# ── the request body is limited before it is parsed ───────────────────────────────────────────────────────────────

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": ORG}


def test_an_oversized_declared_body_is_refused_with_413_before_the_route_runs(monkeypatch) -> None:
    monkeypatch.setattr(settings, "quality_job_max_request_bytes", 1000)
    body = b'{"pipeline_id":"p","records":[' + b'{"a":"' + b"x" * 5000 + b'"}' + b"]}"

    response = client.post(
        "/api/v1/dataconnect/quality-policies/pol-1/execute-jobs", content=body,
        headers={**HDR, "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.json()["details"]["limit_bytes"] == 1000


def test_a_body_within_the_limit_reaches_the_route(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "quality_job_max_request_bytes", 100_000)

    response = client.post(
        "/api/v1/dataconnect/quality-policies/pol-1/execute-jobs", json={"pipeline_id": "p", "records": [{"a": 1}]},
        headers=HDR,
    )

    assert response.status_code != 413  # (404/403 from the route itself: the point is it was not stopped)


def test_the_limit_does_not_apply_to_other_routes(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "quality_job_max_request_bytes", 10)

    response = client.get("/api/v1/dataconnect/pipelines", headers=HDR)

    assert response.status_code != 413


@pytest.mark.asyncio
async def test_a_streamed_body_with_no_honest_length_is_cut_off_at_the_limit() -> None:
    """Chunked transfer has no Content-Length to check up front: the bytes are counted as they arrive."""
    reached_handler = []

    async def inner(scope, receive, send):
        reached_handler.append(True)
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = BodySizeLimitMiddleware(inner, rules=[(re.compile(r"/upload$"), lambda: 100)])
    sent = []
    messages = [{"type": "http.request", "body": b"x" * 60, "more_body": True},
                {"type": "http.request", "body": b"x" * 60, "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}  # no content-length

    await middleware(scope, receive, send)

    assert sent[0]["status"] == 413
    assert reached_handler  # it started, and was cut off mid-body


@pytest.mark.asyncio
async def test_a_lying_content_length_is_still_caught_by_the_byte_count() -> None:
    async def inner(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})

    middleware = BodySizeLimitMiddleware(inner, rules=[(re.compile(r"/upload$"), lambda: 100)])
    sent = []
    messages = [{"type": "http.request", "body": b"x" * 500, "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": [(b"content-length", b"10")]}  # claims 10

    await middleware(scope, receive, send)

    assert sent[0]["status"] == 413
