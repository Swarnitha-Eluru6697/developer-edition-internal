"""FlinkSQL Runner schemas."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


SinkEngine = Literal["trino", "pg_lake"]
JobStatus = Literal["draft","validating","validated","deploying","running",
                    "pausing","paused","cancelling","cancelled","failed","completed"]


# ── Jobs ─────────────────────────────────────────────────────────────────

class GitSource(BaseModel):
    """Where a job's SQL was imported from (X-004): recorded as evidence, never used to fetch anything. The URL is
    stored without any user info; the commit must be a full 40-character SHA."""
    git_url: str = Field(min_length=1, max_length=2000)
    git_ref: str = Field(default="main", max_length=255)
    sql_path: str = Field(default="", max_length=1000)
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    provider: str | None = Field(default=None, max_length=50)
    # W-015: what the user asked for (None = the repository default; `git_ref` is then the branch actually checked out) and when the
    # import happened. Both optional so a source recorded before they existed still validates.
    requested_ref: str | None = Field(default=None, max_length=255)
    imported_at: datetime | None = None


class DqArtifactsRecord(BaseModel):
    """Which DQ artifact a QUALITY job's SQL was generated from (X-004). Only the Data Quality service may set it (a user
    cannot claim a job comes from a DQ artifact): it is recorded as evidence and frozen into every deployment's manifest."""
    model_config = ConfigDict(extra="forbid")
    artifact_version_id: UUID
    binding_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: str = Field(min_length=1, max_length=50)
    ruleset_id: UUID | None = None
    ruleset_version: int | None = Field(default=None, ge=1)
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FlinkJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    sql: str = Field(min_length=1, description="Full FlinkSQL: CREATE TABLE sources + sinks + INSERT INTO")
    parallelism: int = Field(default=1, ge=1, le=512)
    sink_engine: SinkEngine = "trino"
    # A grouping label, not an ownership boundary (the registry is org-shared). Accepted only when the Workspace service
    # vouched for it in the delegated token (FL-003); external CI omits it.
    workspace_id: UUID | None = None
    # 'quality' jobs are submitted by the DQ Service and run parallel to a
    # transform job; their violations are forwarded to UCS under ucs_fqn.
    job_kind: Literal["transform", "quality"] = "transform"
    ucs_fqn: str | None = Field(
        default=None,
        description="UCS FQN for quality result reporting (required when job_kind='quality')",
    )
    source: GitSource | None = Field(default=None, description="Git provenance of `sql`, when imported from Git")
    # X-004: the DQ artifact a quality job's SQL was generated from; accepted only from the Data Quality service.
    dq_artifacts: DqArtifactsRecord | None = None


class FlinkJobUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    sql: str | None = None
    parallelism: int | None = Field(default=None, ge=1, le=512)
    sink_engine: SinkEngine | None = None
    # Provenance of the NEW sql. Editing `sql` without it clears the job's recorded source (it no longer describes the SQL).
    source: GitSource | None = None
    # The DQ artifact the NEW sql was generated from (DQ only). Editing `sql` without it clears the job's record.
    dq_artifacts: DqArtifactsRecord | None = None


class FlinkJobOut(BaseModel):
    id: UUID
    org_id: str
    workspace_id: UUID | None
    name: str
    description: str | None
    sql: str
    parallelism: int
    sink_engine: SinkEngine
    job_kind: Literal["transform", "quality"] = "transform"
    ucs_fqn: str | None = None
    source: dict | None = None
    dq_artifacts: dict | None = None
    status: JobStatus
    validation_errors: list[str]
    last_deployed_at: datetime | None
    last_stopped_at: datetime | None
    savepoint_path: str | None
    consumer_group_id: str | None = None
    generation_counter: int = 0
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


# ── Deploy / Control ──────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    from_savepoint: str | None = None
    parallelism: int | None = Field(default=None, ge=1, le=512)
    idempotency_key: str | None = Field(
        default=None, max_length=255,
        description="Client-supplied key — retrying a deploy call with the "
                    "same key returns the original deployment instead of "
                    "risking a second Flink submission (RUN-FLINK-014)",
    )


class StopRequest(BaseModel):
    """Graceful stop with savepoint (preferred — allows clean resume)."""
    drain: bool = True                    # wait for all in-flight records
    savepoint_path: str | None = None     # custom savepoint target; defaults to config
    idempotency_key: str | None = Field(
        default=None, max_length=255,
        description="Client-supplied key — retrying a stop call with the "
                    "same key returns the original deployment instead of "
                    "re-validating job state (RUN-FLINK-014)",
    )


class DeploymentManifestOut(BaseModel):
    """The audit record of what a deployment executed (X-004). Contains no credential."""
    id: UUID
    job_id: UUID
    generation: int | None = None
    sql_hash: str | None = None
    manifest_sha256: str | None = None
    manifest: dict | None = None
    model_config = {"from_attributes": True}


class FlinkDeploymentOut(BaseModel):
    id: UUID
    job_id: UUID
    flink_job_id: str | None
    status: str
    trigger: str
    from_savepoint: str | None
    deployed_by_user_id: str | None
    error_message: str | None
    idempotency_key: str | None = None
    stop_idempotency_key: str | None = None
    generation: int | None = None
    sql_hash: str | None = None
    submission_tag: str | None = None
    consumer_groups: list[str] = Field(default_factory=list)
    submitted_at: datetime | None
    started_at: datetime | None
    stopped_at: datetime | None
    runtime_ms: int | None
    flink_state: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


# ── Validate (internal) ───────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    sql: str
    workspace_id: UUID | None = None


class SqlPlanColumn(BaseModel):
    name: str
    type: str


class SqlPlanTable(BaseModel):
    """A CREATE TABLE this job's SQL declares, structurally parsed (DQ-FLINK-008)
    — see app.services.sql_plan_service.extract_create_tables."""
    name: str
    columns: list[SqlPlanColumn] = Field(default_factory=list)
    options: dict[str, str] = Field(default_factory=dict)


class PlanQualityContract(BaseModel):
    version: str
    topics: dict[str, str] = Field(default_factory=dict)


class StructuredJobPlan(BaseModel):
    """DQ-FLINK-008 structured contract DQ compiles quality jobs from:
    the tables (columns/types/options), the Flink engine version, a hash of
    the comment/whitespace-normalized SQL, and the quality contract this
    runner's consumer implements."""
    plan_version: str = "1"
    engine: str = "flink"
    engine_version: str
    normalized_sql_hash: str
    sources: list[SqlPlanTable] = Field(default_factory=list)
    sinks: list[SqlPlanTable] = Field(default_factory=list)
    unresolved_tables: list[str] = Field(default_factory=list)
    quality_contract: PlanQualityContract


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    statement_count: int = 0
    # Table NAMES only, via a crude regex — kept for existing callers.
    detected_sources: list[str] = Field(default_factory=list)
    detected_sinks: list[str] = Field(default_factory=list)
    # DQ-FLINK-008: the structured contract — full column/options breakdown
    # per table, correctly distinguishing tables read from (sources) versus
    # written to (sinks), plus any table referenced but never CREATEd here
    # (e.g. a catalog-defined table) in unresolved_tables. A caller that
    # needs to reconstruct a source table (rather than just detect its
    # name) should use this, not detected_sources/detected_sinks, and
    # never regex-parse `sql` itself.
    sources: list[SqlPlanTable] = Field(default_factory=list)
    sinks: list[SqlPlanTable] = Field(default_factory=list)
    unresolved_tables: list[str] = Field(default_factory=list)
    plan: StructuredJobPlan | None = None


# ── Metrics / Status ──────────────────────────────────────────────────────

class CheckpointInfo(BaseModel):
    id: int | None
    status: str | None
    size_bytes: int | None
    duration_ms: int | None
    savepoint_path: str | None
    completed: int
    failed: int
    in_progress: int


class KafkaLagInfo(BaseModel):
    """RUN-FLINK-015: total_lag is None when lag is unknown (no declared
    consumer group / REST Proxy unavailable) — never a fabricated 0.
    per_partition is {topic: {partition: lag}}; per_topic sums it."""
    available: bool
    total_lag: int | None
    consumer_groups: list[str] = Field(default_factory=list)
    per_topic: dict[str, int] = Field(default_factory=dict)
    per_partition: dict[str, dict[str, int]] = Field(default_factory=dict)
    reason: str | None = None


class ThroughputMetrics(BaseModel):
    records_in_per_sec: float | None
    records_out_per_sec: float | None
    bytes_in_per_sec: float | None
    bytes_out_per_sec: float | None


class BackpressureInfo(BaseModel):
    max_ratio: float | None
    per_operator: dict[str, float]


class JobStatusOut(BaseModel):
    job_id: UUID
    flink_job_id: str | None
    status: JobStatus
    flink_state: str | None
    runtime_ms: int | None
    num_restarts: int | None
    throughput: ThroughputMetrics
    kafka_lag: KafkaLagInfo | None
    checkpoint: CheckpointInfo | None
    backpressure: BackpressureInfo
    # now - slowest operator's currentInputWatermark; None until a watermark exists
    watermark_lag_ms: int | None
    heap_used_bytes: int | None
    heap_max_bytes: int | None
    last_polled_at: datetime | None
    alerts: list[str]


class MetricHistoryPoint(BaseModel):
    ts: datetime
    records_in_per_sec: float | None
    records_out_per_sec: float | None
    kafka_consumer_lag: int | None
    checkpoint_duration_ms: int | None
    backpressure_max: float | None
    watermark_lag_ms: int | None


class MetricHistoryOut(BaseModel):
    job_id: UUID
    points: list[MetricHistoryPoint]


# ── Kafka catalog ─────────────────────────────────────────────────────────

class KafkaTopicInfo(BaseModel):
    name: str
    partitions: int
    replication_factor: int
    message_count_estimate: int | None
    schema_format: str | None     # json, avro, protobuf, none
    schema_id: int | None


class KafkaMessagePreview(BaseModel):
    partition: int
    offset: int
    key: str | None
    value: dict[str, Any] | str
    timestamp_ms: int


class KafkaTopicPreview(BaseModel):
    topic: str
    messages: list[KafkaMessagePreview]
    schema_inferred: dict[str, Any] | None
    truncated: bool = False          # total-bytes / row cap cut the sample short
    bytes_returned: int = 0


# ── Table catalog (reusable DDL) ──────────────────────────────────────────

class KafkaSourceTableCreate(BaseModel):
    table_name: str = Field(min_length=1, max_length=255)
    kafka_topic: str
    format: str = "json"
    consumer_group: str | None = None
    ddl_sql: str
    schema_json: dict[str, Any] = Field(default_factory=dict)


class KafkaSourceTableOut(BaseModel):
    id: UUID
    org_id: str
    table_name: str
    kafka_topic: str
    format: str
    ddl_sql: str
    schema_json: dict[str, Any]
    created_at: datetime
    model_config = {"from_attributes": True}


class IcebergSinkTableCreate(BaseModel):
    table_name: str
    sink_type: str = "iceberg"
    target_table: str
    ddl_sql: str
    schema_json: dict[str, Any] = Field(default_factory=dict)


class IcebergSinkTableOut(BaseModel):
    id: UUID
    org_id: str
    table_name: str
    sink_type: str
    target_table: str
    ddl_sql: str
    schema_json: dict[str, Any]
    created_at: datetime
    model_config = {"from_attributes": True}


class SqlTemplateOut(BaseModel):
    """Pre-built SQL templates for common patterns."""
    name: str
    description: str
    category: str   # "source", "sink", "transformation", "full_job"
    sql: str
    placeholders: list[str]
