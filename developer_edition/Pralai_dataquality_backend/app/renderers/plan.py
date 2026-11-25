"""The FlinkSQL runner's StructuredJobPlan, as DQ consumes it (DQ-FLINK-008).

DQ never parses parent-job SQL text. The FlinkSQL runner's scoped
POST /api/v1/validate returns a `plan` object (its sql_plan_service walks
the script's real bracket/quote structure; the Flink SQL Gateway validates
it); DQ compiles the quality job only from that structure: the chosen
table's column names/types and WITH options, the runner-reported Flink
engine version, the normalized parent-SQL hash, and the runner's quality
contract version.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field

PLAN_VERSION = "1"
_SUPPORTED_CONNECTORS = {"kafka"}


class PlanError(ValueError):
    pass


class PlanColumn(BaseModel):
    name: str
    type: str


class PlanTable(BaseModel):
    name: str
    columns: list[PlanColumn] = Field(default_factory=list)
    options: dict[str, str] = Field(default_factory=dict)

    @property
    def connector(self) -> str | None:
        return self.options.get("connector")

    def column(self, name: str) -> PlanColumn | None:
        return next((c for c in self.columns if c.name == name), None)


class PlanQualityContract(BaseModel):
    version: str
    topics: dict[str, str] = Field(default_factory=dict)


class StructuredJobPlan(BaseModel):
    plan_version: str
    engine: str = "flink"
    engine_version: str
    normalized_sql_hash: str
    sources: list[PlanTable] = Field(default_factory=list)
    sinks: list[PlanTable] = Field(default_factory=list)
    unresolved_tables: list[str] = Field(default_factory=list)
    quality_contract: PlanQualityContract | None = None

    def plan_hash(self) -> str:
        doc = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"))
                              .encode()).hexdigest()


def select_quality_source(plan: StructuredJobPlan, target_table: str) -> tuple[PlanTable, list[dict]]:
    """Pick the Kafka-backed table the quality job reads.

    1. A source or sink table named exactly `target_table` (or its last
       dot-segment) — checking a sink means checking the job's OUTPUT topic.
    2. Otherwise, exactly one Kafka source.
    Anything else is ambiguous and refused (never guessed)."""
    diagnostics: list[dict] = []
    tables = [*plan.sources, *plan.sinks]
    wanted = {target_table, target_table.split(".")[-1]}
    named = [t for t in tables if t.name in wanted]
    if named:
        table = named[0]
        if table.connector not in _SUPPORTED_CONNECTORS:
            raise PlanError(
                f"table {table.name!r} uses connector {table.connector!r}; quality jobs can only "
                f"read {sorted(_SUPPORTED_CONNECTORS)} tables")
        return table, diagnostics
    kafka_sources = [t for t in plan.sources if t.connector in _SUPPORTED_CONNECTORS]
    if len(kafka_sources) == 1:
        diagnostics.append({
            "stage": "compile", "severity": "info", "target": "flink",
            "message": (f"target_table {target_table!r} is not a table in the parent job's plan; "
                        f"using its only Kafka source {kafka_sources[0].name!r}")})
        return kafka_sources[0], diagnostics
    if not kafka_sources:
        raise PlanError("the parent job's structured plan has no Kafka source table to check"
                        + (f" (unresolved catalog tables: {plan.unresolved_tables})"
                           if plan.unresolved_tables else ""))
    raise PlanError(
        f"the parent job reads {len(kafka_sources)} Kafka sources "
        f"({[t.name for t in kafka_sources]}); set the binding's target_table to one of them")


_NUMERIC = re.compile(r"^(TINYINT|SMALLINT|INT|INTEGER|BIGINT|FLOAT|DOUBLE|REAL|DECIMAL|DEC|NUMERIC)\b",
                      re.IGNORECASE)
_TEMPORAL = re.compile(r"^(TIMESTAMP|TIMESTAMP_LTZ|DATE|TIME)\b", re.IGNORECASE)
_COMPLEX = re.compile(r"^(ROW|ARRAY|MAP|MULTISET)\b", re.IGNORECASE)


def type_class(flink_type: str) -> str:
    t = flink_type.strip()
    if _NUMERIC.match(t):
        return "numeric"
    if _TEMPORAL.match(t):
        return "temporal"
    if _COMPLEX.match(t):
        return "complex"
    return "scalar"


def parse_plan(payload: Any) -> StructuredJobPlan:
    try:
        return StructuredJobPlan.model_validate(payload)
    except Exception as e:  # noqa: BLE001
        raise PlanError(f"the FlinkSQL runner returned no valid structured plan ({e})")
