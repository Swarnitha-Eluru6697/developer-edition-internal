"""Pipeline Pydantic schemas with field-level validation."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.error_policies import ALL_POLICIES
from app.services.quality_mode import QUALITY_MODES

VALID_ZONES = {"raw", "prepared", "curated", "quarantine", "meta"}
VALID_MODES = {"cdc", "snapshot", "batch", "api", "stream", "validation", "replay"}

# GAP-04: how a connector reacts to a bad record, instead of the previous
# unconditional, hardcoded errors.tolerance=all (see
# app/services/pipeline_config.py's error_handling_connect_config).
#   FAIL_FAST         — errors.tolerance=none: the task fails immediately on
#                       the first error. Right for an integration where a
#                       bad record means something is fundamentally wrong
#                       and continuing would make it worse.
#   SKIP_AND_LOG      — errors.tolerance=all with error logging and NO
#                       dead-letter queue: the bad record is skipped, logged,
#                       and not kept anywhere. The honest choice for a SOURCE
#                       connector, which can't have a DLQ.
#   DLQ_AND_CONTINUE  — errors.tolerance=all: a bad record is logged and
#                       routed to this pipeline's own connector-DLQ topic
#                       (see app/services/topic_naming.py); the task keeps
#                       running. SINK connectors only.
#   THRESHOLD         — same errors.tolerance=all as DLQ_AND_CONTINUE at the
#                       Kafka Connect config level (Connect itself has no
#                       concept of "N errors per window"); the runtime
#                       reconciler separately counts this pipeline's
#                       connector-DLQ topic volume every tick and pauses it
#                       once error_threshold_max_errors is exceeded within
#                       error_threshold_window_seconds — see
#                       app/services/error_threshold.py. SINK connectors only.
# Which of these a pipeline may use depends on its connector: see
# app/core/error_policies.py (and pipeline_spec_validator._validate_capabilities).
VALID_ERROR_HANDLING_POLICIES = set(ALL_POLICIES)
DEFAULT_ERROR_THRESHOLD_MAX_ERRORS = 100
DEFAULT_ERROR_THRESHOLD_WINDOW_SECONDS = 300  # 5 minutes


class PipelineBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    template_id: str
    connection_profile_id: str
    transform_policy_id: str | None = None
    schema_version_id: str | None = None
    quality_policy_id: str | None = None
    # NEW-01: how the quality policy is applied. OBSERVE (default): the worker counts and copies failures to quarantine, and the data topic
    # still carries every record. ENFORCE: passing records go to the prepared topic and failures to quarantine; consumers read the prepared
    # topic. See app/services/quality_mode.py.
    quality_mode: str = "OBSERVE"
    # Null = no pipeline-level override; falls back to the org's domain-default
    # TopicPolicy, then platform defaults — see app/services/topic_policy.py.
    topic_policy_id: str | None = None

    # Topic-naming required fields
    zone: str
    domain: str
    source_system: str
    entity: str
    mode: str
    version: str = "v1"

    connector_config: dict[str, Any] = Field(default_factory=dict)
    runtime_pool_id: str | None = None
    status: str = "draft"

    # None = "not chosen": resolved to the connector's own default (sink -> DLQ_AND_CONTINUE, source -> SKIP_AND_LOG)
    # when the pipeline is created — see app/core/error_policies.py.
    error_handling_policy: str | None = None
    # Only meaningful (and only ever set) when error_handling_policy is
    # THRESHOLD — see the model_validator below for how these two get
    # defaulted to DEFAULT_ERROR_THRESHOLD_MAX_ERRORS/WINDOW_SECONDS when
    # THRESHOLD is chosen without specifying them explicitly.
    error_threshold_max_errors: int | None = Field(default=None, gt=0)
    error_threshold_window_seconds: int | None = Field(default=None, gt=0)

    @field_validator("quality_mode")
    @classmethod
    def validate_quality_mode(cls, v: str) -> str:
        if v not in QUALITY_MODES:
            raise ValueError(f"quality_mode must be one of {sorted(QUALITY_MODES)}")
        return v

    @field_validator("zone")
    @classmethod
    def validate_zone(cls, v: str) -> str:
        if v not in VALID_ZONES:
            raise ValueError(f"zone must be one of {sorted(VALID_ZONES)}")
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"v[0-9]+", v):
            raise ValueError("version must match pattern v<number>, e.g. v1, v2")
        return v

    @field_validator("error_handling_policy")
    @classmethod
    def validate_error_handling_policy(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ERROR_HANDLING_POLICIES:
            raise ValueError(f"error_handling_policy must be one of {sorted(VALID_ERROR_HANDLING_POLICIES)}")
        return v

    @model_validator(mode="after")
    def default_threshold_params(self) -> "PipelineBase":
        # THRESHOLD chosen without explicit numbers gets the report's own
        # suggested default (100 errors / 5 minutes) rather than leaving the
        # policy half-configured. update_pipeline (PipelineService)
        # reconstructs a fresh PipelineBase-derived object from the merged
        # (existing row + patch) spec on every edit, so this also correctly
        # re-defaults a pipeline that's switching INTO THRESHOLD via a PATCH
        # that only sets the policy field.
        if self.error_handling_policy == "THRESHOLD":
            if self.error_threshold_max_errors is None:
                self.error_threshold_max_errors = DEFAULT_ERROR_THRESHOLD_MAX_ERRORS
            if self.error_threshold_window_seconds is None:
                self.error_threshold_window_seconds = DEFAULT_ERROR_THRESHOLD_WINDOW_SECONDS
        return self


class PipelineCreate(PipelineBase):
    id: str | None = None


class Pipeline(PipelineBase):
    id: str
    org_id: str
    # When it was created (response-only, never client-set).
    created_at: datetime | None = None
    # None for a sink: it reads existing topics (connector_config.topics) and generates none of its own (GAP-12).
    topic_name: str | None = None
    # BUG-01: the same topic's Gravitino-safe identifier ("__"-joined,
    # e.g. org_1001__raw__erp__sap__customers__cdc__v1) — distinct from
    # topic_name (the real, physical Kafka topic, "-"-joined). Built from the
    # structured TopicParts (BUG-04), never by string replacement, so it is
    # None for a topic name that does not parse as seven fields. See
    # app/services/topic_naming.py's build_catalog_qualified_name.
    # Response-only; never client-set.
    catalog_qualified_name: str | None = None
    connector_name: str
    # NEW-01: where an ENFORCE pipeline writes the records that passed its quality policy — the topic its consumers should read. None
    # for an OBSERVE pipeline, which has no prepared topic. Response-only; never client-set.
    prepared_topic: str | None = None
    generated_config: dict[str, Any] = Field(default_factory=dict)
    # BUG-10: outcome of the best-effort schema auto-introspection run at
    # creation time — see PipelineService.autocreate_schema_from_source.
    # Response-only (never client-set) — None means it was never attempted
    # (a schema_version_id was already supplied explicitly).
    schema_introspection_status: str | None = None
    schema_introspection_message: str | None = None
    # GAP-12: derived from the resolved template's connector_type at
    # create/retarget time — see pipeline_spec_validator.py's
    # _validate_direction. Response-only; never client-set.
    direction: str
    # FE-BUG-07: the reconciler's own desired-vs-observed state (see
    # app/models/pipeline_runtime_state.py) — all four None for a pipeline
    # that's never been deployed/paused/resumed/restarted (no
    # PipelineRuntimeState row exists yet). Response-only; never client-set.
    # `status` above is the mapped, coarser field every existing UI/RBAC
    # check already reads — these are additive, not a replacement for it.
    desired_state: str | None = None
    observed_state: str | None = None
    last_observed_at: datetime | None = None
    runtime_error: str | None = None
    # How the connector writes or reads records ("v1" = every pipeline created before 2026-11-02, "v2" = exact decimals, keyed rows, CDC rows
    # with a schema, upserting sinks): see the Pipeline model. Fixed at creation. Response-only; never client-set.
    record_format: str = "v2"
    # FE-BUG-14: optimistic-concurrency counter for the editable spec — see
    # the Pipeline model's own docstring. Read this back and send it as
    # PipelineUpdate.expected_revision on the next edit.
    revision: int = 1
    # FE-BUG-25: set the instant a deploy/pause/resume/restart is submitted
    # (PipelineService.claim_lifecycle_transition), cleared once `status`
    # settles back to a stable value. The frontend uses this to resume
    # polling the right operation after a page refresh — see this field's
    # own docstring on the Pipeline model. Response-only; never client-set.
    active_operation_id: str | None = None

    model_config = {"from_attributes": True}


class PipelineUpdate(BaseModel):
    """Partial update — only an editable pipeline specification may change
    this way. Deliberately excludes:
      - identity fields (zone/domain/source_system/entity/mode/version):
        these determine the pipeline's topic/connector identity (see the
        Pipeline model's 7-part naming convention) and are immutable once
        set — changing them would silently re-target the pipeline at a
        different Kafka topic/connector while the old one keeps running.
      - runtime state (status, generated_config, topic_name,
        connector_name): owned exclusively by deploy/pause/resume/restart
        and the runtime reconciler, never by an edit to the specification.
      - audit/identity metadata (id, org_id).

    Only fields actually present in the request body are applied — see
    PipelineService.update_pipeline's use of model_dump(exclude_unset=True) —
    so omitting a field leaves it untouched rather than resetting it to a
    schema default, which is what made the old PipelineCreate-based PUT
    dangerous (e.g. status silently resetting to "draft" on every edit).
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    template_id: str | None = None
    connection_profile_id: str | None = None
    transform_policy_id: str | None = None
    schema_version_id: str | None = None
    quality_policy_id: str | None = None
    quality_mode: str | None = None
    topic_policy_id: str | None = None
    connector_config: dict[str, Any] | None = None
    runtime_pool_id: str | None = None
    error_handling_policy: str | None = None
    error_threshold_max_errors: int | None = Field(default=None, gt=0)
    error_threshold_window_seconds: int | None = Field(default=None, gt=0)
    # FE-BUG-14: the `revision` the client last read (see the Pipeline
    # schema's own field) — optional and opt-in, so an existing/internal
    # caller that never sends it keeps today's last-write-wins behavior.
    # When present and stale, update_pipeline rejects the whole request with
    # a 409 rather than applying any of the other fields in this payload.
    expected_revision: int | None = None

    @field_validator("error_handling_policy")
    @classmethod
    def validate_error_handling_policy(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ERROR_HANDLING_POLICIES:
            raise ValueError(f"error_handling_policy must be one of {sorted(VALID_ERROR_HANDLING_POLICIES)}")
        return v

    @field_validator("quality_mode")
    @classmethod
    def validate_quality_mode(cls, v: str | None) -> str | None:
        if v is not None and v not in QUALITY_MODES:
            raise ValueError(f"quality_mode must be one of {sorted(QUALITY_MODES)}")
        return v


class PipelineStatus(BaseModel):
    pipeline_id: str
    status: str
    connector_name: str
    topic_name: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)


# FE-BUG-24: backs the "Ready to deploy" review screen the UI shows before
# Deploy is ever clicked — see PipelineService.check_deploy_readiness. `ok`
# is False, and `reason` set, only for a failed check, so the UI can render
# a plain checkmark list with an inline explanation just under the items
# that need one.
class DeployReadinessCheck(BaseModel):
    key: str
    label: str
    ok: bool
    reason: str | None = None


class DeployReadiness(BaseModel):
    pipeline_id: str
    topic_name: str | None = None
    ready: bool
    checks: list[DeployReadinessCheck]


# FE-BUG-08: the frontend's "New pipeline" wizard used to have no way to show
# the real topic name before Create was clicked — either it silently showed
# nothing, or (the bug this closes) it would have had to reconstruct the
# name itself from zone/domain/source_system/entity/mode/version, which is
# exactly the kind of independent frontend/backend naming logic that let
# BUG-01's wrong separator go unnoticed on the UI side too. This lets the
# wizard ask the backend for the real, canonical name — computed by the
# exact same app/services/topic_naming.py formula deploy-time config
# generation uses — as the identity fields are filled in, instead of
# guessing.
class TopicNamePreview(BaseModel):
    topic_name: str
