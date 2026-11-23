from fastapi import APIRouter, Depends, HTTPException, status
from gateway_auth import Principal

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.capability import QualityCapabilities
from app.schemas.common import ValidationResult
from app.schemas.quality import (
    QualityExecutionJob,
    QualityExecutionRequest,
    QualityPolicy,
    QualityPolicyCreate,
    QualityPreviewResult,
    QualityResubmitRequest,
    QualityResult,
    QualityStreamStats,
    StreamingQualityRequest,
    StreamingQualityResult,
)
from app.services.kafka_runtime import KafkaProduceError, KafkaProducer, get_kafka_producer
from app.services.pipeline_service import pipeline_service
from app.services.quality_capabilities import quality_capabilities
from app.services.quality_service import JobStateConflict, quality_service
from app.services.safe_error import to_safe_error

router = APIRouter()


@router.get("/quality-policies", response_model=list[QualityPolicy])
async def list_quality_policies(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:view")),
) -> list[dict]:
    return await quality_service.list_policies(org_id)


@router.post("/quality-policies", response_model=QualityPolicy, status_code=status.HTTP_201_CREATED)
async def create_quality_policy(
    payload: QualityPolicyCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    try:
        created = await quality_service.create_policy(org_id, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        org_id=org_id, actor=actor, action="created",
        resource_type="quality_policy", resource_id=created["id"], after_state=created,
    )
    return created


@router.get(
    "/quality-policies/capabilities", response_model=QualityCapabilities,
    summary="Which quarantine destinations and failure actions can actually be used",
)
async def quality_policy_capabilities(
    _principal: Principal = Depends(require("dataconnect:quality:view")),
) -> dict:
    """NEW-09: every option a quality policy offers, each with an availability — supported, requires_configuration (implemented, but this
    deployment is not set up for it) or unavailable (not implemented; choosing it is refused) — and a stable reason code. A screen builds its choices
    from this, so it never offers one the API would reject. Declared before /quality-policies/{policy_id} so "capabilities" is not read as an id."""
    return quality_capabilities()


@router.get("/quality-policies/{policy_id}", response_model=QualityPolicy)
async def get_quality_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:view")),
) -> dict:
    policy = await quality_service.get_policy(org_id, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Quality policy not found")
    return policy


@router.put("/quality-policies/{policy_id}", response_model=QualityPolicy)
async def update_quality_policy(
    policy_id: str,
    payload: QualityPolicyCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    before = await get_quality_policy(policy_id, org_id)  # 404s if missing or owned by another org
    try:
        updated = await quality_service.update_policy(org_id, policy_id, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(
        org_id=org_id, actor=actor, action="updated",
        resource_type="quality_policy", resource_id=policy_id,
        before_state=before, after_state=updated,
    )
    return updated


@router.delete("/quality-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quality_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> None:
    before = await get_quality_policy(policy_id, org_id)  # 404s if missing or owned by another org
    await quality_service.delete_policy(org_id, policy_id)
    await record_audit(
        org_id=org_id, actor=actor, action="deleted",
        resource_type="quality_policy", resource_id=policy_id, before_state=before,
    )


@router.post("/quality-policies/{policy_id}/validate", response_model=ValidationResult)
async def validate_quality_policy_endpoint(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    policy = await get_quality_policy(policy_id, org_id)
    return quality_service.validate_policy(policy)


@router.get("/quality-results", response_model=list[QualityResult])
async def list_quality_results(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:view")),
) -> list[dict]:
    return await quality_service.list_results(org_id)


@router.get("/quality-stream-stats", response_model=list[QualityStreamStats])
async def list_quality_stream_stats(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:view")),
) -> list[dict]:
    """Live per-pipeline running totals from QualityStreamWorker's
    continuous, real-record enforcement — see QualityStreamStats's own
    docstring for why this is a separate list from /quality-results."""
    return await quality_service.list_stream_stats(org_id)


@router.post("/quality-policies/{policy_id}/preview", response_model=QualityPreviewResult)
async def preview_quality_policy(
    policy_id: str,
    payload: QualityExecutionRequest,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    """Synchronous, bounded (settings.quality_preview_max_records) — for
    UI/testing against a handful of records. Never persists a QualityResult
    row and never quarantines anything; for a real batch, submit an
    execution job instead."""
    policy = await get_quality_policy(policy_id, org_id)
    pipeline = await pipeline_service.get_pipeline(org_id, payload.pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    try:
        return quality_service.preview_policy(
            org_id=org_id, pipeline=pipeline, policy=policy, records=payload.records
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc


@router.post(
    "/quality-policies/{policy_id}/execute-jobs",
    response_model=QualityExecutionJob,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_quality_execution_job(
    policy_id: str,
    payload: QualityExecutionRequest,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    """Production batch path: bounded (settings.quality_job_max_records) but
    evaluated off the request thread — this returns immediately with a
    'queued' job; poll GET /quality-execution-jobs/{id} for its result."""
    policy = await get_quality_policy(policy_id, org_id)
    pipeline = await pipeline_service.get_pipeline(org_id, payload.pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    try:
        return await quality_service.submit_execution_job(
            org_id=org_id,
            pipeline=pipeline,
            policy=policy,
            records=payload.records,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc


@router.post(
    "/quality-execution-jobs/{job_id}/resume",
    response_model=QualityExecutionJob,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume a failed or stale execution job from its checkpoint",
)
async def resume_quality_execution_job(
    job_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    """A failed or stale job keeps its staged input until it expires; this queues it again and a worker continues after
    the last chunk it finished, instead of the caller resubmitting the whole batch."""
    try:
        job = await quality_service.resume_execution_job(org_id, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # A job whose input is gone raises QualityJobInputMissingError (410, QUALITY_JOB_INPUT_MISSING, not retryable, with the resubmit
    # action in `details`); it is rendered by the standard error handler.
    if job is None:
        raise HTTPException(status_code=404, detail="Quality execution job not found")
    return job


@router.post(
    "/quality-execution-jobs/{job_id}/resubmit",
    response_model=QualityExecutionJob,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit the batch again for a failed or stale execution job",
)
async def resubmit_quality_execution_job(
    job_id: str,
    payload: QualityResubmitRequest,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    """The way forward when a job's input records are gone (QUALITY_JOB_INPUT_MISSING — retrying cannot recreate them): supply the
    records again and a NEW job is queued against the same pipeline and policy, linked by `resubmitted_from`. Only a failed or
    stale job can be resubmitted; a queued or running one is left alone."""
    try:
        job = await quality_service.resubmit_execution_job(org_id, job_id, payload.records)
    except JobStateConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:  # the batch is over the size limits, as on the submit endpoint
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Quality execution job not found")
    return job


@router.get("/quality-execution-jobs/{job_id}", response_model=QualityExecutionJob)
async def get_quality_execution_job(
    job_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:quality:view")),
) -> dict:
    job = await quality_service.get_execution_job(org_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Quality execution job not found")
    return job


@router.post("/quality-policies/{policy_id}/stream/process", response_model=StreamingQualityResult)
async def process_streaming_quality_record(
    policy_id: str,
    payload: StreamingQualityRequest,
    org_id: str = Depends(get_org_id),
    producer: KafkaProducer = Depends(get_kafka_producer),
    _principal: Principal = Depends(require("dataconnect:quality:edit")),
) -> dict:
    policy = await get_quality_policy(policy_id, org_id)
    pipeline = await pipeline_service.get_pipeline(org_id, payload.pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    try:
        return await quality_service.process_streaming_record(
            producer=producer,
            pipeline=pipeline,
            policy=policy,
            record=payload.record,
        )
    except KafkaProduceError as exc:
        # The record could not be durably written — never report "processed"
        # when nothing landed in the prepared/quarantine topic. The error's own text embeds the Kafka client's / REST proxy's message
        # (broker addresses, a response body), so the caller gets the standard safe error (fixed message, code, log reference) and the
        # detail goes to the redacted log under that reference.
        safe = to_safe_error(exc, event="quality.stream_produce_failed", target="kafka_connect")
        raise HTTPException(status_code=502, detail=safe.describe()) from exc
