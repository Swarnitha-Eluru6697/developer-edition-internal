"""
Quality policy CRUD is Postgres-authoritative (`dc_quality_policies` via
QualityPolicyRepository) — previously this went through the in-memory store
and never survived a restart. Every write opens its own session and commits
before returning, following the same pattern used for connection profiles
and transform policies (see app/services/connection_service.py).
Quality-check EXECUTION RESULTS are Postgres-backed too, as described below.

Redesign rationale: a quality result used to be built entirely in-memory and
never reached Postgres, despite the QualityResult model and
QualityResultRepository already existing — an execution's aggregates
vanished on the next restart, and its bounded `sample_failures` list was the
*only* record of which rows failed; every other failed record was silently
dropped. Now:
  - Aggregates (failed_count, total_count, status, ...) plus a small, bounded
    sample of failures (settings.quality_max_sample_failures) are persisted
    to dc_quality_results — bounded by construction, never an unbounded
    JSONB blob for a high-volume batch.
  - Every failed record (not just the bounded sample) is routed through the
    policy's quarantine_sink (Kafka today — see app/services/quarantine_sink.py)
    when on_failure is route_to_quarantine, so nothing is silently discarded.

A second redesign split how `records` reach this evaluation at all:
  - `preview_policy` is synchronous, bounded to settings.quality_preview_max_records,
    and never persists a QualityResult or quarantines anything — for
    UI/testing against a handful of records.
  - `submit_execution_job`/`get_execution_job` are the production batch path:
    bounded to settings.quality_job_max_records, but the actual evaluation +
    quarantine + persistence (still `execute_policy`, below) runs off the
    HTTP request thread via app/services/quality_job_runner.py, so a large
    batch is never a long synchronous request. Poll the job's status to get
    its result once it completes.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.errors import QualityJobInputMissingError, QualityPolicyInUseError, QualityRuleError
from app.db.session import AsyncSessionLocal
from app.models.quality import QualityExecutionJob, QualityPolicy, QualityResult, QualityStreamStats
from app.repositories.dataconnect import (
    QualityExecutionJobRecordsRepository,
    QualityExecutionJobRepository,
    QualityPolicyRepository,
    QualityResultRepository,
    QualityStreamStatsRepository,
)
from app.schemas.errors import FieldError
from app.services.kafka_runtime import KafkaProducer
from app.services.pipeline_topics import data_topic
from app.services.quality import (
    build_quality_result_summary,
    validate_quality_policy,
    validate_quality_policy_field_codes,
)
from app.services.quality_job_staging import plan_chunks
from app.services.quarantine_sink import get_quarantine_sink
from app.services.streaming_quality_processor import StreamingQualityProcessor
from app.services.ucs_quality_sync import emit_quality_result

logger = structlog.get_logger(__name__)


def _policy_to_dict(row: QualityPolicy) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "description": row.description,
        "rules": row.rules,
        "on_failure": row.on_failure,
        "quarantine_sink": row.quarantine_sink,
        "created_at": row.created_at,
    }


def _result_to_dict(row: QualityResult) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "pipeline_id": row.pipeline_id,
        "quality_policy_id": row.quality_policy_id,
        "topic_name": row.topic_name,
        "rule_name": row.rule_name,
        "severity": row.severity,
        "status": row.status,
        "failed_count": row.failed_count,
        "total_count": row.total_count,
        "sample_failures": row.sample_failures,
    }


def _stream_stats_to_dict(row: QualityStreamStats) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "pipeline_id": row.pipeline_id,
        "quality_policy_id": row.quality_policy_id,
        "total_count": row.total_count,
        "failed_count": row.failed_count,
        "passed_count": row.total_count - row.failed_count,
        "sample_failures": row.sample_failures,
        "updated_at": row.updated_at,
    }


def _job_to_dict(job: QualityExecutionJob, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": job.id,
        "org_id": job.org_id,
        "pipeline_id": job.pipeline_id,
        "quality_policy_id": job.quality_policy_id,
        "status": job.status,
        "record_count": job.record_count,
        "chunks_total": getattr(job, "chunk_count", 0) or 0,
        "chunks_completed": ((getattr(job, "progress", None) or {}).get("chunks_completed", 0)),
        "error_message": job.error_message,
        "error_code": job.error_code,
        "retryable": job.retryable,
        "correlation_id": job.correlation_id,
        "available_actions": _available_actions(job),
        "resubmitted_from": getattr(job, "resubmitted_from", None),
        "result": result,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


class JobStateConflict(Exception):
    """The job is not in a state this action applies to (rendered as 409), as opposed to a ValueError about the batch itself (413)."""


# Failure codes after which the staged input is gone (or deliberately dropped): resuming cannot work, only submitting again can.
_NOT_RESUMABLE_CODES = frozenset({"QUALITY_JOB_INPUT_MISSING", "JOB_REFERENCE_MISSING"})


def _available_actions(job: QualityExecutionJob) -> list[str]:
    """BUG-06: what a caller can do next. Only a failed or stale job has any; one whose input is gone can only be resubmitted."""
    if job.status not in ("failed", "stale"):
        return []
    if job.error_code in _NOT_RESUMABLE_CODES:
        return ["resubmit"]
    return ["resume", "resubmit"]


_CHUNK_INSERT_GROUP = 8  # chunk rows per INSERT: bounds the statement size when a batch has many large chunks


class QualityService:
    async def list_policies(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await QualityPolicyRepository(session).list_for_org(org_id)
            return [_policy_to_dict(row) for row in rows]

    async def get_policy(self, org_id: str, policy_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await QualityPolicyRepository(session).get_for_org(org_id, policy_id)
            return _policy_to_dict(row) if row else None

    def _validate_rules(self, payload: dict) -> None:
        errors = validate_quality_policy_field_codes(payload)
        if errors:
            raise QualityRuleError(
                ", ".join(message for _field, message, _code in errors),
                field_errors=[
                    FieldError(field=field, message=message, code=code) for field, message, code in errors
                ],
            )

    async def create_policy(self, org_id: str, payload: dict) -> dict:
        payload["org_id"] = org_id
        self._validate_rules(payload)
        async with AsyncSessionLocal() as session:
            row = await QualityPolicyRepository(session).create(payload)
            await session.commit()
            return _policy_to_dict(row)

    async def update_policy(self, org_id: str, policy_id: str, payload: dict) -> dict:
        payload["org_id"] = org_id
        self._validate_rules(payload)
        async with AsyncSessionLocal() as session:
            repo = QualityPolicyRepository(session)
            row = await repo.get_for_org(org_id, policy_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Quality policy not found"
                )
            updated_row = await repo.update(row, payload)
            await session.commit()
            return _policy_to_dict(updated_row)

    async def delete_policy(self, org_id: str, policy_id: str) -> None:
        """Refused (QualityPolicyInUseError) while a pipeline still uses the policy. Otherwise the policy goes together with its own
        history — its results, execution jobs and stream statistics, which describe nothing once the policy is gone. A pipeline's delete
        keeps that history but unlinks it from the pipeline, so before this the database refused the policy's delete ("references data that
        doesn't exist") and a policy whose pipelines were all deleted could never be deleted."""
        from app.models.pipeline import Pipeline

        async with AsyncSessionLocal() as session:
            repo = QualityPolicyRepository(session)
            row = await repo.get_for_org(org_id, policy_id)
            if row is None:
                return  # not found, or belongs to a different org — nothing to delete
            users = (await session.execute(
                select(Pipeline.name).where(Pipeline.org_id == org_id, Pipeline.quality_policy_id == policy_id).order_by(Pipeline.name)
            )).scalars().all()
            if users:
                raise QualityPolicyInUseError(
                    f"Quality policy '{row.name}' is used by {len(users)} pipeline(s): {', '.join(users)}. "
                    "Remove it from those pipelines first.",
                    details={"pipelines": list(users)},
                )
            # Jobs first: a job references its result, and the result references the policy.
            for model in (QualityExecutionJob, QualityResult, QualityStreamStats):
                await session.execute(delete(model).where(model.org_id == org_id, model.quality_policy_id == policy_id))
            await repo.delete(row)
            await session.commit()

    def validate_policy(self, policy: dict) -> dict:
        return validate_quality_policy(policy)

    async def list_results(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await QualityResultRepository(session).list_for_org(org_id)
            return [_result_to_dict(row) for row in rows]

    async def list_stream_stats(self, org_id: str) -> list[dict]:
        """Live, continuously-updated running totals per pipeline from
        QualityStreamWorker's real-time enforcement — see
        QualityStreamStats's own docstring for why this is intentionally
        separate from list_results()'s discrete Execute-button history."""
        async with AsyncSessionLocal() as session:
            rows = await QualityStreamStatsRepository(session).list_for_org(org_id)
            return [_stream_stats_to_dict(row) for row in rows]

    def preview_policy(
        self,
        *,
        org_id: str,
        pipeline: dict,
        policy: dict,
        records: list[dict],
    ) -> dict:
        """Synchronous, bounded, throwaway: never persists a QualityResult
        row and never quarantines anything. For a batch too large for
        preview, submit_execution_job is the intended path instead."""
        if len(records) > settings.quality_preview_max_records:
            raise ValueError(
                f"Preview is limited to {settings.quality_preview_max_records} records "
                f"(got {len(records)}); submit an execution job for larger batches."
            )
        summary, _ = build_quality_result_summary(
            org_id=org_id,
            pipeline_id=pipeline["id"],
            quality_policy_id=policy["id"],
            topic_name=data_topic(pipeline),
            records=records,
            policy=policy,
            # Preview's whole point is to show the caller what would happen —
            # bounded input already, so show every failure, not just the
            # small sample a persisted QualityResult keeps.
            sample_limit=len(records),
        )
        return summary

    async def submit_execution_job(
        self,
        *,
        org_id: str,
        pipeline: dict,
        policy: dict,
        records: list[dict],
        resubmitted_from: str | None = None,
    ) -> dict:
        """Durably records the submission and returns immediately (status
        'queued'); the actual evaluation/quarantine/persistence runs off the
        request thread on a claim/lease worker — see app/services/quality_job_runner.py."""
        if len(records) > settings.quality_job_max_records:
            raise ValueError(
                f"Execution jobs are limited to {settings.quality_job_max_records} records "
                f"per submission (got {len(records)})."
            )
        chunks = plan_chunks(records)  # also enforces the per-record and whole-batch byte limits (ValueError -> 413)
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.quality_job_records_ttl_seconds)
        async with AsyncSessionLocal() as session:
            repo = QualityExecutionJobRepository(session)
            job = await repo.create(
                {
                    "org_id": org_id,
                    "pipeline_id": pipeline["id"],
                    "quality_policy_id": policy["id"],
                    "status": "queued",
                    "record_count": len(records),
                    "chunk_count": len(chunks),
                    **({"resubmitted_from": resubmitted_from} if resubmitted_from else {}),
                }
            )
            # GAP-13: persisted in the same transaction as the job row
            # itself — either both exist or neither does, so a queued/
            # running job can never be found with no records to resume.
            records_repo = QualityExecutionJobRecordsRepository(session)
            for start in range(0, len(chunks), _CHUNK_INSERT_GROUP):
                await records_repo.create_many([
                    {
                        "job_id": job.id, "org_id": org_id, "chunk_index": chunk.index, "byte_size": chunk.byte_size,
                        "records": chunk.records, "expires_at": expires_at,
                    }
                    for chunk in chunks[start:start + _CHUNK_INSERT_GROUP]
                ])
            await session.commit()
            job_dict = _job_to_dict(job)

        from app.services.quality_job_runner import wake_worker

        wake_worker()  # nudge this process's worker; whichever worker claims the row runs it
        return job_dict

    async def resume_execution_job(self, org_id: str, job_id: str) -> dict | None:
        """GAP-13: put a failed/stale job back in the queue to continue from its checkpoint. A failed job keeps its staged
        chunks until they expire, which is what makes this possible. None if there is no such job; ValueError if it is
        not failed/stale.

        BUG-06: if the input it still needs is gone (expired, or deleted) the job cannot be resumed and retrying cannot help, so
        this records that on the job (QUALITY_JOB_INPUT_MISSING, retryable=false — it then offers only `resubmit`) and raises
        QualityJobInputMissingError instead of queueing a job that would only fail again."""
        async with AsyncSessionLocal() as session:
            repo = QualityExecutionJobRepository(session)
            job = await repo.get_for_org(org_id, job_id)
            if job is None:
                return None
            if job.status not in ("failed", "stale"):
                raise ValueError(f"Only a failed or stale job can be resumed; this one is {job.status}.")
            records_repo = QualityExecutionJobRecordsRepository(session)
            done = int((getattr(job, "progress", None) or {}).get("chunks_completed", 0))
            needed = max(1, getattr(job, "chunk_count", 0) or 0) - done   # chunk_count 0 = one legacy chunk
            if needed > 0 and await records_repo.count_chunks_from(job_id, done) < needed:
                from app.services.quality_job_runner import (
                    INPUT_MISSING_CODE,
                    INPUT_MISSING_MESSAGE,
                )

                if job.error_code != INPUT_MISSING_CODE:
                    await repo.update(job, {
                        "error_code": INPUT_MISSING_CODE, "error_message": INPUT_MISSING_MESSAGE, "retryable": False,
                    })
                    await session.commit()
                raise QualityJobInputMissingError(
                    INPUT_MISSING_MESSAGE,
                    details={"action": "resubmit", "resubmit_path": f"/quality-execution-jobs/{job_id}/resubmit"},
                )
            requeued = await repo.requeue(org_id, job_id)
            if requeued is None:  # raced with another resume
                raise ValueError("This job is no longer failed or stale.")
            await records_repo.extend_expiry(
                job_id, datetime.now(UTC) + timedelta(seconds=settings.quality_job_records_ttl_seconds),
            )
            await session.commit()
            job_dict = _job_to_dict(requeued)

        from app.services.quality_job_runner import wake_worker

        wake_worker()
        return job_dict

    async def resubmit_execution_job(self, org_id: str, job_id: str, records: list[dict]) -> dict | None:
        """BUG-06: submit the batch again for a failed/stale job — the way forward when its input is gone. A NEW job is created for
        the same pipeline and policy with the records the caller supplies, linked back by `resubmitted_from`; the original keeps
        its own failure. None if there is no such job; ValueError if it is queued/running/succeeded (never resubmit active work), or
        its pipeline or policy no longer exists (JobStateConflict); ValueError if the new batch is over the size limits."""
        async with AsyncSessionLocal() as session:
            original = await QualityExecutionJobRepository(session).get_for_org(org_id, job_id)
        if original is None:
            return None
        if original.status not in ("failed", "stale"):
            raise JobStateConflict(f"Only a failed or stale job can be resubmitted; this one is {original.status}.")
        from app.services.pipeline_service import pipeline_service

        pipeline = await pipeline_service.get_pipeline(org_id, original.pipeline_id)
        policy = await self.get_policy(org_id, original.quality_policy_id)
        if pipeline is None or policy is None:
            raise JobStateConflict("The pipeline or quality policy this job ran against no longer exists, so it cannot be resubmitted.")
        return await self.submit_execution_job(
            org_id=org_id, pipeline=pipeline, policy=policy, records=records, resubmitted_from=job_id,
        )

    async def get_execution_job(self, org_id: str, job_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            job = await QualityExecutionJobRepository(session).get_for_org(org_id, job_id)
            if job is None:
                return None
            result = None
            if job.result_id:
                result_row = await QualityResultRepository(session).get(job.result_id)
                result = _result_to_dict(result_row) if result_row else None
            return _job_to_dict(job, result=result)

    async def execute_policy(
        self,
        *,
        org_id: str,
        pipeline: dict,
        policy: dict,
        records: list[dict],
        producer: KafkaProducer,
        route_failures_to_quarantine: bool = True,
    ) -> dict:
        """route_failures_to_quarantine=False persists the QualityResult
        summary (still visible in the Quality tab) but never produces to the
        policy's quarantine topic — for PipelineService.autorun_quality_from_source's
        one-time create-time sample check, so it doesn't duplicate the
        QualityStreamWorker's own, continuous quarantine writes for the same
        pipeline (see that method's docstring). Every other caller (the
        manual Execute button / execute-jobs) keeps quarantining, since a
        deliberate user-triggered run against real records is exactly when
        seeing them actually routed to quarantine is the point."""
        summary, quarantine_envelopes = build_quality_result_summary(
            org_id=org_id,
            pipeline_id=pipeline["id"],
            quality_policy_id=policy["id"],
            topic_name=data_topic(pipeline),
            records=records,
            policy=policy,
        )
        await self._route_failures(
            summary=summary, envelopes=quarantine_envelopes, pipeline=pipeline, policy=policy, producer=producer,
            route_failures_to_quarantine=route_failures_to_quarantine,
        )
        return await self._persist_result(org_id, policy, summary)

    async def _route_failures(
        self, *, summary: dict, envelopes: list[dict], pipeline: dict, policy: dict, producer: KafkaProducer,
        route_failures_to_quarantine: bool = True, dedupe_prefix: str | None = None,
    ) -> None:
        if not envelopes:
            return
        if route_failures_to_quarantine and policy.get("on_failure", "route_to_quarantine") == "route_to_quarantine":
            sink = get_quarantine_sink(policy, producer)
            if dedupe_prefix is None:
                await sink.send(envelopes=envelopes, pipeline=pipeline)
            else:
                await sink.send(envelopes=envelopes, pipeline=pipeline, dedupe_prefix=dedupe_prefix)
        else:
            logger.info(
                "quality.failures_not_quarantined",
                pipeline_id=pipeline["id"],
                on_failure=policy.get("on_failure"),
                route_failures_to_quarantine=route_failures_to_quarantine,
                failed_count=summary["failed_count"],
            )

    async def _persist_result(self, org_id: str, policy: dict, summary: dict, *, job_id: str | None = None) -> dict:
        """Persists one result. With `job_id` (a chunked execution job) it is the job's ONE result: a repeat — the job reclaimed after a crash
        between this commit and marking the job finished — returns the result already stored and does not emit it to UCS again."""
        async with AsyncSessionLocal() as session:
            repo = QualityResultRepository(session)
            if job_id is None:
                row, created = await repo.create(summary), True
            else:
                row, created = await repo.create_once_for_job(summary, job_id=job_id)
            await session.commit()
            result = _result_to_dict(row)

        if created:
            # Best-effort, never blocks/fails this result on a UCS outage — see
            # ucs_quality_sync.emit_quality_result's own docstring.
            await emit_quality_result(org_id, policy, result)
        return result

    async def process_job_chunk(
        self, *, org_id: str, pipeline: dict, policy: dict, records: list[dict], producer: KafkaProducer,
        dedupe_prefix: str,
    ) -> dict:
        """GAP-13: evaluate ONE staged chunk of a job and route its failures to quarantine. Returns that chunk's
        contribution — {"failed", "total", "samples"} — for the worker to add to the job's checkpoint; nothing is
        persisted as a QualityResult until every chunk is done (finalize_job_result)."""
        summary, envelopes = build_quality_result_summary(
            org_id=org_id, pipeline_id=pipeline["id"], quality_policy_id=policy["id"],
            topic_name=data_topic(pipeline), records=records, policy=policy,
        )
        await self._route_failures(
            summary=summary, envelopes=envelopes, pipeline=pipeline, policy=policy, producer=producer,
            dedupe_prefix=dedupe_prefix,
        )
        return {"failed": summary["failed_count"], "total": summary["total_count"], "samples": summary["sample_failures"]}

    async def finalize_job_result(self, *, org_id: str, pipeline: dict, policy: dict, progress: dict, job_id: str | None = None) -> dict:
        """GAP-13: the single QualityResult for a chunked job, from its checkpoint's running totals. `job_id` (NEW-05) makes it idempotent: a job
        finalized twice (reclaimed after a crash before it was marked finished) still has exactly one result."""
        failed, total = progress.get("failed", 0), progress.get("total", 0)
        summary = {
            "org_id": org_id,
            "pipeline_id": pipeline["id"],
            "quality_policy_id": policy["id"],
            "topic_name": data_topic(pipeline),
            "rule_name": "*",
            "severity": "error",
            "status": "passed" if failed == 0 else "failed",
            "failed_count": failed,
            "total_count": total,
            "sample_failures": list(progress.get("samples", []))[: settings.quality_max_sample_failures],
        }
        return await self._persist_result(org_id, policy, summary, job_id=job_id)

    async def process_streaming_record(
        self,
        *,
        producer: KafkaProducer,
        pipeline: dict,
        policy: dict,
        record: dict,
    ) -> dict:
        processor = StreamingQualityProcessor(producer)
        return await processor.process_record(record=record, pipeline=pipeline, policy=policy)


quality_service = QualityService()
