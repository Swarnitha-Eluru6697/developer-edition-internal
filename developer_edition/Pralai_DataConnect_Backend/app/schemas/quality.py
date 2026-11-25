from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class QualityPolicyBase(BaseModel):
    name: str
    description: str | None = None
    rules: list[dict[str, Any]] = Field(default_factory=list)
    on_failure: str = "route_to_quarantine"
    quarantine_sink: str = "kafka"


class QualityPolicyCreate(QualityPolicyBase):
    id: str | None = None


class QualityPolicy(QualityPolicyBase):
    id: str
    org_id: str
    # When it was created (response-only, never client-set).
    created_at: datetime | None = None


class QualityResult(BaseModel):
    id: str
    org_id: str
    pipeline_id: str | None = None
    quality_policy_id: str | None = None
    topic_name: str | None = None
    rule_name: str | None = None
    severity: str | None = None
    status: str | None = None
    failed_count: int = 0
    total_count: int = 0
    sample_failures: list[dict[str, Any]] = Field(default_factory=list)


class QualityStreamStats(BaseModel):
    """Live running totals for one pipeline's continuous, real-record
    enforcement (QualityStreamWorker) — distinct from QualityResult, which
    is a discrete Execute-button/execute-job run against caller-supplied
    (often hand-typed sample) records. See app/models/quality.py's
    QualityStreamStats docstring for why the two never reconcile."""

    id: str
    org_id: str
    pipeline_id: str
    quality_policy_id: str | None = None
    total_count: int = 0
    failed_count: int = 0
    passed_count: int = 0
    sample_failures: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime


class QualityExecutionRequest(BaseModel):
    """Shared body shape for both preview (small, synchronous) and execution
    job (larger, backgrounded) submissions — each endpoint enforces its own
    record-count ceiling (settings.quality_preview_max_records /
    settings.quality_job_max_records) at the service layer, since the bound
    differs per endpoint and needs to read the current setting at request
    time rather than being baked into the schema."""

    pipeline_id: str
    records: list[dict[str, Any]]


class QualityPreviewResult(BaseModel):
    """Result of a synchronous preview run — never persisted as a
    QualityResult row and never quarantined; just the same aggregate shape,
    computed on the spot for UI/testing."""

    pipeline_id: str
    quality_policy_id: str
    topic_name: str | None = None
    status: str
    failed_count: int = 0
    total_count: int = 0
    sample_failures: list[dict[str, Any]] = Field(default_factory=list)


class QualityExecutionJob(BaseModel):
    id: str
    org_id: str
    pipeline_id: str
    quality_policy_id: str
    status: str
    record_count: int = 0
    # GAP-13: how far a chunked run has got — chunks_completed / chunks_total. A resumed job continues from here.
    chunks_total: int = 0
    chunks_completed: int = 0
    # BUG-11 — see app/schemas/operations.py's identical fields.
    error_message: str | None = None
    error_code: str | None = None
    retryable: bool | None = None
    correlation_id: str | None = None
    # BUG-06: what the caller can do about a failed/stale job — "resume" (continue from its checkpoint; needs its staged input) and/or
    # "resubmit" (submit the batch again). A job whose input is gone (error_code QUALITY_JOB_INPUT_MISSING, retryable=false) offers
    # only "resubmit". Empty for a job that is queued, running or succeeded.
    available_actions: list[str] = Field(default_factory=list)
    # Set on a job created by resubmitting another: the job it replaces.
    resubmitted_from: str | None = None
    result: QualityResult | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class QualityResubmitRequest(BaseModel):
    """Body of POST /quality-execution-jobs/{id}/resubmit. The pipeline and policy come from the job being replaced; the records
    must be supplied again, because the point is that the originals are gone."""

    records: list[dict[str, Any]]


class StreamingQualityRequest(BaseModel):
    pipeline_id: str
    record: dict[str, Any]


class StreamingQualityResult(BaseModel):
    status: str
    destination_topic: str
    failed_rules: list[str] = Field(default_factory=list)
    quarantine_record: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
