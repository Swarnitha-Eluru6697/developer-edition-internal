"""All SQLAlchemy models for the FlinkSQL Runner service."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True,
                         default=uuid.uuid4, server_default=func.gen_random_uuid())


class FlinkJob(Base):
    __tablename__ = "flink_jobs"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_flink_job_org_name"),
        Index("ix_flink_jobs_org", "org_id"),
        Index("ix_flink_jobs_workspace", "workspace_id"),
        CheckConstraint("status IN ('draft','validating','validated','deploying','running',"
                        "'pausing','paused','cancelling','cancelled','failed','completed')",
                        name="ck_flink_job_status"),
        CheckConstraint("job_kind IN ('transform', 'quality')", name="ck_flink_job_kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by_user_id: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    parallelism: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    consumer_group_id: Mapped[str | None] = mapped_column(String(255))
    sink_engine: Mapped[str] = mapped_column(String(20), nullable=False, default="trino")
    # Quality jobs run parallel to a transform job and emit violation records
    # that the quality_consumer forwards to UCS.
    job_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="transform")
    # UCS dataset FQN results are reported under. Only set for job_kind='quality'.
    ucs_fqn: Mapped[str | None] = mapped_column(String(500))
    # X-004: where `sql` came from when it was imported from Git: {git_url (no credentials), git_ref, sql_path,
    # commit_sha, provider}. NULL for SQL typed in by hand; cleared when the SQL is edited without a new source.
    source: Mapped[dict | None] = mapped_column(JSONB)
    # X-004: for a QUALITY job, the DQ artifact its SQL was generated from {artifact_version_id, binding_id, content_hash,
    # renderer_version, ruleset_id, ruleset_version, manifest_sha256}, set only by the Data Quality service. NULL = not
    # recorded; cleared when the SQL is edited without a new record.
    dq_artifacts: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    validation_errors: Mapped[list] = mapped_column(ARRAY(Text), nullable=False, default=list)
    last_deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    savepoint_path: Mapped[str | None] = mapped_column(String(1000))
    # RUN-FLINK-013: which deployment generation / SQL artifact the current
    # savepoint_path belongs to — a savepoint is only restored into the
    # exact SQL artifact (hash) that produced it.
    savepoint_generation: Mapped[int | None] = mapped_column(Integer)
    savepoint_sql_hash: Mapped[str | None] = mapped_column(String(64))
    # RUN-FLINK-013: monotonically increasing deploy counter — each deploy
    # claims the next value atomically (FlinkDeployment.generation).
    generation_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                    server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deployments = relationship("FlinkDeployment", back_populates="job", cascade="all, delete-orphan")
    metric_snapshots = relationship("FlinkMetricSnapshot", back_populates="job", cascade="all, delete-orphan")


class FlinkDeployment(Base):
    """One deploy or stop attempt for a job — its own id is that attempt's
    "generation" (RUN-FLINK-013): a stable identifier a reconciler can use
    to resume this exact attempt after a restart, distinct from any other
    attempt for the same job. parallelism/stop_savepoint_dir/stop_drain
    persist the desired state a resumed attempt needs to redo its work
    identically to how it was originally requested — previously these
    lived only as local variables in the API request that started the
    background task, so a restart lost them along with the task itself.

    idempotency_key/stop_idempotency_key (RUN-FLINK-014) let a client
    safely retry a deploy/stop call: a retry carrying the same key returns
    the exact row the first attempt created/updated instead of racing a
    second Flink submission. The partial unique indexes below are the
    actual enforcement — a DB uniqueness constraint, not just the
    optimistic status-guarded UPDATE in job_service.deploy()/stop() — so
    at most one deployment can ever be "submitting" (the active
    generation) for a given job, and a given idempotency key can only
    ever be attached to one deployment per job."""
    __tablename__ = "flink_deployments"
    __table_args__ = (
        Index("ix_flink_dep_job", "job_id"),
        Index("ix_flink_dep_flink_id", "flink_job_id"),
        CheckConstraint("status IN ('submitting','running','paused','cancelled','failed','completed')",
                        name="ck_flink_dep_status"),
        Index("uq_flink_dep_active_generation", "job_id", unique=True,
              postgresql_where=text("status = 'submitting'")),
        Index("uq_flink_dep_idempotency_key", "job_id", "idempotency_key", unique=True,
              postgresql_where=text("idempotency_key IS NOT NULL")),
        Index("uq_flink_dep_stop_idempotency_key", "job_id", "stop_idempotency_key", unique=True,
              postgresql_where=text("stop_idempotency_key IS NOT NULL")),
        Index("uq_flink_dep_job_generation", "job_id", "generation", unique=True,
              postgresql_where=text("generation IS NOT NULL")),
        Index("ix_flink_dep_lease", "status", "lease_expires_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("flink_jobs.id", ondelete="CASCADE"), nullable=False)
    flink_job_id: Mapped[str | None] = mapped_column(String(64))
    gateway_session_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitting")
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    from_savepoint: Mapped[str | None] = mapped_column(String(1000))
    parallelism: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    stop_savepoint_dir: Mapped[str | None] = mapped_column(String(1000))
    stop_drain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    stop_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    # RUN-FLINK-014: sha256 of the request a key was first used with — a
    # retry with the same key but a different payload is rejected (422).
    idempotency_request_hash: Mapped[str | None] = mapped_column(String(64))
    stop_idempotency_request_hash: Mapped[str | None] = mapped_column(String(64))
    # RUN-FLINK-013: generation = this job's Nth deploy; sql_hash = sha256
    # of the SQL artifact it submits.
    generation: Mapped[int | None] = mapped_column(Integer)
    sql_hash: Mapped[str | None] = mapped_column(String(64))
    # X-004: what this deployment executes, frozen when it was created (app/services/execution_manifest.py): the SQL
    # text, name, engine, parallelism, restore savepoint, subject, Git source, runtime versions. The submission reads
    # only this, never the mutable job row; a trigger (migration 0008) refuses to change it. NULL only for a
    # deployment created by an older runner version.
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    # RUN-FLINK-013: everything needed to re-find a submission after a
    # crash, persisted BEFORE/AS it reaches Flink: the unique pipeline.name
    # the job is submitted under, the Gateway operation handle, when the
    # submission started, and the stop-with-savepoint trigger id.
    submission_tag: Mapped[str | None] = mapped_column(String(255))
    gateway_operation_handle: Mapped[str | None] = mapped_column(String(255))
    submit_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_trigger_id: Mapped[str | None] = mapped_column(String(255))
    # RUN-FLINK-013: ownership lease — the process driving this deployment
    # holds it and heartbeats; others may act only once it has expired.
    owner_id: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # RUN-FLINK-015: the Kafka consumer group(s) the submitted SQL actually
    # reads with (parsed from 'properties.group.id'), used for lag.
    consumer_groups: Mapped[list] = mapped_column(JSONB, nullable=False, default=list,
                                                  server_default=text("'[]'::jsonb"))
    deployed_by_user_id: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flink_state: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    job = relationship("FlinkJob", back_populates="deployments")


class FlinkMetricSnapshot(Base):
    __tablename__ = "flink_metric_snapshots"
    __table_args__ = (Index("ix_flink_metric_job_ts", "job_id", "captured_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("flink_jobs.id", ondelete="CASCADE"), nullable=False)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("flink_deployments.id", ondelete="SET NULL"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    records_in_per_sec: Mapped[float | None] = mapped_column(Float)
    records_out_per_sec: Mapped[float | None] = mapped_column(Float)
    bytes_in_per_sec: Mapped[float | None] = mapped_column(Float)
    bytes_out_per_sec: Mapped[float | None] = mapped_column(Float)
    # RUN-FLINK-015: NULL = lag unknown/unavailable — never 0.
    kafka_consumer_lag: Mapped[int | None] = mapped_column(BigInteger)
    # {"consumer_groups": [...], "per_partition": {topic: {partition: lag}}}
    kafka_lag_detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_checkpoint_id: Mapped[int | None] = mapped_column(Integer)
    last_checkpoint_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    last_checkpoint_duration_ms: Mapped[int | None] = mapped_column(Integer)
    checkpoint_status: Mapped[str | None] = mapped_column(String(20))
    checkpoints_completed: Mapped[int | None] = mapped_column(Integer)
    checkpoints_failed: Mapped[int | None] = mapped_column(Integer)
    backpressure_max: Mapped[float | None] = mapped_column(Float)
    backpressure_detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    heap_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    heap_max_bytes: Mapped[int | None] = mapped_column(BigInteger)
    num_restarts: Mapped[int | None] = mapped_column(Integer)
    # now - slowest operator's currentInputWatermark (NULL = no watermark yet)
    watermark_lag_ms: Mapped[int | None] = mapped_column(BigInteger)
    job = relationship("FlinkJob", back_populates="metric_snapshots")


class KafkaSourceTable(Base):
    __tablename__ = "kafka_source_tables"
    __table_args__ = (UniqueConstraint("org_id", "table_name", name="uq_kafka_src_org_name"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kafka_topic: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[str] = mapped_column(String(50), nullable=False, default="json")
    consumer_group: Mapped[str | None] = mapped_column(String(255))
    ddl_sql: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class IcebergSinkTable(Base):
    __tablename__ = "iceberg_sink_tables"
    __table_args__ = (UniqueConstraint("org_id", "table_name", name="uq_iceberg_sink_org_name"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sink_type: Mapped[str] = mapped_column(String(20), nullable=False, default="iceberg")
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    ddl_sql: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class QualityResultOutbox(Base):
    """Durable staging store for everything the quality consumer delivers to
    UCS (DQ-RESULT-009/010/011).

    Written BEFORE the Kafka offset is committed, so nothing polled can be
    lost. `kind`:
      quality_run  a QualityRun (started / completed_pass / completed_fail /
                   completed_warn / unknown / stale) for one check
      gate_event   a blocking binding's gate signal (quarantine / fail_stop)
      dead_letter  a record that violated the shared quality contract —
                   kept (status 'rejected'), never silently dropped

    status: pending (waiting, due at next_attempt_at) -> processing (claimed by a worker) -> delivered |
    rejected (UCS refused it / contract violation) | skipped | parked (gave up after max attempts).

    idempotency_key is SEMANTIC — derived from the contract's aggregation key
    (org, dataset, check, window, run_ref) + event kind — so a Flink replay
    of the same window or a re-poll of the same Kafka message collapses onto
    one row (ON CONFLICT DO NOTHING), and the same key is sent to UCS as the
    Idempotency-Key header on every delivery attempt.
    """
    __tablename__ = "quality_result_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_quality_outbox_idempotency_key"),
        Index("ix_quality_outbox_status", "status"),
        Index("ix_quality_outbox_due", "status", "next_attempt_at"),
        CheckConstraint("status IN ('pending', 'processing', 'delivered', 'rejected', 'skipped', 'parked')",
                        name="ck_quality_outbox_status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="quality_run",
                                      server_default="quality_run")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # FL-004: a worker CLAIMS a row (pending -> processing, with its identity and a lease expiry) in a short
    # transaction, delivers it with no transaction open, then finalizes it in a second one. A worker that dies
    # in between leaves a 'processing' row whose lease lapses, and any worker claims it again.
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # FL-005: a failed delivery is not retried before this time (exponential backoff, persisted), and after
    # quality_outbox_max_attempts the row is 'parked': kept, never retried, until an operator requeues it.
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class QualityCheckHeartbeat(Base):
    """Liveness of one quality check within one deployment (run_ref) —
    DQ-RESULT-011. First window -> 'started' QualityRun; no window for
    longer than the stale threshold -> 'stale' QualityRun."""
    __tablename__ = "quality_check_heartbeats"
    __table_args__ = (
        Index("ix_quality_heartbeat_state_seen", "state", "last_seen_at"),
        CheckConstraint("state IN ('active', 'stale')", name="ck_quality_heartbeat_state"),
    )
    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    ucs_fqn: Mapped[str] = mapped_column(String(500), primary_key=True)
    check_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    binding_id: Mapped[str | None] = mapped_column(String(64))
    rule_name: Mapped[str | None] = mapped_column(String(255))
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_window_end: Mapped[str | None] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class FlinkSavepoint(Base):
    """Every savepoint this runner took (stop-with-savepoint or manual
    trigger), tied to the deployment generation and SQL artifact hash that
    produced it (RUN-FLINK-013) — so a restore can be refused when the SQL
    it would be restored into is a different artifact."""
    __tablename__ = "flink_savepoints"
    __table_args__ = (
        Index("ix_flink_savepoints_job", "job_id", "created_at"),
        UniqueConstraint("job_id", "path", name="uq_flink_savepoint_job_path"),
        CheckConstraint("kind IN ('savepoint', 'checkpoint')", name="ck_flink_savepoint_kind"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("flink_jobs.id", ondelete="CASCADE"), nullable=False)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("flink_deployments.id", ondelete="SET NULL"))
    generation: Mapped[int | None] = mapped_column(Integer)
    sql_hash: Mapped[str | None] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="savepoint")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class KafkaPreviewAudit(Base):
    """Durable audit record of every Kafka catalog access decision —
    topic list, schema lookup and raw-message preview, allowed or denied
    (RUN-FLINK-016)."""
    __tablename__ = "kafka_preview_audit"
    __table_args__ = (
        Index("ix_kafka_preview_audit_org_ts", "org_id", "created_at"),
        CheckConstraint("action IN ('list_topics', 'schema', 'preview')", name="ck_kafka_preview_audit_action"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(500))
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    message_count: Mapped[int | None] = mapped_column(Integer)
    bytes_returned: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
