"""Schema models — contract versions and drift approval workflow."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class SchemaVersion(Base, UuidPk):
    """
    A versioned schema contract for a specific domain entity.

    Identified by the 5-tuple: (org_id, domain, source_system, entity, version).
    This is the "approved" baseline that schema drift is measured against.

    Compatibility modes follow the Confluent Schema Registry convention:
        BACKWARD  — new schema can read data written with the previous schema
        FORWARD   — previous schema can read data written with the new schema
        FULL      — both BACKWARD and FORWARD
        NONE      — no compatibility checks
    """

    __tablename__ = "dc_schema_versions"
    __table_args__ = (
        UniqueConstraint("org_id", "domain", "source_system", "entity", "version"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    domain: Mapped[str] = mapped_column(String(100))
    source_system: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50))
    schema_format: Mapped[str] = mapped_column(String(50), default="json_schema")
    schema_definition: Mapped[dict] = mapped_column(JSONB)
    compatibility: Mapped[str] = mapped_column(String(50), default="BACKWARD")
    status: Mapped[str] = mapped_column(String(50), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SchemaInferenceRun(Base, UuidPk):
    """
    A durable record of one POST /schemas/infer run — the rich, observational
    output of inference (per-field confidence, null_only, precision/scale,
    plus the overall confidence and sample size) kept SEPARATE from
    SchemaVersion.schema_definition, which represents an approved contract.

    This is deliberate: confidence/null_only are properties of *how sure we
    are given this sample*, not properties of the contract itself, and must
    never leak into an approved SchemaVersion — see
    schema_service.strip_inference_metadata(), used whenever a caller's
    submitted schema_definition might otherwise carry them over verbatim.
    """

    __tablename__ = "dc_schema_inference_runs"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    sample_size: Mapped[int] = mapped_column(default=0)
    confidence: Mapped[float] = mapped_column(default=0.0)
    fields: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchemaDriftApproval(Base, UuidPk, TimestampMixin):
    """
    A drift approval ticket raised when a pipeline observes a schema that
    deviates from the approved SchemaVersion contract.

    Persisted for operational traceability — every field needed to answer
    "what drifted, when, on which pipeline/run, what was decided, when, and
    what schema version (if any) resulted" lives on this row:
      - detected_fingerprint / current_fingerprint: deterministic content
        hashes (see app/services/schema_fingerprint.py) of the observed vs.
        approved schema at the moment drift was detected, so this row stays
        a faithful record of that comparison even if the approved
        SchemaVersion is edited later.
      - detected_at: when the drift was detected — kept distinct from
        `created_at` (this row's insertion time; normally the same instant)
        since "detected" is the domain-meaningful moment.
      - pipeline_id / run_id: which pipeline (and, if known, which specific
        run) observed the drift.
      - status: lifecycle — pending | approved | rejected.
      - decision / reviewed_at ("decision timestamp"): the reviewer's actual
        decision and when they made it (reviewed_by/review_comment carry the
        rest of the review).
      - resulting_schema_version_id: the SchemaVersion (if any) the decision
        produced.

    `schema_version_id`, `pipeline_id`, and `resulting_schema_version_id` are
    deliberately NOT database foreign keys: SchemaVersion and Pipeline CRUD
    are still memory-store-backed in this codebase (see schema_service.py /
    pipeline_service.py), so a Postgres FK to either table could never
    resolve against an id that only exists in memory — the same reason
    SchemaInferenceRun leaves its own cross-references unconstrained. (This
    table's schema_version_id/pipeline_id FKs previously existed as dead
    weight too, since this whole table was memory-store-backed until now and
    so never actually inserted a row through them.)

    Lifecycle: pending → approved | rejected

    Drift events are classified by schema_drift.compare_schema_fields() and
    may trigger different actions (allow_and_notify, hold_for_approval,
    block_deployment, quarantine, etc.) depending on severity.
    """

    __tablename__ = "dc_schema_drift_approvals"

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    schema_version_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    pipeline_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), index=True)
    run_id: Mapped[str | None] = mapped_column(String(255))
    detected_fingerprint: Mapped[str | None] = mapped_column(String(64))
    current_fingerprint: Mapped[str | None] = mapped_column(String(64))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    drift_events: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        comment="Classified drift events from schema_drift.compare_schema_fields()",
    )
    observed_schema: Mapped[dict] = mapped_column(
        JSONB,
        comment="The schema observed in the live data stream",
    )
    status: Mapped[str] = mapped_column(String(50), default="pending")
    decision: Mapped[str | None] = mapped_column(String(50), comment="The reviewer's decision: approved | rejected")
    resulting_schema_version_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    requested_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_comment: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
