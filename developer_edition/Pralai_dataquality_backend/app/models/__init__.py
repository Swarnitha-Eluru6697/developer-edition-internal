from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, func, text,
    Uuid as SaUuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(SaUuid(), primary_key=True, default=uuid.uuid4)


# ─── Rule ────────────────────────────────────────────────────────────────────

class Rule(Base):
    """A named, parameterized, VERSIONED data quality rule definition.

    `version` increments on every change to anything that affects rendering
    (parameters/severity), and is part of every ArtifactVersion hash.

    DQ-SEC-006: custom_sql rules are `privileged`; they render only while
    `review_status == "approved"` (set by a principal holding
    dq.custom_sql.write with an explicit acknowledgment).
    """
    __tablename__ = "dq_rules"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_dq_rule_org_name"),
        Index("ix_dq_rule_org", "org_id"),
        Index("ix_dq_rule_dimension", "dimension"),
        CheckConstraint(
            "rule_type IN ('not_null','unique','accepted_values','range','regex',"
            "'freshness','row_count','row_count_anomaly','custom_sql','custom_python')",
            name="ck_dq_rule_type",
        ),
        CheckConstraint(
            "dimension IN ('nulls','uniqueness','validity','freshness','volume','schema','custom')",
            name="ck_dq_rule_dimension",
        ),
        CheckConstraint("severity IN ('CRITICAL','ERROR','WARNING','INFO')",
                        name="ck_dq_rule_severity"),
        CheckConstraint("review_status IS NULL OR review_status IN ('pending','approved')",
                        name="ck_dq_rule_review_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)
    dimension: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="ERROR")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    tags: Mapped[list] = mapped_column(ARRAY(String), nullable=False, default=list)
    is_system_rule: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default=text("1"))
    privileged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                             server_default=text("false"))
    review_status: Mapped[str | None] = mapped_column(String(20))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)

    ruleset_items = relationship("RulesetItem", back_populates="rule", cascade="all, delete-orphan")


# ─── Ruleset ─────────────────────────────────────────────────────────────────

class Ruleset(Base):
    """A named collection of rule-column items. `version` increments on any
    change to the ruleset or its items."""
    __tablename__ = "dq_rulesets"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_dq_ruleset_org_name"),
        Index("ix_dq_ruleset_org", "org_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default=text("1"))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)

    items = relationship("RulesetItem", back_populates="ruleset",
                         cascade="all, delete-orphan", order_by="RulesetItem.sort_order")
    bindings = relationship("JobBinding", back_populates="ruleset", cascade="all, delete-orphan")


class RulesetItem(Base):
    __tablename__ = "dq_ruleset_items"
    __table_args__ = (
        UniqueConstraint("ruleset_id", "rule_id", "column_name", name="uq_dq_item_rule_col"),
        Index("ix_dq_item_ruleset", "ruleset_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    ruleset_id: Mapped[uuid.UUID] = mapped_column(
        SaUuid(), ForeignKey("dq_rulesets.id", ondelete="CASCADE"), nullable=False)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        SaUuid(), ForeignKey("dq_rules.id", ondelete="CASCADE"), nullable=False)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    parameter_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ruleset = relationship("Ruleset", back_populates="items")
    rule = relationship("Rule", back_populates="ruleset_items")


# ─── JobBinding ──────────────────────────────────────────────────────────────

class JobBinding(Base):
    """Attaches a ruleset to a dbt project / FlinkSQL job / Python job and a
    UCS dataset.

    DQ-BIND-003: `resolved_at` is set only after the job was resolved through
    its runner (same org, right kind) and the dataset through UCS (exists,
    dq.bind authorised); the canonical identifiers returned by those systems
    are persisted (job_name/job_kind/job_org_id, dataset_fqn,
    dataset_object_type). Bindings created before this existed have
    resolved_at NULL and must be re-resolved (POST /bindings/{id}/resolve)
    before they can be applied.

    enforcement_mode semantics per engine: app.renderers.capability.
    flink_fail_stop: explicit opt-in for the FlinkSQL gate's fail-stop action.
    """
    __tablename__ = "dq_job_bindings"
    __table_args__ = (
        UniqueConstraint("ruleset_id", "job_type", "job_id", "target_table",
                         name="uq_dq_binding_unique"),
        Index("ix_dq_binding_org", "org_id"),
        Index("ix_dq_binding_job", "job_type", "job_id"),
        CheckConstraint("job_type IN ('dbt_project','flink_job','python_job')",
                        name="ck_dq_binding_job_type"),
        CheckConstraint("enforcement_mode IN ('blocking','warn','async')",
                        name="ck_dq_binding_mode"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    ruleset_id: Mapped[uuid.UUID] = mapped_column(
        SaUuid(), ForeignKey("dq_rulesets.id", ondelete="CASCADE"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(20), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(SaUuid(), nullable=False)
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    ucs_fqn: Mapped[str] = mapped_column(String(500), nullable=False)
    enforcement_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="warn")
    flink_fail_stop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                  server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                         server_default=text("1"))
    job_name: Mapped[str | None] = mapped_column(String(255))
    job_kind: Mapped[str | None] = mapped_column(String(40))
    job_org_id: Mapped[str | None] = mapped_column(String(255))
    dataset_fqn: Mapped[str | None] = mapped_column(String(500))
    dataset_object_type: Mapped[str | None] = mapped_column(String(40))
    resolution: Mapped[dict | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)

    ruleset = relationship("Ruleset", back_populates="bindings")
    artifact_versions = relationship("ArtifactVersion", back_populates="binding",
                                     cascade="all, delete-orphan", passive_deletes=True,
                                     order_by="ArtifactVersion.rendered_at.desc()")


# ─── ArtifactVersion ─────────────────────────────────────────────────────────

class ArtifactVersion(Base):
    """An IMMUTABLE, content-addressed compiled artifact (DQ-ART-004).

    Rows are insert-only: a Postgres trigger (migration 0003) rejects every
    UPDATE, and (binding_id, content_hash) is unique for hash_version>=2 rows,
    so the same inputs always map to the same row and different inputs can
    never overwrite one.

    content_hash = sha256 over `hash_inputs` (canonical JSON): ruleset id +
    version, each rule's id + version + effective params, the binding's
    version and target, the source contract (Flink structured-plan hash and
    normalized SQL hash), renderer semantic version, target engine version,
    the quality contract version/topics, render overrides and `kind`.

    kind:
      standard — compiled from the binding's real inputs; the only kind apply
                 will push.
      preview  — compiled with request-specific overrides; hashed with its
                 kind + overrides so it can never collide with or be served
                 as a standard artifact.
      legacy   — rows produced before migration 0003 (old, weaker hash);
                 never reused or applied.
    """
    __tablename__ = "dq_artifact_versions"
    __table_args__ = (
        Index("ix_dq_artifact_binding_hash", "binding_id", "content_hash"),
        Index("uq_dq_artifact_binding_hash_v2", "binding_id", "content_hash", unique=True,
              postgresql_where=text("hash_version IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    binding_id: Mapped[uuid.UUID] = mapped_column(
        SaUuid(), ForeignKey("dq_job_bindings.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(20), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(50), nullable=False)
    dbt_artifact: Mapped[str | None] = mapped_column(Text)
    flinksql_artifact: Mapped[str | None] = mapped_column(Text)
    gx_artifact: Mapped[dict | None] = mapped_column(JSONB)
    soda_artifact: Mapped[str | None] = mapped_column(Text)
    validation_diagnostics: Mapped[list | None] = mapped_column(JSONB)
    rendered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now(), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="standard",
                                      server_default=text("'legacy'"))
    hash_version: Mapped[int | None] = mapped_column(Integer)
    hash_inputs: Mapped[dict | None] = mapped_column(JSONB)
    ruleset_id: Mapped[uuid.UUID | None] = mapped_column(SaUuid())
    ruleset_version: Mapped[int | None] = mapped_column(Integer)
    binding_version: Mapped[int | None] = mapped_column(Integer)
    rule_versions: Mapped[dict | None] = mapped_column(JSONB)
    target_model: Mapped[str | None] = mapped_column(String(500))
    source_plan_hash: Mapped[str | None] = mapped_column(String(64))
    normalized_sql_hash: Mapped[str | None] = mapped_column(String(64))
    contract_version: Mapped[str | None] = mapped_column(String(20))
    overrides: Mapped[dict | None] = mapped_column(JSONB)
    supported_engine_versions: Mapped[dict | None] = mapped_column(JSONB)
    capability: Mapped[dict | None] = mapped_column(JSONB)
    validation_status: Mapped[str | None] = mapped_column(String(20))
    created_by: Mapped[str | None] = mapped_column(String(255))

    binding = relationship("JobBinding", back_populates="artifact_versions")


class ArtifactApplication(Base):
    """One apply attempt of one ArtifactVersion to its runner."""
    __tablename__ = "dq_artifact_applications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_dq_application_idempotency_key"),
        Index("ix_dq_application_binding", "binding_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    binding_id: Mapped[uuid.UUID] = mapped_column(
        SaUuid(), ForeignKey("dq_job_bindings.id", ondelete="CASCADE"), nullable=False)
    artifact_version_id: Mapped[uuid.UUID] = mapped_column(
        SaUuid(), ForeignKey("dq_artifact_versions.id", ondelete="CASCADE"), nullable=False)
    target: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    actor: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)


class OutboxEvent(Base):
    """Transactional outbox for UCS deliveries (audit events, check
    registrations) — written in the same transaction as the change it
    describes; delivered at-least-once by app.services.outbox with an
    Idempotency-Key header."""
    __tablename__ = "dq_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_dq_outbox_idempotency_key"),
        Index("ix_dq_outbox_status_next", "status", "next_attempt_at"),
        CheckConstraint("status IN ('pending','delivered','failed')", name="ck_dq_outbox_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending",
                                        server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                      server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceTokenJti(Base):
    """Single-use record of inbound service-token jtis (replay protection)."""
    __tablename__ = "dq_service_token_jti"

    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
