"""Response/request contracts for the dbt, FlinkSQL and Data Quality proxies (WS-FE-014).

The proxy routers (app/api/dbt_proxy.py, flink_proxy.py, dq_proxy.py) forward
the upstream service's bytes untouched — they return a ``Response`` object, so
FastAPI never validates or re-serialises the body against these models. The
models exist so the workspace's own OpenAPI document describes what the SPA
actually receives, and the frontend can generate its types instead of
hand-copying each service's contract.

Source of truth for every shape here is the upstream service's own schema:
  dbt     — pralai-dbt-runner      app/schemas/project.py
  Flink   — pralai-flinksql-runner app/schemas/__init__.py
  DQ      — inaiera-data-quality    app/schemas/__init__.py (+ ApplyService /
            UcsSyncService for the two routes it answers with a bare dict)

Every model allows extra fields (``extra="allow"`` → ``additionalProperties:
true`` in the schema) so an upstream addition is documented as "more may
arrive" rather than being a contract violation. Fields the upstream always
sends are declared without a default so they are *required* in the schema;
fields that may be absent default to ``None``.

Request bodies of the pure pass-through routes are documented with
:func:`json_body` (``openapi_extra``) rather than a typed body parameter: a
typed parameter would make FastAPI validate the request locally, changing who
answers a bad request (us, not the upstream) and the error envelope the SPA
sees. ``json_body`` only touches the OpenAPI document.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProxyModel(BaseModel):
    """Base for proxied upstream shapes: tolerate fields we don't know about."""

    model_config = ConfigDict(extra="allow")


def json_body(model: type[BaseModel], *, required: bool = True) -> dict[str, Any]:
    """``openapi_extra`` documenting a JSON request body without validating it.

    The schema is inlined (these request models are flat — no nested refs), so
    it appears under the operation's ``requestBody`` in the generated client.
    """
    schema = model.model_json_schema(mode="validation")
    schema.pop("$defs", None)
    return {
        "requestBody": {
            "required": required,
            "content": {"application/json": {"schema": schema}},
        },
    }


# ── dbt Runner ──────────────────────────────────────────────────────────────

DbtEngine = Literal["trino", "pg_lake", "clickhouse"]
# What the Runner runs: "dbt" — a dbt project folder; "sql" — one .sql file (project_path); "python" — one .py file (project_path).
DbtJobKind = Literal["dbt", "sql", "python"]
# "cancelling" (DBT-005): a cancel was requested and the run's Kubernetes Job is being terminated; the runner reports
# `cancelled` only once the cluster confirms it. Not terminal. Keep equal to the dbt runner's RunStatus
# (tests/test_proxy_contract.py compares them).
DbtRunStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]
DbtRunTrigger = Literal["manual", "scheduled", "api"]


class DbtProjectRegister(BaseModel):
    """POST /api/v1/dbt/projects/register body (dbt Runner ProjectRegister)."""

    git_url: str
    git_ref: str = "main"
    # Folder (relative to the repo root) that holds the project's dbt_project.yml,
    # for repos with several dbt projects. Omit/empty = repo root or its only project.
    project_path: str | None = None
    name: str | None = None
    kind: DbtJobKind = "dbt"
    # SQL jobs only: run on this Workspace connection (Postgres / pg_lake) instead of the runner's warehouse engine. Checked here:
    # the caller must be allowed to use it, and the job then runs as the caller.
    connection_id: UUID | None = None
    workspace_id: UUID | None = None
    engine: DbtEngine = "trino"
    schedule_cron: str | None = None
    # Run on every new commit of git_ref (watched by Dagster's git sensor), independent of schedule_cron (DG-008).
    git_trigger_enabled: bool = False
    # Names a credential the operator provisioned for the runner (GIT_CREDENTIAL_<REF>); never a token or a URL with one (DBT-009).
    credential_ref: str | None = None
    # Dependents: projects that fail when this one fails (and everything below them). A job link, not a data link.
    dependent_project_ids: list[UUID] = Field(default_factory=list)
    # Upstream projects: this one fails (is not started) when any of them fails.
    upstream_project_ids: list[UUID] = Field(default_factory=list)


class DbtProjectUpdate(BaseModel):
    """PATCH /api/v1/dbt/projects/{id} body (dbt Runner ProjectUpdate)."""

    git_ref: str | None = None
    schedule_cron: str | None = None
    schedule_enabled: bool | None = None
    git_trigger_enabled: bool | None = None
    credential_ref: str | None = None       # "" clears it (DBT-009)
    # Replaces the project's Dependents; [] removes them all, omitted leaves them as they are.
    dependent_project_ids: list[UUID] | None = None
    # Replaces the projects above this one the same way.
    upstream_project_ids: list[UUID] | None = None


class DbtRunTriggerRequest(BaseModel):
    """POST /api/v1/dbt/projects/{id}/run body (dbt Runner RunTriggerRequest)."""

    select: str | None = Field(default=None, description="dbt --select expression; full build if omitted")


class DbtLastRun(ProxyModel):
    """Most recent run of a project, embedded in the project list."""

    id: UUID
    status: str
    trigger: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    models_total: int | None
    models_failed: int | None
    error_message: str | None


class DbtProject(ProxyModel):
    id: UUID
    org_id: str
    workspace_id: UUID | None
    name: str
    # "dbt" from an older runner that does not send it.
    kind: DbtJobKind = "dbt"
    # The Workspace connection a SQL job runs on, if any.
    connection_id: UUID | None = None
    git_url: str
    git_ref: str
    # Folder inside the repo ("" = repo root / the only project).
    project_path: str = ""
    engine: DbtEngine
    schedule_cron: str | None
    schedule_enabled: bool
    # Run on every new commit, independent of the schedule (DG-008). Defaults to false for an older runner that does not send it.
    git_trigger_enabled: bool = False
    credential_ref: str | None = None
    last_synced_sha: str | None
    requested_ref: str | None = None
    created_at: datetime
    updated_at: datetime
    # null when the project has never been built.
    last_run: DbtLastRun | None
    # Dependents (direct links) and the failing projects above this one. Empty lists from an older runner that does not send them.
    dependent_project_ids: list[UUID] = Field(default_factory=list)
    upstream_project_ids: list[UUID] = Field(default_factory=list)
    # Projects whose data this one reads (from dbt's lineage of their latest successful builds).
    data_upstream_project_ids: list[UUID] = Field(default_factory=list)


class DbtRun(ProxyModel):
    id: UUID
    project_id: UUID
    trigger: DbtRunTrigger
    git_sha: str | None
    select: str | None
    status: DbtRunStatus
    models_total: int | None
    models_succeeded: int | None
    models_failed: int | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    created_at: datetime


class DbtRunEvent(ProxyModel):
    id: UUID
    run_id: UUID
    event_type: str
    node_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class DbtLineageEdge(ProxyModel):
    source_table: str
    target_table: str
    relationship_type: str


class DbtLineage(ProxyModel):
    project_id: UUID
    edges: list[DbtLineageEdge]


# ── FlinkSQL Runner ─────────────────────────────────────────────────────────

FlinkSinkEngine = Literal["trino", "pg_lake"]
FlinkJobStatus = Literal[
    "draft", "validating", "validated", "deploying", "running",
    "pausing", "paused", "cancelling", "cancelled", "failed", "completed",
    # Not in the runner's JobStatus literal today, but the SPA has always
    # treated it as a lifecycle state; kept so existing UI comparisons stay
    # type-correct.
    "stopped",
]


class FlinkJobCreate(BaseModel):
    """POST /api/v1/flink/jobs body (FlinkSQL Runner FlinkJobCreate)."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    sql: str = Field(min_length=1)
    parallelism: int = Field(default=1, ge=1, le=512)
    sink_engine: FlinkSinkEngine = "trino"
    workspace_id: UUID | None = None


class FlinkJobUpdate(BaseModel):
    """PATCH /api/v1/flink/jobs/{id} body (FlinkSQL Runner FlinkJobUpdate)."""

    name: str | None = None
    description: str | None = None
    sql: str | None = None
    parallelism: int | None = Field(default=None, ge=1, le=512)
    sink_engine: FlinkSinkEngine | None = None


class FlinkJobSource(ProxyModel):
    """Where a job's SQL was imported from (W-015): typed, runner-owned provenance — never encoded in the description."""
    git_url: str                       # canonical repository URL
    git_ref: str | None = None         # the branch/tag that was actually checked out
    requested_ref: str | None = None   # what the user asked for (None = the repository default)
    sql_path: str | None = None
    commit_sha: str                    # the immutable commit that was imported
    provider: str | None = None
    imported_at: datetime | None = None


class FlinkJob(ProxyModel):
    id: UUID
    org_id: str
    workspace_id: UUID | None
    name: str
    # The user's own text. (Jobs imported before W-015 may still end in a legacy "[flink-git] …" line; see `source` for the real record.)
    description: str | None
    sql: str
    parallelism: int
    sink_engine: FlinkSinkEngine
    job_kind: Literal["transform", "quality"] | None = None
    ucs_fqn: str | None = None
    source: FlinkJobSource | None = None
    status: FlinkJobStatus
    validation_errors: list[str]
    last_deployed_at: datetime | None
    last_stopped_at: datetime | None
    savepoint_path: str | None
    created_at: datetime
    updated_at: datetime | None = None


class FlinkDeployment(ProxyModel):
    """Answer of deploy / stop / cancel (FlinkSQL Runner FlinkDeploymentOut)."""

    id: UUID
    job_id: UUID
    flink_job_id: str | None
    status: str
    trigger: str
    from_savepoint: str | None
    deployed_by_user_id: str | None
    error_message: str | None
    submitted_at: datetime | None
    started_at: datetime | None
    stopped_at: datetime | None
    runtime_ms: int | None
    flink_state: str | None
    created_at: datetime


class FlinkJobLiveStatus(ProxyModel):
    """GET /api/v1/flink/jobs/{id}/status (FlinkSQL Runner JobStatusOut).

    ``flink_job_id`` is the Flink cluster's own JobID — what the Flink Web UI
    keys its pages on, distinct from our job's UUID. Metric sub-objects
    (throughput, kafka_lag, checkpoint, backpressure, …) arrive as extra fields.
    """

    job_id: UUID
    status: FlinkJobStatus
    flink_job_id: str | None
    flink_state: str | None
    runtime_ms: int | None
    num_restarts: int | None


class FlinkValidateResponse(ProxyModel):
    """POST /api/v1/flink/validate (FlinkSQL Runner ValidateResponse)."""

    valid: bool
    errors: list[str]
    statement_count: int | None = None
    detected_sources: list[str] | None = None
    detected_sinks: list[str] | None = None


class FlinkUiOut(BaseModel):
    """GET /api/v1/flink/ui — browser-reachable Flink Web UI base ("" = hidden)."""

    url: str


# ── Data Quality ────────────────────────────────────────────────────────────

DqRuleType = Literal[
    "not_null", "unique", "accepted_values", "range", "regex",
    "freshness", "row_count", "row_count_anomaly", "custom_sql", "custom_python",
]
DqDimension = Literal["nulls", "uniqueness", "validity", "freshness", "volume", "schema", "custom"]
DqSeverity = Literal["CRITICAL", "ERROR", "WARNING", "INFO"]
DqJobType = Literal["dbt_project", "flink_job", "python_job"]
DqEnforcementMode = Literal["blocking", "warn", "async"]


class DqBindingCreate(BaseModel):
    """POST /api/v1/dq/bindings body (DQ JobBindingCreate)."""

    ruleset_id: UUID
    job_type: DqJobType
    job_id: UUID
    target_table: str = Field(min_length=1, max_length=500)
    ucs_fqn: str = Field(min_length=1, max_length=500)
    enforcement_mode: DqEnforcementMode = "warn"
    flink_fail_stop: bool = False


class DqRule(ProxyModel):
    id: UUID
    org_id: str
    name: str
    display_name: str
    description: str | None
    rule_type: DqRuleType
    dimension: DqDimension
    severity: DqSeverity
    parameters: dict[str, Any]
    tags: list[str]
    is_system_rule: bool
    version: int
    privileged: bool
    review_status: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class DqRulesetItem(ProxyModel):
    id: UUID
    ruleset_id: UUID
    rule_id: UUID
    column_name: str
    parameter_overrides: dict[str, Any]
    enabled: bool
    sort_order: int
    rule: DqRule | None = None


class DqRuleset(ProxyModel):
    id: UUID
    org_id: str
    name: str
    description: str | None
    version: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    items: list[DqRulesetItem]


class DqRulesetSummary(ProxyModel):
    id: UUID
    org_id: str
    name: str
    description: str | None
    version: int | None = None
    created_at: datetime
    item_count: int


class DqRuleCapabilityEntry(ProxyModel):
    item_id: str | None = None
    rule_id: str | None = None
    rule_name: str
    rule_type: DqRuleType
    column_name: str
    reason: str | None = None


class DqRulesetCapability(ProxyModel):
    job_type: DqJobType | None = None
    enforcement_mode: DqEnforcementMode | None = None
    ruleset_version: int | None = None
    degraded_reason: str | None = None
    blocking_applicable: bool
    supported: list[DqRuleCapabilityEntry]
    unsupported: list[DqRuleCapabilityEntry]
    degraded: list[DqRuleCapabilityEntry]


class DqBinding(ProxyModel):
    id: UUID
    org_id: str
    ruleset_id: UUID
    job_type: DqJobType
    job_id: UUID
    target_table: str
    ucs_fqn: str
    enforcement_mode: DqEnforcementMode
    created_by: str | None
    created_at: datetime
    ruleset: DqRulesetSummary | None = None
    # DQ-BIND-003: canonical job/dataset resolution. A null resolved_at means
    # the binding must be resolved (POST …/resolve) before apply accepts it.
    flink_fail_stop: bool | None = None
    version: int | None = None
    job_name: str | None = None
    job_kind: str | None = None
    dataset_fqn: str | None = None
    dataset_object_type: str | None = None
    resolution: dict[str, Any] | None = None
    resolved_at: datetime | None = None


class DqRender(ProxyModel):
    """Stack-specific artifacts compiled from a binding's ruleset (DQ RenderOut).

    Only the artifact field(s) matching the binding's job_type are populated.
    """

    binding_id: UUID
    version_id: UUID | None = None
    kind: Literal["standard", "preview", "legacy"] | None = None
    job_type: DqJobType
    target_table: str
    ucs_fqn: str
    enforcement_mode: DqEnforcementMode | None = None
    content_hash: str
    renderer_version: str | None = None
    engine_version: str | None = None
    validation_status: str | None = None
    rendered_at: datetime | None
    dbt_artifact: str | None = None
    flinksql_artifact: str | None = None
    gx_artifact: dict[str, Any] | None = None
    soda_artifact: str | None = None


class DqApplyResult(ProxyModel):
    """POST /api/v1/dq/bindings/{id}/apply.

    200 even when nothing was pushed (``applied: false`` + ``status:
    "not_applicable"``) — read ``applied``, then ``error`` / ``message``.
    422 (refused) and 502 (runner failure) carry the same shape plus
    ``reason`` / ``remediation`` / ``unsupported_rules``.
    """

    applied: bool
    target: DqJobType | None = None
    status: str | None = None
    artifact_version_id: str | None = None
    content_hash: str | None = None
    idempotent_replay: bool | None = None
    project_id: str | None = None
    job_id: str | None = None
    job_name: str | None = None
    target_table: str | None = None
    ucs_fqn: str | None = None
    reason: str | None = None
    message: str | None = None
    error: str | None = None


class DqUcsSyncCheckResult(ProxyModel):
    fqn: str
    check: str
    ok: bool
    detail: str | None = None


class DqUcsSyncReport(ProxyModel):
    """POST /api/v1/dq/sync/ucs.

    Today the DQ service queues registrations durably (outbox) and reports
    ``registrations_queued`` + ``delivery``; the per-check ``checks_*`` /
    ``results`` fields are the older synchronous report, still read by the
    SPA when present.
    """

    bindings: int
    registrations_queued: int | None = None
    delivery: str | None = None
    checks_registered: int | None = None
    checks_failed: int | None = None
    results: list[DqUcsSyncCheckResult] | None = None
    error: str | None = None
    message: str | None = None


class DqValidateRuleResponse(ProxyModel):
    valid: bool
    errors: list[str]
    rendered_dbt_yaml: str | None = None
    rendered_flinksql: str | None = None
