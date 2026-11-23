from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SchemaField(BaseModel):
    """One inferred (or hand-authored) field. `type` is one of the fixed
    inference vocabulary (see app/services/schema_inference.py's module
    docstring): string | integer | number | decimal | boolean | date |
    timestamp | object | array — not enforced as a Literal here since this
    model is also used for hand-authored schema_definition content, which
    may need a type outside that vocabulary.

    `confidence` and `null_only` are inference-observation metadata, never
    part of an approved contract — see schema_service.strip_inference_metadata.
    `fields`/`items` are populated only when `type` is "object"/"array" and
    inference didn't stop at settings.schema_infer_max_depth. `precision`/
    `scale` are populated only when `type` is "decimal".
    """

    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    confidence: float | None = None
    null_only: bool = False
    fields: list["SchemaField"] | None = None
    items: "SchemaField | None" = None
    precision: int | None = None
    scale: int | None = None


class SchemaInferRequest(BaseModel):
    records: list[dict[str, Any]]


class SchemaInferResult(BaseModel):
    fields: list[SchemaField]
    confidence: float
    sample_size: int = 0
    inference_run_id: str | None = None


class SchemaInferenceRun(BaseModel):
    """The persisted record behind `inference_run_id` — kept separate from
    any SchemaVersion; see app/models/schema.py's SchemaInferenceRun."""

    id: str
    org_id: str
    sample_size: int
    confidence: float
    fields: list[SchemaField]
    created_at: datetime | None = None


class SchemaDriftRequest(BaseModel):
    records: list[dict[str, Any]]
    pipeline_id: str | None = None
    run_id: str | None = None


class SchemaDriftResult(BaseModel):
    compatible: bool
    events: list[dict[str, Any]] = Field(default_factory=list)
    approval_required: bool = False
    approval_request_id: str | None = None
    detected_fingerprint: str | None = None
    current_fingerprint: str | None = None
    detected_at: datetime | None = None


class SchemaDriftApprovalCreate(BaseModel):
    schema_version_id: str
    pipeline_id: str | None = None
    run_id: str | None = None
    observed_schema: dict[str, Any]
    drift_events: list[dict[str, Any]] = Field(default_factory=list)
    requested_by: str | None = None


class SchemaDriftApproval(BaseModel):
    """The durable drift event/approval record — see
    app/models/schema.py's SchemaDriftApproval for what each field means and
    why schema_version_id/pipeline_id/resulting_schema_version_id aren't
    database foreign keys."""

    id: str
    org_id: str
    schema_version_id: str
    pipeline_id: str | None = None
    run_id: str | None = None
    detected_fingerprint: str | None = None
    current_fingerprint: str | None = None
    detected_at: datetime | None = None
    observed_schema: dict[str, Any]
    drift_events: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    decision: str | None = None
    resulting_schema_version_id: str | None = None
    requested_by: str | None = None
    reviewed_by: str | None = None
    review_comment: str | None = None
    reviewed_at: datetime | None = None


class SchemaDriftReviewRequest(BaseModel):
    reviewed_by: str
    comment: str | None = None
    resulting_schema_version_id: str | None = None  # only meaningful when approving


class SchemaVersionBase(BaseModel):
    domain: str
    source_system: str
    entity: str
    version: str = "v1"
    schema_format: str = "json_schema"
    schema_definition: dict[str, Any] = Field(default_factory=dict)
    compatibility: str = "BACKWARD"
    status: str = "active"


class SchemaVersionCreate(SchemaVersionBase):
    id: str | None = None


class SchemaVersion(SchemaVersionBase):
    id: str
    org_id: str
    # When it was created (response-only, never client-set).
    created_at: datetime | None = None
