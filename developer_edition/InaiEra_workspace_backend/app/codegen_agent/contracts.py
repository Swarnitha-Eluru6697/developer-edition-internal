"""Inter-stage data contracts for the codegen harness (design §6).

These Pydantic models are the typed payloads that flow between stages:
GroundingPack -> Plan -> Artifact -> ValidationReport, with Diagnostic as
the normalised failure that drives repair routing (§9). Keeping them as
Pydantic models means every stage boundary is schema-validated in-process,
and the shapes round-trip cleanly into the codegen_messages.validation_detail
JSONB column for audit and replay.
"""
from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# Targets mirror the existing CodegenBackend registry so the harness stays
# aligned with the rest of the service. "python" is static-check-only (§7.5).
Target = Literal["sql", "python", "dbt", "flinksql"]


# --------------------------------------------------------------------------
# Error taxonomy and routing (§9.1)
# --------------------------------------------------------------------------
class ErrorClass(str, enum.Enum):
    """Normalised failure classes. Every validator maps its raw error into one
    of these so the repair controller can route without parsing stderr."""

    # Structural / dialect (route: GENERATE)
    SYNTAX_ERROR = "SYNTAX_ERROR"
    DIALECT_UNSUPPORTED_FN = "DIALECT_UNSUPPORTED_FN"
    LINT_VIOLATION = "LINT_VIOLATION"
    # Schema (route: GROUND for unknown identifiers, GENERATE for ambiguity)
    SCHEMA_UNKNOWN_IDENTIFIER = "SCHEMA_UNKNOWN_IDENTIFIER"
    SCHEMA_AMBIGUOUS_REF = "SCHEMA_AMBIGUOUS_REF"
    # Plan-level (route: PLAN)
    PLAN_TYPE_MISMATCH = "PLAN_TYPE_MISMATCH"
    PLAN_GRAIN_UNDECLARED = "PLAN_GRAIN_UNDECLARED"
    PLAN_COST_RISK = "PLAN_COST_RISK"
    # Policy (route: TERMINAL, or PLAN with masking)
    POLICY_PII = "POLICY_PII"
    POLICY_WRITE_TARGET = "POLICY_WRITE_TARGET"
    # Runtime / engine (route: GENERATE)
    RUNTIME_ERROR = "RUNTIME_ERROR"
    CONNECTOR_OPTION_INVALID = "CONNECTOR_OPTION_INVALID"
    CHANGELOG_MODE_INCOMPATIBLE = "CHANGELOG_MODE_INCOMPATIBLE"
    ASSERTION_FAILED = "ASSERTION_FAILED"
    DATA_DIFF_UNEXPECTED = "DATA_DIFF_UNEXPECTED"
    # Python static-only (§7.5)
    PY_TYPE_ERROR = "PY_TYPE_ERROR"
    PY_PROHIBITED_IMPORT = "PY_PROHIBITED_IMPORT"
    PY_SECURITY_FINDING = "PY_SECURITY_FINDING"
    PY_KAFKA_SCHEMA_MISMATCH = "PY_KAFKA_SCHEMA_MISMATCH"
    PY_KAFKA_TOPIC_UNKNOWN = "PY_KAFKA_TOPIC_UNKNOWN"
    PY_UNDECLARED_WRITE = "PY_UNDECLARED_WRITE"
    # Fallback
    UNKNOWN = "UNKNOWN"


class RepairRoute(str, enum.Enum):
    """The stage a failure is dispatched back to."""

    GROUND = "GROUND"
    PLAN = "PLAN"
    GENERATE = "GENERATE"
    TERMINAL = "TERMINAL"  # no automatic repair; surface diagnosis to user


# Static routing table (§9.1). The repair controller consults this to decide
# where a normalised failure goes.
ROUTE_BY_CLASS: dict[ErrorClass, RepairRoute] = {
    ErrorClass.SYNTAX_ERROR: RepairRoute.GENERATE,
    ErrorClass.DIALECT_UNSUPPORTED_FN: RepairRoute.GENERATE,
    ErrorClass.LINT_VIOLATION: RepairRoute.GENERATE,
    ErrorClass.SCHEMA_UNKNOWN_IDENTIFIER: RepairRoute.GROUND,
    ErrorClass.SCHEMA_AMBIGUOUS_REF: RepairRoute.GENERATE,
    ErrorClass.PLAN_TYPE_MISMATCH: RepairRoute.PLAN,
    ErrorClass.PLAN_GRAIN_UNDECLARED: RepairRoute.PLAN,
    ErrorClass.PLAN_COST_RISK: RepairRoute.PLAN,
    ErrorClass.POLICY_PII: RepairRoute.TERMINAL,
    ErrorClass.POLICY_WRITE_TARGET: RepairRoute.TERMINAL,
    ErrorClass.RUNTIME_ERROR: RepairRoute.GENERATE,
    ErrorClass.CONNECTOR_OPTION_INVALID: RepairRoute.GENERATE,
    ErrorClass.CHANGELOG_MODE_INCOMPATIBLE: RepairRoute.PLAN,
    ErrorClass.ASSERTION_FAILED: RepairRoute.PLAN,
    ErrorClass.DATA_DIFF_UNEXPECTED: RepairRoute.TERMINAL,
    ErrorClass.PY_TYPE_ERROR: RepairRoute.GENERATE,
    ErrorClass.PY_PROHIBITED_IMPORT: RepairRoute.GENERATE,
    ErrorClass.PY_SECURITY_FINDING: RepairRoute.GENERATE,
    ErrorClass.PY_KAFKA_SCHEMA_MISMATCH: RepairRoute.GENERATE,
    ErrorClass.PY_KAFKA_TOPIC_UNKNOWN: RepairRoute.GROUND,
    ErrorClass.PY_UNDECLARED_WRITE: RepairRoute.GENERATE,
    ErrorClass.UNKNOWN: RepairRoute.GENERATE,
}


# --------------------------------------------------------------------------
# Grounding (§5.3 / §6.1)
# --------------------------------------------------------------------------
class ColumnMeta(BaseModel):
    name: str
    type: str | None = None
    nullable: bool | None = None
    tags: list[str] = Field(default_factory=list)


class TableMeta(BaseModel):
    fqn: str
    columns: list[ColumnMeta] = Field(default_factory=list)
    partition_keys: list[str] = Field(default_factory=list)
    description: str | None = None
    retrieval_score: float | None = None


class KafkaTopicMeta(BaseModel):
    """A Kafka topic + its registered schema, for Python/Flink grounding (§5.3)."""

    topic: str
    schema_type: Literal["avro", "protobuf", "json", "unknown"] = "unknown"
    schema_str: str | None = None  # the registered schema document, if any
    subject: str | None = None


class GroundingPack(BaseModel):
    """Immutable for the lifetime of a run (§4.5). Assembled once by the
    grounding stage; re-grounding produces a NEW pack, not a mutation, so the
    prompt prefix stays stable and the gateway's prefix cache stays warm."""

    target: Target
    tables: list[TableMeta] = Field(default_factory=list)
    kafka_topics: list[KafkaTopicMeta] = Field(default_factory=list)
    exemplars: list[dict[str, Any]] = Field(default_factory=list)
    # Governance surfaced from tags/policy for L3 checks.
    pii_columns: list[str] = Field(default_factory=list)
    write_allowlist: list[str] = Field(default_factory=list)
    # For python: the systems the notebook is expected to touch (may be empty —
    # a pure file-processing notebook grounds against exemplars only, §5.3).
    external_systems: list[str] = Field(default_factory=list)
    truncated: bool = False
    notes: str | None = None

    def render(self) -> str:
        """Render the pack as the schema section of the prompt. Deterministic
        ordering so identical packs produce byte-identical prompts (prefix
        cache friendliness, §4.5)."""
        lines: list[str] = []
        if self.tables:
            lines.append("Available tables (confirmed from catalog):")
            for t in sorted(self.tables, key=lambda x: x.fqn):
                cols = ", ".join(
                    f"{c.name} {c.type or '?'}"
                    + ("" if not c.tags else f" [{','.join(c.tags)}]")
                    for c in t.columns
                )
                pk = f" (partitioned by {', '.join(t.partition_keys)})" if t.partition_keys else ""
                lines.append(f"  - {t.fqn}{pk}: {cols}")
        if self.kafka_topics:
            lines.append("\nAvailable Kafka topics (from schema registry):")
            for k in sorted(self.kafka_topics, key=lambda x: x.topic):
                lines.append(f"  - {k.topic} [{k.schema_type}]")
                if k.schema_str:
                    lines.append(f"      schema: {k.schema_str[:800]}")
        if self.pii_columns:
            lines.append(
                "\nPII columns (must be masked or excluded from output): "
                + ", ".join(sorted(self.pii_columns))
            )
        if self.exemplars:
            lines.append("\nRelevant accepted examples from this project:")
            for ex in self.exemplars[:3]:
                path = ex.get("path", "example")
                snippet = str(ex.get("code", ""))[:600]
                lines.append(f"  # {path}\n{snippet}")
        if not lines:
            return "(no external systems referenced — write self-contained code)"
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Plan (§5.4 / §6.2)
# --------------------------------------------------------------------------
class PlanJoin(BaseModel):
    left: str
    right: str
    type: str = "inner"


class Plan(BaseModel):
    """Structured intent emitted before any code is written (§5.4). Validated
    against the grounding pack in code, not by the model."""

    target: Target
    intent: str
    tables: list[str] = Field(default_factory=list)
    joins: list[PlanJoin] = Field(default_factory=list)
    grain: str | None = None
    filters: list[str] = Field(default_factory=list)
    output_columns: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    # python-specific: systems the notebook will touch + declared side effects
    external_systems: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)  # e.g. "produce:orders", "write:/data/out.parquet"


# --------------------------------------------------------------------------
# Diagnostic (§6.3) — the normalised failure that drives repair
# --------------------------------------------------------------------------
class Diagnostic(BaseModel):
    rung: str  # L0..L5 or "PLAN"
    error_class: ErrorClass
    message: str
    offending_identifier: str | None = None
    position: dict[str, int] | None = None
    route_to: RepairRoute
    suggested_context: list[str] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        *,
        rung: str,
        error_class: ErrorClass,
        message: str,
        offending_identifier: str | None = None,
        suggested_context: list[str] | None = None,
    ) -> "Diagnostic":
        return cls(
            rung=rung,
            error_class=error_class,
            message=message,
            offending_identifier=offending_identifier,
            route_to=ROUTE_BY_CLASS.get(error_class, RepairRoute.GENERATE),
            suggested_context=suggested_context or [],
        )


# --------------------------------------------------------------------------
# Validation report (§6.4)
# --------------------------------------------------------------------------
class RungResult(BaseModel):
    rung: str
    status: Literal["pass", "fail", "skip"]
    duration_ms: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)
    diagnostic: Diagnostic | None = None


class ValidationReport(BaseModel):
    passed: bool
    rungs: list[RungResult] = Field(default_factory=list)
    # The first failing diagnostic, if any — what the repair controller acts on.
    first_failure: Diagnostic | None = None

    @property
    def status(self) -> str:
        # Maps to the existing codegen_messages.validation_status vocabulary.
        if not self.rungs:
            return "skipped"
        if self.passed:
            return "passed"
        return "failed"

    def as_detail(self) -> dict[str, Any]:
        """Flatten into the JSONB shape stored on the message row + streamed
        to the UI. Kept JSON-serialisable (enums -> values)."""
        return {
            "passed": self.passed,
            "rungs": [
                {
                    "rung": r.rung,
                    "status": r.status,
                    "duration_ms": r.duration_ms,
                    "detail": r.detail,
                    **(
                        {"error_class": r.diagnostic.error_class.value}
                        if r.diagnostic
                        else {}
                    ),
                }
                for r in self.rungs
            ],
            **(
                {
                    "first_failure": {
                        "rung": self.first_failure.rung,
                        "error_class": self.first_failure.error_class.value,
                        "message": self.first_failure.message,
                        "route_to": self.first_failure.route_to.value,
                        "offending_identifier": self.first_failure.offending_identifier,
                    }
                }
                if self.first_failure
                else {}
            ),
        }
