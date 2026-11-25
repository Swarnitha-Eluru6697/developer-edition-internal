"""Quality models — data quality policies and execution results."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class QualityPolicy(Base, UuidPk, TimestampMixin):
    """
    A named set of data quality rules applied to messages flowing through a pipeline.

    Each rule in `rules` is a dict with:
        name       : str   — unique rule name
        field      : str   — field to validate (dot-path for nested)
        check      : str   — not_null | regex | range | enum | custom
        params     : dict  — check-specific parameters
        severity   : str   — warning | error | critical
        on_failure : str   — log | quarantine | block (overrides policy default)

    When the policy is attached to a pipeline, the streaming quality processor
    evaluates each rule in-stream and routes failures to the quarantine topic
    (or applies the on_failure action) using the Kafka producer abstraction.

    `quarantine_sink` selects where FULL failed records (not the bounded
    sample kept on QualityResult) are sent when on_failure is
    route_to_quarantine — see app/services/quarantine_sink.py. Only "kafka"
    has a working backend today; "object_storage" is a reserved value for a
    future backend with no implementation yet — app/services/quality.py's
    validate_quality_policy_fields (BUG-09) rejects it at policy create/
    update time (a 422, before a policy is ever saved), rather than only
    failing the first time a real batch execution tries to quarantine a
    record through it.

    BUG-09 (defense in depth): the database enforces the same rule —
    ck_dc_quality_policies_quarantine_sink_implemented only admits sinks that
    actually have a backend — so a hand-written INSERT/UPDATE, a script or a
    future code path that skips the API's validation still can't store a
    policy that would fail the first time it quarantines a record. Keep it in
    step with app/services/quality.py's QUARANTINE_SINKS: implementing a new
    sink means widening BOTH (new migration for the constraint).
    """

    __tablename__ = "dc_quality_policies"
    __table_args__ = (
        CheckConstraint("quarantine_sink IN ('kafka')", name="ck_dc_quality_policies_quarantine_sink_implemented"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[list] = mapped_column(JSONB, default=list)
    on_failure: Mapped[str] = mapped_column(
        String(100),
        default="route_to_quarantine",
        comment="Default failure action: route_to_quarantine | log | block",
    )
    quarantine_sink: Mapped[str] = mapped_column(
        String(50),
        default="kafka",
        comment="Destination for full failed records: kafka (object_storage is reserved, not implemented — see the CHECK constraint)",
    )


class QualityResult(Base, UuidPk):
    """
    A point-in-time quality check result for a specific rule on a specific pipeline.

    Written by QualityService.execute_policy each time a policy is run
    against a batch. Only aggregates (failed_count, total_count, status) plus
    a small, bounded sample of failing records are stored here — see
    settings.quality_max_sample_failures — so a high-volume batch never turns
    this row's JSONB sample_failures column into an unbounded blob. Every
    failed record still reaches the policy's quarantine_sink; this table is
    not the record of what failed, only a summary of it.

    Aggregated by the monitoring service for the dashboard.

    `job_id` (NEW-05): the execution job a result summarises, UNIQUE, so one job has exactly one result. A job's result was committed first
    and the job marked finished afterwards; a crash between the two made the reclaiming worker finalize the job again and write a SECOND result
    for it (and emit it to UCS twice). The result of a job is now written once, whatever the crash window. NULL for a result that did not come
    from a job (a synchronous preview or execute).
    """

    __tablename__ = "dc_quality_results"
    __table_args__ = (UniqueConstraint("job_id", name="uq_dc_quality_results_job_id"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    # SET NULL, not the FK's implicit RESTRICT default: this is a historical
    # measurement, not something the pipeline owns — deleting the pipeline
    # must be able to proceed and just leave the summary orphaned.
    pipeline_id: Mapped[str | None] = mapped_column(
        ForeignKey("dc_pipelines.id", ondelete="SET NULL"), index=True
    )
    quality_policy_id: Mapped[str | None] = mapped_column(ForeignKey("dc_quality_policies.id"))
    topic_name: Mapped[str | None] = mapped_column(String(500))
    rule_name: Mapped[str | None] = mapped_column(String(255))
    severity: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str | None] = mapped_column(String(50))
    failed_count: Mapped[int] = mapped_column(default=0)
    total_count: Mapped[int] = mapped_column(default=0)
    sample_failures: Mapped[list] = mapped_column(JSONB, default=list)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    job_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))


class QualityExecutionJob(Base, UuidPk):
    """
    A production batch quality-check submission (POST .../execute-jobs),
    run off the HTTP request thread by app/services/quality_job_runner.py so
    a large `records` payload never turns into a long synchronous request.

    Preview (POST .../preview) never creates one of these — it's small,
    bounded, and synchronous by design (see QualityService.preview_policy);
    this table exists only for the asynchronous job path. `result_id` is set
    once the job succeeds, pointing at the QualityResult it produced.

    GAP-13: the job's actual `records` payload is NOT a column here — see
    QualityExecutionJobRecords below, a separate, TTL'd table (this row
    only ever tracks `record_count`, deliberately bounded metadata).

    Execution is claim/lease based, the same model as Operation
    (app/models/operations.py; app/services/quality_job_runner.py runs it):
    queued -> a worker claims it atomically (status=running, attempt_count += 1,
    lease_expires_at = now + lease) -> heartbeats the lease while it works ->
    succeeded/failed. If the worker's process dies mid-run, the lease simply
    lapses and ANY worker's next poll reclaims the row; attempt_count caps that
    so a job that keeps killing its worker ends `stale`, not retried forever.
    """

    __tablename__ = "dc_quality_execution_jobs"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    # CASCADE, not the FK's implicit RESTRICT default: pipeline_id is NOT NULL
    # here (unlike QualityResult's), so SET NULL isn't an option — a queued/
    # running job has no meaning once its pipeline is gone, so it should be
    # removed along with it rather than block the delete.
    pipeline_id: Mapped[str] = mapped_column(
        ForeignKey("dc_pipelines.id", ondelete="CASCADE"), index=True
    )
    quality_policy_id: Mapped[str] = mapped_column(ForeignKey("dc_quality_policies.id"))
    status: Mapped[str] = mapped_column(
        String(50), default="queued", comment="queued | running | succeeded | failed | stale"
    )
    record_count: Mapped[int] = mapped_column(default=0)
    result_id: Mapped[str | None] = mapped_column(ForeignKey("dc_quality_results.id"))
    error_message: Mapped[str | None] = mapped_column(Text)
    # BUG-11 — see app/models/operations.py's identical columns for what
    # these carry (app/services/safe_error.py classifies both runners'
    # failures the same way).
    error_code: Mapped[str | None] = mapped_column(String(100))
    retryable: Mapped[bool | None] = mapped_column()
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Claim/lease (see class docstring). NULL lease on a `running` row only happens for a row
    # claimed before this existed; it is treated as already lapsed.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    # GAP-13: the staged batch is stored as `chunk_count` chunks (QualityExecutionJobRecords). `progress` is the worker's
    # checkpoint, committed after every chunk it finishes: {"chunks_completed", "failed", "total", "samples"} — the
    # running totals the final QualityResult is built from. A reclaimed job resumes at chunks_completed with these
    # totals instead of starting over. 0 chunks on a row staged before chunking existed means "one legacy chunk".
    chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    progress: Mapped[dict | None] = mapped_column(JSONB)
    # BUG-06: set on a job created by "resubmit" — the failed/stale job whose input was lost and which this one replaces. The
    # original keeps its own (non-retryable) failure; this is only the link between the two.
    resubmitted_from: Mapped[str | None] = mapped_column(String(64))


class QualityExecutionJobRecords(Base, UuidPk):
    """
    GAP-13: the actual `records` batch a QualityExecutionJob was submitted
    with, stored as ordered CHUNKS (one row each). Previously this only ever lived in the submitting process's
    memory (passed straight into the asyncio.create_task running the job —
    see app/services/quality_job_runner.py's pre-GAP-13 docstring for that
    design's own stated reasoning), so a restart lost it outright and a
    queued/running job could only ever be reaped as failed, never resumed.

    A separate table from QualityExecutionJob itself, not a column on it,
    so: the job's own row (polled repeatedly by the submitting caller for
    status) stays small and fast regardless of batch size; deleting a
    finished job's records is a targeted single-row delete
    (quality_job_runner._delete_records), not a write to the job row a
    concurrent status poll might also be touching; and `expires_at` gives
    this row a bounded lifetime independent of the job row's own lifetime.

    Records are deleted once their job SUCCEEDS (quality_job_runner._execute); a failed or stale job keeps them so it can be
    resumed. `expires_at` (default settings.quality_job_records_ttl_seconds) is a retention bound for those kept-for-resume rows —
    and the periodic sweep (quality_job_runner.cleanup_expired_records, started from the app lifespan) NEVER deletes the input of a
    queued or running job, however old its rows are: a job waiting its turn, or running long, still needs them (BUG-06). Only
    when a job leaves queued/running does its retention clock apply, which is why a job that fails has its expiry extended
    from that moment. The batch
    itself was already size-bounded at submission time
    (settings.quality_job_max_records) — this doesn't introduce a new
    unbounded-growth risk, only somewhere durable for that already-bounded
    payload to live for the short window it's actually needed.
    """

    __tablename__ = "dc_quality_execution_job_records"

    __table_args__ = (UniqueConstraint("job_id", "chunk_index"),)

    job_id: Mapped[str] = mapped_column(ForeignKey("dc_quality_execution_jobs.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    # GAP-13: one row per CHUNK of the batch (settings.quality_job_chunk_records / _chunk_bytes), in order, so a worker
    # streams through a large batch holding one chunk at a time instead of loading it whole.
    chunk_index: Mapped[int] = mapped_column(default=0, server_default="0")
    byte_size: Mapped[int] = mapped_column(default=0, server_default="0")
    records: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QualityStreamStats(Base, UuidPk, TimestampMixin):
    """
    Live, continuously-updated running totals for QualityStreamWorker's
    real-time per-record enforcement (app/services/quality_stream_worker.py)
    — one row per pipeline, incremented for every real record the worker
    evaluates against its live Kafka input topic.

    Deliberately a separate table from QualityResult, not a specialization
    of it: QualityResult is a discrete, point-in-time summary of one
    Execute-button/execute-job run against caller-supplied `records`
    (production batch or hand-typed sample data); this table is a running
    lifetime total for the continuous, real-record enforcement path. The two
    measure genuinely different things — a one-off test against arbitrary
    sample data versus an ongoing stream — so a policy's QualityResult
    history and its pipeline's QualityStreamStats total are never expected
    to reconcile with each other, the same way a Kafka topic's real message
    count has no fixed relationship to how many times someone has manually
    probed similar-looking sample data against the same policy.

    CASCADE on pipeline delete (unlike QualityResult's SET NULL): this row
    only exists to describe one pipeline's *current* live worker, so it has
    no meaning — and nothing left worth keeping — once that pipeline is gone.
    """

    __tablename__ = "dc_quality_stream_stats"
    __table_args__ = (UniqueConstraint("pipeline_id"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    pipeline_id: Mapped[str] = mapped_column(
        ForeignKey("dc_pipelines.id", ondelete="CASCADE"), index=True
    )
    quality_policy_id: Mapped[str | None] = mapped_column(ForeignKey("dc_quality_policies.id"))
    total_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    # Bounded sample of the most recent failing records' quarantine
    # envelopes (same shape as QualityResult.sample_failures, capped at
    # settings.quality_stream_max_sample_failures, oldest dropped first) —
    # lets the UI show *why* this pipeline's live records are failing
    # without this table ever growing into an unbounded blob.
    sample_failures: Mapped[list] = mapped_column(JSONB, default=list)


class QualityStreamOffset(Base, TimestampMixin):
    """NEW-05: how far into each Kafka partition of an input topic a pipeline's quality statistics already reach — the offset of the NEXT record
    that has not been counted. Written in the SAME transaction as the statistics it covers (QualityStreamStats, the failure sample and the
    per-rule failure counts), so a record whose offset is already covered is a replay and is not counted again.

    The worker commits its Kafka offset AFTER it has recorded a record. A crash in between restarts the consumer from the older offset and hands
    the same records over again; without this table every one of them was counted a second time, inflating total/failed counts, repeating the
    failure sample and doubling the per-rule counts on the Quality page. PostgreSQL decides what has been counted; Kafka's committed offset only
    says where to start reading.

    Keyed per PIPELINE as well as topic and partition: two pipelines can read the same input topic, and each counts the records for itself.
    Only the statistics are protected. Messages the worker PUBLISHES (quarantine, prepared, dead letter) are at-least-once by nature — it cannot
    know whether a copy reached Kafka before the crash — so each carries a stable `inai.dedupe_key` for the consumer (see
    app/services/quality_stream_worker.py). A topic that is deleted and recreated starts again at 0, so the worker drops stored offsets beyond a
    topic's end when it starts.
    """

    __tablename__ = "dc_quality_stream_offsets"

    pipeline_id: Mapped[str] = mapped_column(ForeignKey("dc_pipelines.id", ondelete="CASCADE"), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), primary_key=True)
    kafka_partition: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    next_offset: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
