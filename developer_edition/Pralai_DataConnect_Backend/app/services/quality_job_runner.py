"""
Runs submitted QualityExecutionJobs on a Postgres claim/lease worker — the same model as
Operation (app/services/operation_runner.py), replacing the earlier in-process
`asyncio.create_task` that lost a running job the moment its process died and, to compensate,
blindly "resumed" every queued/running job at startup (which would have re-run a job another
live replica was still executing).

Lifecycle (app/models/quality.py's QualityExecutionJob docstring):
    queued -> a worker claims it atomically (QualityExecutionJobRepository.claim_next: status=running,
    attempt_count += 1, lease_expires_at = now + lease; FOR UPDATE SKIP LOCKED, so several workers
    never take the same row) -> a heartbeat keeps renewing the lease while the job runs ->
    succeeded / failed.
    If the process dies mid-run the lease just lapses; ANY worker's next poll reclaims the row. A
    job claimed more than settings.quality_job_max_attempts times without finishing ends `stale`.

A job is reconstructible from its row alone: the pipeline and policy are re-fetched by id and the
batch comes from QualityExecutionJobRecords (GAP-13, a separate TTL'd table so this row stays
small), so nothing the executing worker needs lives only in the submitting request's memory.

GAP-13: the batch is staged as ordered CHUNKS and a worker streams through them, holding one chunk in
memory at a time. After each chunk it commits a checkpoint on the job row (`progress`: chunks done and
the running failed/total/sample totals), so a job reclaimed after its worker died RESUMES after the last
finished chunk with the same totals instead of re-evaluating everything. Only the chunk that was in
flight when the worker died is redone — its quarantine messages carry a stable `inai.dedupe_key`, so the
repeat is recognisable to a consumer (delivery is at-least-once for that one chunk).

The staged chunks are deleted when the job SUCCEEDS, and kept when it fails or goes stale (until their
TTL) so a failed job can be inspected and resumed (QualityService.resume_execution_job) rather than
resubmitted from scratch. They are also kept on cancellation (graceful shutdown), so the job that gets
reclaimed still has its input. The TTL and the periodic `cleanup_expired_records` sweep are backstops, and the sweep NEVER deletes
the input of a queued or running job (BUG-06): a job can wait or run longer than the TTL. A job that fails or goes stale has its expiry
extended from that moment, so the resume window is measured from the failure, not from submission.

If a job's input is nonetheless gone (QUALITY_JOB_INPUT_MISSING) it is NOT retryable — running it again cannot recreate the records.
It fails once, with that code and retryable=false, and the job reports `resubmit` as its way forward (QualityService.resubmit_execution_job).

A module-level set holds a strong reference to each in-flight task so it isn't garbage collected
mid-flight — asyncio only holds a weak reference to a task nothing else is tracking.
"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.repositories.dataconnect import (
    QualityExecutionJobRecordsRepository,
    QualityExecutionJobRepository,
)
from app.services.safe_error import SafeError, manual_safe_error, to_safe_error

logger = structlog.get_logger(__name__)

_running_jobs: set[asyncio.Task] = set()
_cleanup_task: asyncio.Task | None = None
_worker_task: asyncio.Task | None = None
_wake_event = asyncio.Event()

INPUT_MISSING_CODE = "QUALITY_JOB_INPUT_MISSING"
INPUT_MISSING_MESSAGE = (
    "This job's input records are no longer available, so it cannot be retried or resumed. Resubmit the batch to run it again."
)

_MISSING_REFERENCE_ERROR = (
    "This job's pipeline or quality policy no longer exists, so it can't be run. "
    "Please resubmit it against a valid pipeline/policy."
)


async def _finish_failed(job_id: str, safe_error: SafeError, status: str = "failed") -> None:
    async with AsyncSessionLocal() as session:
        repo = QualityExecutionJobRepository(session)
        job = await repo.get(job_id)
        if job is not None:
            await repo.update(
                job,
                {
                    "status": status, "completed_at": datetime.now(UTC),
                    "error_message": safe_error.message, "error_code": safe_error.code,
                    "retryable": safe_error.retryable, "correlation_id": safe_error.correlation_id,
                },
            )
            await session.commit()


async def _extend_input_retention(job_id: str) -> None:
    """A job that has just failed or gone stale keeps its input for a full TTL from NOW (its resume window), not from when it was
    submitted — a job that waited or ran for hours before failing must not have its input expire the moment it fails."""
    try:
        async with AsyncSessionLocal() as session:
            await QualityExecutionJobRecordsRepository(session).extend_expiry(
                job_id, datetime.now(UTC) + timedelta(seconds=settings.quality_job_records_ttl_seconds),
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - the failure is already recorded; a missed extension only shortens the resume window
        logger.warning("quality.execution_job_retention_extend_failed", job_id=job_id, exc_info=True)


async def _load_chunk(job_id: str, chunk_index: int) -> list[dict[str, Any]] | None:
    """One staged chunk of the batch, or None if it is gone."""
    async with AsyncSessionLocal() as session:
        row = await QualityExecutionJobRecordsRepository(session).get_chunk(job_id, chunk_index)
        return row.records if row is not None else None


async def _load_records(job_id: str) -> list[dict[str, Any]] | None:
    """The WHOLE staged batch, concatenated — for tests and diagnostics only. The worker never does this: it streams
    chunk by chunk (_load_chunk), which is the point of chunking."""
    async with AsyncSessionLocal() as session:
        chunks = await QualityExecutionJobRecordsRepository(session).list_chunks(job_id)
        return [record for chunk in chunks for record in chunk.records] if chunks else None


async def _delete_records(job_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await QualityExecutionJobRecordsRepository(session).delete_by_job_id(job_id)
        await session.commit()


async def start_cleanup_task() -> None:
    """Idempotent — safe to call more than once (a no-op if already
    running), matching operation_runner.start_worker's convention."""
    global _cleanup_task
    if _cleanup_task is None:
        _cleanup_task = asyncio.create_task(_cleanup_loop())


async def stop_cleanup_task() -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None


async def _cleanup_loop() -> None:
    while True:
        try:
            deleted = await cleanup_expired_records()
            if deleted:
                logger.info("quality_execution_job_records.expired_cleaned_up", count=deleted)
        except Exception:  # noqa: BLE001 - one bad sweep must not kill the whole periodic loop
            logger.exception("quality_execution_job_records.cleanup_failed")
        await asyncio.sleep(settings.quality_job_records_cleanup_interval_seconds)


async def cleanup_expired_records() -> int:
    """Backstop for QualityExecutionJobRecords rows that outlived their
    `expires_at` without being deleted by the normal completion/resume
    paths — see this module's own docstring for why that should be rare.
    Returns how many were deleted."""
    async with AsyncSessionLocal() as session:
        deleted = await QualityExecutionJobRecordsRepository(session).delete_expired(datetime.now(UTC))
        await session.commit()
        return deleted


def wake_worker() -> None:
    """Called right after a job is committed as `queued`, so this process's worker claims it
    immediately instead of waiting out the poll interval. Any other replica's worker may claim it
    first — that is fine, claiming is atomic."""
    _wake_event.set()


async def start_worker() -> None:
    """Idempotent — safe to call more than once (a no-op if already running)."""
    global _worker_task
    if _worker_task is None:
        _worker_task = asyncio.create_task(_poll_loop())


async def stop_worker() -> None:
    """Stops claiming, then cancels in-flight jobs. A cancelled job stays `running` with a lease
    that lapses, so another worker (or this one after a restart) reclaims it; its records are kept."""
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task
        _worker_task = None
    for task in list(_running_jobs):
        task.cancel()
    for task in list(_running_jobs):
        with suppress(asyncio.CancelledError):
            await task


async def _poll_loop() -> None:
    while True:
        try:
            claimed = await claim_next_job()
        except Exception:  # noqa: BLE001 - a claim failure (e.g. DB briefly down) must not kill the worker loop
            logger.warning("quality_job_worker.claim_failed", exc_info=True)
            claimed = None

        if claimed is not None:
            task = asyncio.create_task(_execute(claimed))
            _running_jobs.add(task)
            task.add_done_callback(_running_jobs.discard)
            continue  # try to claim another right away rather than waiting out the poll interval

        _wake_event.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake_event.wait(), timeout=settings.quality_job_poll_interval_seconds)


async def claim_next_job() -> dict[str, Any] | None:
    """Atomically claims one queued-or-abandoned job (see QualityExecutionJobRepository.claim_next)."""
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        row = await QualityExecutionJobRepository(session).claim_next(
            now, now + timedelta(seconds=settings.quality_job_lease_seconds),
        )
        await session.commit()
        if row is None:
            return None
        return {
            "id": row.id, "org_id": row.org_id, "pipeline_id": row.pipeline_id,
            "quality_policy_id": row.quality_policy_id, "attempt_count": row.attempt_count,
            "chunk_count": getattr(row, "chunk_count", 0) or 0, "progress": getattr(row, "progress", None) or {},
        }


async def drain_once() -> int:
    """Claims and runs, one after another, every job that is currently claimable; returns how many.
    The worker loop does the same concurrently in the background — this exists so a caller (or a
    test) can run pending work to completion deterministically."""
    ran = 0
    while (job := await claim_next_job()) is not None:
        await _execute(job)
        ran += 1
    return ran


async def _heartbeat(job_id: str) -> None:
    """Renews the lease partway through so a still-running, still-alive worker never loses its own
    claim to another worker's poll — see settings.quality_job_lease_seconds."""
    interval = settings.quality_job_lease_seconds / 2
    while True:
        await asyncio.sleep(interval)
        try:
            async with AsyncSessionLocal() as session:
                still_ours = await QualityExecutionJobRepository(session).renew_lease(
                    job_id, datetime.now(UTC) + timedelta(seconds=settings.quality_job_lease_seconds),
                )
                await session.commit()
            if not still_ours:
                return
        except Exception:  # noqa: BLE001 - a transient renewal failure shouldn't kill a healthy run; worst case the lease lapses and another worker reclaims it
            logger.warning("quality_job.heartbeat_failed", job_id=job_id, exc_info=True)


async def _save_progress(job_id: str, progress: dict[str, Any]) -> bool:
    """Commit the checkpoint. False when the job is no longer `running` under this worker."""
    async with AsyncSessionLocal() as session:
        still_ours = await QualityExecutionJobRepository(session).save_progress(job_id, progress)
        await session.commit()
        return still_ours


async def _execute(job: dict[str, Any]) -> None:
    # Deferred imports: these modules import this one (directly or transitively), so importing
    # them at load time would be circular.
    from app.services.kafka_runtime import get_kafka_producer
    from app.services.pipeline_service import pipeline_service
    from app.services.quality_service import quality_service

    job_id = job["id"]

    if job["attempt_count"] > settings.quality_job_max_attempts:
        safe_error = manual_safe_error(
            "ATTEMPTS_EXHAUSTED",
            (
                f"Gave up after {job['attempt_count']} attempts — this job was repeatedly claimed "
                "but never reached a terminal status. Please resubmit it."
            ),
            retryable=True, event="quality.execution_job_attempts_exhausted",
            job_id=job_id, attempt_count=job["attempt_count"],
        )
        await _finish_failed(job_id, safe_error, status="stale")
        await _extend_input_retention(job_id)
        return  # staged chunks are kept (until their TTL) so the job can be resumed

    pipeline = await pipeline_service.get_pipeline(job["org_id"], job["pipeline_id"])
    policy = await quality_service.get_policy(job["org_id"], job["quality_policy_id"])
    if pipeline is None or policy is None:
        safe_error = manual_safe_error(
            "JOB_REFERENCE_MISSING", _MISSING_REFERENCE_ERROR, retryable=True,
            event="quality.execution_job_reference_missing", job_id=job_id,
        )
        await _finish_failed(job_id, safe_error)
        await _delete_records(job_id)
        return

    total_chunks = job.get("chunk_count") or 1  # 0 = staged before chunking existed: one legacy chunk
    progress = dict(job.get("progress") or {})
    start = int(progress.get("chunks_completed", 0))
    producer = await get_kafka_producer()
    limit = settings.quality_max_sample_failures
    if start:
        logger.info("quality.execution_job_resumed", job_id=job_id, from_chunk=start, of=total_chunks)

    heartbeat_task = asyncio.create_task(_heartbeat(job_id))
    try:
        for index in range(start, total_chunks):
            records = await _load_chunk(job_id, index)
            if records is None:
                # Shouldn't happen for a job still queued/running (chunks are created atomically with the job row — see
                # submit_execution_job), but never crash on it: an honest failure beats a bare exception.
                # BUG-06: retrying the same job cannot recreate records that are gone, so this is terminal and NOT retryable.
                safe_error = manual_safe_error(
                    INPUT_MISSING_CODE, INPUT_MISSING_MESSAGE,
                    retryable=False, event="quality.execution_job_input_missing", job_id=job_id, chunk=index,
                )
                await _finish_failed(job_id, safe_error)
                return
            chunk = await quality_service.process_job_chunk(
                org_id=job["org_id"], pipeline=pipeline, policy=policy, records=records, producer=producer,
                dedupe_prefix=f"{job_id}:{index}",
            )
            del records  # this chunk is done with: only the next one is ever held
            progress = {
                "chunks_completed": index + 1,
                "failed": progress.get("failed", 0) + chunk["failed"],
                "total": progress.get("total", 0) + chunk["total"],
                "samples": (list(progress.get("samples", [])) + list(chunk["samples"]))[:limit],
            }
            if not await _save_progress(job_id, progress):
                # The job is no longer ours (its lease lapsed and another worker reclaimed it): stop, and leave its
                # chunks and checkpoint to whoever owns it now.
                logger.warning("quality.execution_job_lost_lease", job_id=job_id, at_chunk=index)
                return
        result = await quality_service.finalize_job_result(
            org_id=job["org_id"], pipeline=pipeline, policy=policy, progress=progress, job_id=job_id,
        )
    except asyncio.CancelledError:
        # Shutting down mid-run: leave the job `running` with its records intact — the lease lapses
        # and another worker reclaims it. Deleting the batch here would make that impossible.
        raise
    except Exception as exc:  # noqa: BLE001 - a background job's failure must be recorded, never lost to a bare task exception
        safe_error = to_safe_error(exc, event="quality.execution_job_failed", job_id=job_id)
        # Chunks and checkpoint are KEPT: a failed job is resumable (resume_execution_job), not only resubmittable.
        await _finish_failed(job_id, safe_error)
        await _extend_input_retention(job_id)
        return
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task

    async with AsyncSessionLocal() as session:
        repo = QualityExecutionJobRepository(session)
        row = await repo.get(job_id)
        if row is not None:
            await repo.update(
                row, {"status": "succeeded", "result_id": result["id"], "completed_at": datetime.now(UTC)},
            )
            await session.commit()
    await _delete_records(job_id)
    logger.info("quality.execution_job_succeeded", job_id=job_id, failed_count=result["failed_count"])
