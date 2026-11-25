"""Engine capability matrix and per-rule capability (DQ-MODEL-002, DQ-CAP-007).

ENGINE_CAPABILITY_MATRIX is the single source of truth for what each
enforcement_mode MEANS on each engine; the renderers implement exactly
this, the API serves it (GET /api/v1/capabilities), and apply refuses to
push a blocking binding containing a rule the engine cannot express.

  engine  blocking                          warn                async
  dbt     test severity=error: a failing    severity=warn:      severity=warn + tag dq_async; the
          test FAILS `dbt build`            logged, build OK    runner excludes tag:dq_async from
                                                                `dbt build` and runs them as a
                                                                separate `dbt test` job
  flink   explicit quarantine/gate contract: windowed advisory  same as warn (the quality job
          violating rows -> quarantine      results only        already runs beside the parent)
          topic, breaching windows -> gate
          topic; parent job is NOT stopped
          unless the binding opts into
          fail_stop (flink_fail_stop=true)
  python  on_failure=block: the Python      on_failure=warn     on_failure=async: run detached;
          runner fails the task             log and continue    task outcome not gated

classify_items() reports, per enabled item, supported / unsupported /
degraded for a job_type + enforcement_mode, using the effective typed
parameters, custom_sql review state and (for Flink) the real source schema
from the runner's structured plan.
"""
from __future__ import annotations

from typing import Any

from app.renderers.params import ParameterError, effective_parameters
from app.renderers.plan import PlanTable, type_class

S, U = "supported", "unsupported"

ENGINE_CAPABILITY_MATRIX: dict[str, dict[str, Any]] = {
    "dbt_project": {
        "engine": "dbt-core",
        "modes": {
            "blocking": {
                "guarantee": "enforced", "enforced_by": "dbt build",
                "mechanism": "test config.severity=error",
                "semantics": "A failing test fails `dbt build` (error) and its downstream "
                             "nodes are skipped."},
            "warn": {
                "guarantee": "advisory", "enforced_by": "dbt build",
                "mechanism": "test config.severity=warn",
                "semantics": "A failing test is reported as a warning; the build succeeds."},
            "async": {
                "guarantee": "advisory", "enforced_by": "separate dbt test job",
                "mechanism": "config.severity=warn + tags:[dq_async]",
                "semantics": "Tests are excluded from `dbt build` (--exclude tag:dq_async) and "
                             "run by the dbt runner as a separate `dbt test --select "
                             "tag:dq_async` job whose outcome never fails the build."},
        },
        "rule_types": {"not_null": S, "unique": S, "accepted_values": S, "range": S, "regex": S,
                       "freshness": S, "custom_sql": S, "row_count": U,
                       "row_count_anomaly": U, "custom_python": U},
    },
    "flink_job": {
        "engine": "flink",
        "modes": {
            "blocking": {
                "guarantee": "degraded", "enforced_by": "quality gate contract",
                "mechanism": "quarantine topic + gate topic (shared quality contract)",
                "semantics": "Each violating row is written to the contract's quarantine topic "
                             "and every window whose violation rate exceeds the threshold emits "
                             "a gate event. The parent job keeps running: fail-stop (the runner "
                             "stops the parent job on a gate event) happens ONLY when the "
                             "binding explicitly sets flink_fail_stop=true."},
            "warn": {
                "guarantee": "advisory", "enforced_by": "none",
                "mechanism": "windowed results topic",
                "semantics": "One aggregated result per check per window is delivered to UCS; "
                             "the parent job is unaffected."},
            "async": {
                "guarantee": "advisory", "enforced_by": "none",
                "mechanism": "windowed results topic",
                "semantics": "Identical to warn: the quality job already runs in parallel to "
                             "the parent job."},
        },
        "rule_types": {"not_null": S, "accepted_values": S, "range": S, "regex": S,
                       "freshness": S, "custom_sql": S, "unique": U, "row_count": U,
                       "row_count_anomaly": U, "custom_python": U},
    },
    "python_job": {
        "engine": "great_expectations/soda",
        "modes": {
            "blocking": {
                "guarantee": "enforced_by_runner", "enforced_by": "python runner task outcome",
                "mechanism": "artifact meta on_failure=block",
                "semantics": "The Python runner must fail (non-zero exit) the task when any "
                             "expectation/check fails."},
            "warn": {
                "guarantee": "advisory", "enforced_by": "python runner",
                "mechanism": "artifact meta on_failure=warn",
                "semantics": "Failures are logged and reported; the task continues."},
            "async": {
                "guarantee": "advisory", "enforced_by": "python runner",
                "mechanism": "artifact meta on_failure=async",
                "semantics": "Checks run detached; the task outcome is never gated on them."},
        },
        "rule_types": {"not_null": S, "unique": S, "accepted_values": S, "range": S, "regex": S,
                       "freshness": S, "row_count": S, "custom_sql": S, "custom_python": S,
                       "row_count_anomaly": U},
    },
}

_COLUMN_REQUIRED = {"not_null", "unique", "accepted_values", "range", "regex", "freshness"}
_UNSUPPORTED_REASONS = {
    ("dbt_project", "row_count"): "dbt has no generic test for a table-level row-count aggregate",
    ("dbt_project", "row_count_anomaly"): "dbt has no generic test for a row-count anomaly",
    ("dbt_project", "custom_python"): "custom_python names a GX expectation; dbt cannot run it",
    ("flink_job", "unique"): "streaming uniqueness needs a dedup job, not a windowed predicate",
    ("flink_job", "row_count"): "a table-level row count has no streaming equivalent",
    ("flink_job", "row_count_anomaly"): "a row-count anomaly has no streaming equivalent",
    ("flink_job", "custom_python"): "custom_python names a GX expectation; Flink cannot run it",
    ("python_job", "row_count_anomaly"): "no GX/Soda check expresses a 7-day row-count anomaly",
}


def degraded_reason(job_type: str, enforcement_mode: str, flink_fail_stop: bool) -> str | None:
    if job_type == "flink_job" and enforcement_mode == "blocking":
        return ("FlinkSQL blocking is a quarantine/gate contract, not inline enforcement: "
                + ("on a breaching window the runner stops the parent job (fail_stop opted in), "
                   "after the window's rows were already processed"
                   if flink_fail_stop else
                   "violating rows are quarantined and gate events emitted, but the parent job "
                   "keeps running (fail_stop not opted in)"))
    return None


def _rule_unsupported_reason(job_type: str, item: dict, source: PlanTable | None) -> str | None:
    rule_type = item["rule_type"]
    column = item.get("column_name") or ""
    matrix = ENGINE_CAPABILITY_MATRIX.get(job_type)
    if matrix is None:
        return f"unknown job_type {job_type!r}"
    if matrix["rule_types"].get(rule_type) != S:
        return _UNSUPPORTED_REASONS.get((job_type, rule_type),
                                        f"rule_type {rule_type!r} has no {job_type} equivalent")
    if rule_type == "custom_sql" and item.get("review_status") != "approved":
        return "custom_sql rule is privileged and has not been approved (review_status=" \
               f"{item.get('review_status')!r})"
    try:
        params = effective_parameters(rule_type, item.get("parameters"),
                                      item.get("parameter_overrides"), strict=True)
    except ParameterError as e:
        return f"invalid parameters: {e}"
    if rule_type in _COLUMN_REQUIRED and not column:
        return f"rule_type {rule_type!r} needs a column_name"
    if job_type == "python_job" and rule_type == "custom_python" and not params.get("expectation"):
        return "custom_python needs an `expectation` parameter"
    if job_type == "flink_job" and source is not None:
        if column:
            col = source.column(column)
            if col is None:
                return f"column {column!r} does not exist in source table {source.name!r}"
            cls = type_class(col.type)
            if rule_type == "range" and cls != "numeric":
                return f"range needs a numeric column; {column!r} is {col.type}"
            if rule_type == "freshness" and cls != "temporal":
                return f"freshness needs a TIMESTAMP/DATE column; {column!r} is {col.type}"
            if cls == "complex" and rule_type in ("accepted_values", "regex"):
                return f"{rule_type} cannot be applied to complex column type {col.type}"
    return None


def classify_items(
    items: list[dict], job_type: str, enforcement_mode: str = "warn", *,
    source: PlanTable | None = None, flink_fail_stop: bool = False,
) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {"supported": [], "unsupported": [], "degraded": []}
    degraded = degraded_reason(job_type, enforcement_mode, flink_fail_stop)
    for item in items:
        if not item.get("enabled", True):
            continue
        entry = {"item_id": item.get("item_id"), "rule_id": item.get("rule_id"),
                 "rule_name": item["rule_name"], "rule_type": item["rule_type"],
                 "column_name": item.get("column_name") or ""}
        reason = _rule_unsupported_reason(job_type, item, source)
        if reason:
            buckets["unsupported"].append({**entry, "reason": reason})
        elif degraded:
            buckets["degraded"].append({**entry, "reason": degraded})
        else:
            buckets["supported"].append(entry)
    return buckets


def is_renderable(job_type: str, item: dict, source: PlanTable | None = None) -> str | None:
    """None when the item compiles for job_type, else the reason it does not."""
    return _rule_unsupported_reason(job_type, item, source)
