"""Pydantic schemas — same ORMModel / Page / ErrorEnvelope pattern as dbt-runner."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


# ── Enums ──────────────────────────────────────────────────────────────────

RuleType = Literal[
    "not_null", "unique", "accepted_values", "range", "regex",
    "freshness", "row_count", "row_count_anomaly", "custom_sql", "custom_python"
]
Dimension = Literal["nulls", "uniqueness", "validity", "freshness", "volume", "schema", "custom"]
Severity  = Literal["CRITICAL", "ERROR", "WARNING", "INFO"]
JobType   = Literal["dbt_project", "flink_job", "python_job"]
EnforcementMode = Literal["blocking", "warn", "async"]


# ── Rule ───────────────────────────────────────────────────────────────────

_NO_CONTROL = r"^[^\x00-\x1f\x7f]+$"


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=_NO_CONTROL)
    display_name: str = ""
    description: str | None = None
    rule_type: RuleType
    dimension: Dimension
    severity: Severity = "ERROR"
    parameters: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    # DQ-SEC-006: required true for custom_sql (plus dq.custom_sql.write).
    acknowledged_privileged: bool = False


class RuleUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    severity: Severity | None = None
    parameters: dict[str, Any] | None = None
    tags: list[str] | None = None
    acknowledged_privileged: bool = False


class RuleApprove(BaseModel):
    acknowledged_privileged: bool = False


class RuleOut(ORMModel):
    id: UUID
    org_id: str
    name: str
    display_name: str
    description: str | None
    rule_type: RuleType
    dimension: Dimension
    severity: Severity
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


# ── Ruleset ────────────────────────────────────────────────────────────────

class RulesetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=_NO_CONTROL)
    description: str | None = None


class RulesetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class RulesetItemCreate(BaseModel):
    rule_id: UUID
    column_name: str = Field(default="", max_length=255)   # '' = table-level rule
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    sort_order: int = 0


class RulesetItemUpdate(BaseModel):
    column_name: str | None = Field(default=None, max_length=255)
    parameter_overrides: dict[str, Any] | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class RulesetItemOut(ORMModel):
    id: UUID
    ruleset_id: UUID
    rule_id: UUID
    column_name: str
    parameter_overrides: dict[str, Any]
    enabled: bool
    sort_order: int
    rule: RuleOut | None = None


class RulesetOut(ORMModel):
    id: UUID
    org_id: str
    name: str
    description: str | None
    version: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    items: list[RulesetItemOut] = Field(default_factory=list)


class RulesetSummary(ORMModel):
    id: UUID
    org_id: str
    name: str
    description: str | None
    version: int = 1
    created_at: datetime
    item_count: int = 0


# ── Job Binding ────────────────────────────────────────────────────────────

class JobBindingCreate(BaseModel):
    ruleset_id: UUID
    job_type: JobType
    job_id: UUID
    target_table: str = Field(min_length=1, max_length=500)
    ucs_fqn: str = Field(min_length=1, max_length=500,
                         description="UCS FQN this binding reports results under")
    enforcement_mode: EnforcementMode = "warn"
    # Explicit opt-in to the FlinkSQL gate's fail-stop action (blocking only).
    flink_fail_stop: bool = False


class JobBindingOut(ORMModel):
    id: UUID
    org_id: str
    ruleset_id: UUID
    job_type: JobType
    job_id: UUID
    target_table: str
    ucs_fqn: str
    enforcement_mode: EnforcementMode
    flink_fail_stop: bool = False
    version: int = 1
    job_name: str | None = None
    job_kind: str | None = None
    dataset_fqn: str | None = None
    dataset_object_type: str | None = None
    resolution: dict | None = None
    resolved_at: datetime | None = None
    created_by: str | None
    created_at: datetime
    ruleset: RulesetSummary | None = None


# ── Capability (DQ-MODEL-002 / DQ-CAP-007) ────────────────────────────────

class RuleCapabilityEntry(BaseModel):
    item_id: str | None = None
    rule_id: str | None = None
    rule_name: str
    rule_type: RuleType
    column_name: str
    reason: str | None = None


class RulesetCapability(BaseModel):
    job_type: JobType | None = None
    enforcement_mode: EnforcementMode | None = None
    ruleset_version: int | None = None
    mode_semantics: dict[str, Any] | None = None
    degraded_reason: str | None = None
    blocking_applicable: bool = True
    supported: list[RuleCapabilityEntry] = Field(default_factory=list)
    unsupported: list[RuleCapabilityEntry] = Field(default_factory=list)
    degraded: list[RuleCapabilityEntry] = Field(default_factory=list)


# ── Compiled artifacts (DQ-ART-004) ────────────────────────────────────────

class RenderOverrides(BaseModel):
    """Typed, request-specific compile overrides (FlinkSQL only). Any
    override makes the result a kind='preview' artifact."""
    window_minutes: int | None = Field(default=None, ge=1, le=1440)
    sample_limit: int | None = Field(default=None, ge=0, le=4)


class RenderOut(BaseModel):
    binding_id: UUID
    version_id: UUID
    kind: Literal["standard", "preview", "legacy"]
    job_type: JobType
    target_table: str
    target_model: str | None = None
    ucs_fqn: str
    enforcement_mode: EnforcementMode
    on_failure: str | None = None
    content_hash: str
    hash_version: int | None = None
    renderer_version: str
    engine_version: str
    supported_engine_versions: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    contract_version: str | None = None
    ruleset_id: UUID | None = None
    ruleset_version: int | None = None
    binding_version: int | None = None
    rule_versions: dict[str, int] = Field(default_factory=dict)
    source_plan_hash: str | None = None
    normalized_sql_hash: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    validation_status: str | None = None
    validation_diagnostics: list[dict] = Field(default_factory=list)
    capability: dict[str, Any] = Field(default_factory=dict)
    rendered_at: datetime | None
    dbt_artifact: str | None = None
    flinksql_artifact: str | None = None
    gx_artifact: dict | None = None
    soda_artifact: str | None = None


# ── Validate (internal, scoped service JWT) ────────────────────────────────

class ValidateRuleRequest(BaseModel):
    rule_type: RuleType
    dimension: Dimension
    severity: Severity = "ERROR"
    parameters: dict[str, Any] = Field(default_factory=dict)
    column_name: str = ""
    target_engine: Literal["trino", "pg_lake"] = "trino"


class ValidateRuleResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    rendered_dbt_yaml: str | None = None
    rendered_flinksql: str | None = None
