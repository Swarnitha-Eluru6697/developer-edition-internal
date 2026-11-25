"""Shared test builders (no I/O)."""
from __future__ import annotations

import uuid
from typing import Any

from app.core import quality_contract as qc
from app.renderers.plan import PlanColumn, PlanTable, StructuredJobPlan

ORG = "test-org"


def item(rule_type: str, column: str = "", *, params: dict | None = None,
         overrides: dict | None = None, name: str | None = None, severity: str = "ERROR",
         dimension: str = "validity", enabled: bool = True, review_status: str | None = None,
         rule_version: int = 1) -> dict[str, Any]:
    if rule_type == "custom_sql" and review_status is None:
        review_status = "approved"
    return {
        "item_id": str(uuid.uuid4()), "rule_id": str(uuid.uuid4()),
        "rule_name": name or f"{rule_type}_{column or 'table'}", "rule_type": rule_type,
        "rule_version": rule_version, "dimension": dimension, "severity": severity,
        "parameters": params or {}, "parameter_overrides": overrides or {},
        "column_name": column, "enabled": enabled, "sort_order": 0,
        "review_status": review_status,
    }


def source_table(name: str = "orders_src", columns: list[tuple[str, str]] | None = None,
                 options: dict | None = None) -> PlanTable:
    cols = columns or [("order_id", "BIGINT"), ("amount", "DECIMAL(10, 2)"),
                       ("status", "STRING"), ("email", "STRING"), ("updated_at", "TIMESTAMP(3)")]
    return PlanTable(name=name, columns=[PlanColumn(name=n, type=t) for n, t in cols],
                     options=options or {"connector": "kafka", "topic": "orders",
                                         "properties.bootstrap.servers": "kafka:9092",
                                         "properties.group.id": "transform-group",
                                         "scan.startup.mode": "earliest-offset",
                                         "format": "json"})


def plan(*, sources: list[PlanTable] | None = None, sinks: list[PlanTable] | None = None,
         engine_version: str = "1.19.1", contract_version: str | None = None,
         topics: dict | None = None, normalized_sql_hash: str = "a" * 64) -> StructuredJobPlan:
    return StructuredJobPlan(
        plan_version="1", engine="flink", engine_version=engine_version,
        normalized_sql_hash=normalized_sql_hash,
        sources=sources if sources is not None else [source_table()],
        sinks=sinks or [], unresolved_tables=[],
        quality_contract={"version": contract_version or qc.CONTRACT_VERSION,
                          "topics": topics or {"results": qc.RESULTS_TOPIC,
                                               "gate": qc.GATE_TOPIC,
                                               "quarantine": qc.QUARANTINE_TOPIC}},
    )


def plan_payload(**kw) -> dict:
    return plan(**kw).model_dump(mode="json")


def flink_ctx(mode: str = "warn", *, fail_stop: bool = False) -> dict:
    return {"org_id": ORG, "ucs_fqn": "iceberg.silver.orders", "binding_id": str(uuid.uuid4()),
            "artifact_version_id": str(uuid.uuid4()), "enforcement_mode": mode,
            "flink_fail_stop": fail_stop, "parent_job_id": str(uuid.uuid4()),
            "job_name": "dq-quality-test"}


TOPICS = {"results": qc.RESULTS_TOPIC, "gate": qc.GATE_TOPIC, "quarantine": qc.QUARANTINE_TOPIC}
