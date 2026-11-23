"""FlinkSQL renderer: compiled items + StructuredJobPlan -> quality job SQL.

Inputs are STRUCTURED only (DQ-FLINK-008): the source table chosen from
the FlinkSQL runner's plan (column names/types, WITH options) and the
shared quality contract (app.core.quality_contract) for every sink's topic
and schema (DQ-CONTRACT-005). Nothing here reads or re-parses SQL text.

Shape of the compiled job:
  CREATE TABLE `dq_source`          the plan's table, own consumer group,
                                    + processing-time attribute
  CREATE TABLE `dq_results_sink`    contract record window_result
  [blocking] CREATE TABLE `dq_gate_sink`        contract record gate_event
  [blocking] CREATE TABLE `dq_quarantine_sink`  contract record quarantine_record
  CREATE TEMPORARY VIEW `dq_check_<n>` per check: one aggregated row per
                                    processing-time TUMBLE window with
                                    evaluated_rows, violation_count, threshold,
                                    bounded samples (DQ-RESULT-010)
  EXECUTE STATEMENT SET BEGIN … END  results for every check; for blocking
                                    also gate events (breaching windows) and
                                    quarantined rows.

Samples are bounded by construction (FIRST_VALUE / LAST_VALUE / MIN / MAX
of the violating values, each truncated to the contract's max chars), which
also keeps the artifact valid on Flink 1.17–1.19 (no ARRAY_AGG).
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.core import quality_contract as qc
from app.renderers.capability import is_renderable
from app.renderers.params import effective_parameters
from app.renderers.plan import PlanTable, type_class
from app.renderers.security import (
    RenderSecurityError, UnsafeIdentifierError, comment_safe, quote_flink_identifier, render_sql_literal, sql_string,
    validate_custom_sql_condition, validate_flink_type,
)

DEFAULT_WINDOW_MINUTES = 1
MAX_SAMPLE_SLOTS = 4
SOURCE_TABLE = "dq_source"
RESULTS_SINK = "dq_results_sink"
GATE_SINK = "dq_gate_sink"
QUARANTINE_SINK = "dq_quarantine_sink"
_OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_DROPPED_SOURCE_OPTIONS = ("sink.", "scan.startup.specific-offsets", "scan.startup.timestamp-millis",
                           "properties.group.id", "scan.startup.mode",
                           "properties.auto.offset.reset")


class FlinkCompileError(ValueError):
    pass


def _q(name: str) -> str:
    return quote_flink_identifier(name, field="identifier")


def _options_sql(options: dict[str, str]) -> str:
    lines = []
    for k, v in options.items():
        if not _OPTION_KEY_RE.match(k):
            raise UnsafeIdentifierError(f"unsafe connector option key {k!r}")
        lines.append(f"  {sql_string(k, field='option key')} = {sql_string(v, field='option value')}")
    return ",\n".join(lines)


def proc_time_column(source: PlanTable) -> str:
    name = "__dq_proc_time"
    existing = {c.name for c in source.columns}
    while name in existing:
        name += "_"
    return name


def source_ddl(source: PlanTable, *, group_id: str) -> str:
    if not source.columns:
        raise FlinkCompileError(f"source table {source.name!r} has no columns in the plan")
    cols = [f"  {_q(c.name)} {validate_flink_type(c.type)}" for c in source.columns]
    cols.append(f"  {_q(proc_time_column(source))} AS PROCTIME()")
    options = {k: v for k, v in source.options.items()
               if not k.startswith(_DROPPED_SOURCE_OPTIONS)}
    options["properties.group.id"] = group_id
    options["scan.startup.mode"] = "group-offsets"
    options["properties.auto.offset.reset"] = "latest"
    if options.get("format") == "json" or options.get("value.format") == "json":
        key = "json.ignore-parse-errors" if options.get("format") == "json" \
            else "value.json.ignore-parse-errors"
        options.setdefault(key, "true")
    return (f"CREATE TABLE {_q(SOURCE_TABLE)} (\n" + ",\n".join(cols)
            + "\n) WITH (\n" + _options_sql(options) + "\n);")


def sink_ddl(table: str, record_type: str, topic: str) -> str:
    cols = [f"  {_q(f['name'])} {f['flink_type']}" for f in qc.fields(record_type)]
    options = {"connector": qc.KAFKA_SINK_OPTIONS["connector"], "topic": topic,
               "properties.bootstrap.servers": "${KAFKA_BOOTSTRAP_SERVERS}"}
    options.update({k: v for k, v in qc.KAFKA_SINK_OPTIONS.items() if k != "connector"})
    return (f"CREATE TABLE {_q(table)} (\n" + ",\n".join(cols)
            + "\n) WITH (\n" + _options_sql(options) + "\n);")


def violation_condition(item: dict, params: dict, source: PlanTable) -> str:
    """Predicate that is TRUE for a violating row."""
    rt = item["rule_type"]
    col = item.get("column_name") or ""
    if rt == "custom_sql":
        return f"NOT ({validate_custom_sql_condition(params['condition'])})"
    qcol = _q(col)
    ctype = source.column(col).type if source.column(col) else "STRING"
    cls = type_class(ctype)
    if rt == "not_null":
        return f"{qcol} IS NULL"
    if rt == "accepted_values":
        values = params["values"]
        if cls == "numeric" and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                    for v in values):
            lits = ", ".join(render_sql_literal(v) for v in values)
            return f"{qcol} IS NOT NULL AND {qcol} NOT IN ({lits})"
        lits = ", ".join(sql_string(str(v).lower() if isinstance(v, bool) else str(v))
                         for v in values)
        return f"{qcol} IS NOT NULL AND CAST({qcol} AS STRING) NOT IN ({lits})"
    if rt == "range":
        parts = []
        if params.get("min") is not None:
            parts.append(f"{qcol} < {render_sql_literal(params['min'], field='min')}")
        if params.get("max") is not None:
            parts.append(f"{qcol} > {render_sql_literal(params['max'], field='max')}")
        return "(" + " OR ".join(parts) + ")"
    if rt == "regex":
        return (f"{qcol} IS NOT NULL AND NOT REGEXP(CAST({qcol} AS STRING), "
                f"{sql_string(params['pattern'], field='pattern')})")
    if rt == "freshness":
        minutes = max(1, int(math.ceil(float(params["max_age_hours"]) * 60)))
        upper = ctype.upper()
        if upper.startswith("TIMESTAMP_LTZ") or "LOCAL TIME ZONE" in upper:
            return f"{qcol} < TIMESTAMPADD(MINUTE, -{minutes}, CURRENT_TIMESTAMP)"
        return f"CAST({qcol} AS TIMESTAMP(3)) < TIMESTAMPADD(MINUTE, -{minutes}, LOCALTIMESTAMP)"
    raise FlinkCompileError(f"rule_type {rt!r} has no FlinkSQL rendering")


def _samples_expr(col: str, cond: str, slots: int) -> str:
    if not col or slots <= 0:
        return "CAST(NULL AS ARRAY<STRING>)"
    val = (f"CASE WHEN {cond} THEN SUBSTRING(CAST({_q(col)} AS STRING) FROM 1 FOR "
           f"{qc.MAX_SAMPLE_CHARS}) END")
    aggs = ["FIRST_VALUE", "LAST_VALUE", "MIN", "MAX"][:min(slots, MAX_SAMPLE_SLOTS)]
    return "ARRAY[" + ", ".join(f"{a}({val})" for a in aggs) + "]"


def _check_view(n: int, item: dict, params: dict, cond: str, ctx: dict, source: PlanTable,
                window_minutes: int, sample_slots: int) -> str:
    col = item.get("column_name") or ""
    breach = "fail" if str(item["severity"]).upper() in ("CRITICAL", "ERROR") else "warn"
    values: dict[str, str] = {
        "contract_version": sql_string(qc.CONTRACT_VERSION),
        "record_type": sql_string("window_result"),
        "org_id": sql_string(ctx["org_id"]),
        "ucs_fqn": sql_string(ctx["ucs_fqn"]),
        "binding_id": sql_string(str(ctx["binding_id"])),
        "artifact_version_id": sql_string(str(ctx["artifact_version_id"])),
        "check_id": sql_string(str(item["item_id"])),
        "rule_id": sql_string(str(item["rule_id"])),
        "rule_name": sql_string(item["rule_name"]),
        "rule_version": f"CAST({int(item['rule_version'])} AS INT)",
        "dimension": sql_string(item["dimension"]),
        "severity": sql_string(item["severity"]),
        "enforcement_mode": sql_string(ctx["enforcement_mode"]),
        "breach_status": sql_string(breach),
        "threshold": f"CAST({float(params.get('threshold', 0.0))!r} AS DOUBLE)",
        "column_name": sql_string(col) if col else "CAST(NULL AS STRING)",
        "window_start": "window_start",
        "window_end": "window_end",
        "window_seconds": f"CAST({int(window_minutes) * 60} AS INT)",
        "evaluated_rows": "CAST(COUNT(*) AS BIGINT)",
        "violation_count": f"CAST(COALESCE(SUM(CASE WHEN {cond} THEN 1 ELSE 0 END), 0) AS BIGINT)",
        "sample_values": _samples_expr(col, cond, sample_slots),
        "run_ref": sql_string("${DQ_RUN_REF}"),
        "parent_job_id": sql_string(str(ctx["parent_job_id"])) if ctx.get("parent_job_id")
        else "CAST(NULL AS STRING)",
        "emitted_at": "LOCALTIMESTAMP",
    }
    select = ",\n  ".join(f"{values[name]} AS {_q(name)}" for name in qc.field_names("window_result"))
    return (f"-- check {comment_safe(item['rule_name'])} ({comment_safe(item['item_id'])})\n"
            f"CREATE TEMPORARY VIEW {_q(f'dq_check_{n}')} AS\nSELECT\n  {select}\n"
            f"FROM TABLE(TUMBLE(TABLE {_q(SOURCE_TABLE)}, DESCRIPTOR({_q(proc_time_column(source))}), "
            f"INTERVAL '{int(window_minutes)}' MINUTE))\nGROUP BY window_start, window_end;")


def _quarantine_select(item: dict, cond: str, ctx: dict, source: PlanTable) -> str:
    json_cols = [c for c in source.columns if type_class(c.type) != "complex"]
    row_json = ("JSON_OBJECT(" + ", ".join(f"KEY {sql_string(c.name)} VALUE {_q(c.name)}"
                                            for c in json_cols) + ")") if json_cols \
        else "CAST('{}' AS STRING)"
    values = {
        "contract_version": sql_string(qc.CONTRACT_VERSION),
        "record_type": sql_string("quarantine_record"),
        "org_id": sql_string(ctx["org_id"]),
        "ucs_fqn": sql_string(ctx["ucs_fqn"]),
        "binding_id": sql_string(str(ctx["binding_id"])),
        "artifact_version_id": sql_string(str(ctx["artifact_version_id"])),
        "check_id": sql_string(str(item["item_id"])),
        "rule_name": sql_string(item["rule_name"]),
        "run_ref": sql_string("${DQ_RUN_REF}"),
        "parent_job_id": sql_string(str(ctx["parent_job_id"])) if ctx.get("parent_job_id")
        else "CAST(NULL AS STRING)",
        "detected_at": "LOCALTIMESTAMP",
        "row_json": row_json,
    }
    select = ", ".join(f"{values[n]} AS {_q(n)}" for n in qc.field_names("quarantine_record"))
    return f"SELECT {select} FROM {_q(SOURCE_TABLE)} WHERE {cond}"


def render_flink(
    items: list[dict], *, source: PlanTable, ctx: dict, topics: dict[str, str],
    window_minutes: int = DEFAULT_WINDOW_MINUTES, sample_slots: int = MAX_SAMPLE_SLOTS,
) -> tuple[str | None, list[dict]]:
    """ctx: org_id, ucs_fqn, binding_id, artifact_version_id, enforcement_mode,
    flink_fail_stop, parent_job_id, job_name. topics: results/gate/quarantine."""
    diagnostics: list[dict] = []
    blocking = ctx["enforcement_mode"] == "blocking"
    checks: list[tuple[int, dict, dict, str]] = []

    for item in items:
        if not item.get("enabled", True):
            continue
        reason = is_renderable("flink_job", item, source)
        if reason is None:
            try:
                params = effective_parameters(item["rule_type"], item.get("parameters"),
                                              item.get("parameter_overrides"))
                cond = violation_condition(item, params, source)
                for f in ("rule_name", "dimension", "severity"):
                    sql_string(item[f], field=f)
            except (RenderSecurityError, FlinkCompileError) as e:
                reason = f"rejected by injection guard: {e}"
        if reason:
            diagnostics.append({"stage": "compile", "severity": "error", "target": "flink",
                                "check_id": str(item.get("item_id")),
                                "rule_name": item["rule_name"], "rule_type": item["rule_type"],
                                "column_name": item.get("column_name") or "", "message": reason})
            continue
        checks.append((len(checks) + 1, item, params, cond))

    if not checks:
        return None, diagnostics

    if blocking:
        complex_cols = [c.name for c in source.columns if type_class(c.type) == "complex"]
        if complex_cols:
            diagnostics.append({"stage": "compile", "severity": "warning", "target": "flink",
                                "message": f"quarantine row_json omits complex columns {complex_cols}"})

    action = "fail_stop" if ctx.get("flink_fail_stop") else "quarantine"
    statements = [
        "-- ============================================================\n"
        f"-- FlinkSQL quality job: {comment_safe(ctx.get('job_name', ''))}\n"
        "-- Generated by inaiera-data-quality — do not edit manually.\n"
        f"-- Quality contract {qc.CONTRACT_NAME} v{qc.CONTRACT_VERSION}; "
        f"enforcement: {ctx['enforcement_mode']}"
        + (f", gate action: {action}" if blocking else "") + "\n"
        "-- ============================================================",
        source_ddl(source, group_id=f"dq-quality-{ctx['binding_id']}"),
        sink_ddl(RESULTS_SINK, "window_result", topics["results"]),
    ]
    if blocking:
        statements.append(sink_ddl(GATE_SINK, "gate_event", topics["gate"]))
        statements.append(sink_ddl(QUARANTINE_SINK, "quarantine_record", topics["quarantine"]))
    for n, item, params, cond in checks:
        statements.append(_check_view(n, item, params, cond, ctx, source, window_minutes,
                                      sample_slots))

    views = [_q(f"dq_check_{n}") for n, *_ in checks]
    inserts = [f"INSERT INTO {_q(RESULTS_SINK)}\n"
               + "\nUNION ALL\n".join(f"SELECT * FROM {v}" for v in views) + ";"]
    if blocking:
        gate_cols = ", ".join(_q(n) for n in qc.field_names("window_result"))
        breach = ("CAST(`violation_count` AS DOUBLE) > `threshold` * CAST(`evaluated_rows` AS DOUBLE)")
        inserts.append(
            f"INSERT INTO {_q(GATE_SINK)}\n"
            + "\nUNION ALL\n".join(
                f"SELECT {gate_cols}, {sql_string(action)} AS `action` FROM {v} WHERE {breach}"
                for v in views) + ";")
        inserts.append(
            f"INSERT INTO {_q(QUARANTINE_SINK)}\n"
            + "\nUNION ALL\n".join(_quarantine_select(item, cond, ctx, source)
                                   for _, item, _, cond in checks) + ";")
    statements.append("EXECUTE STATEMENT SET\nBEGIN\n" + "\n".join(inserts) + "\nEND;")
    return "\n\n".join(statements) + "\n", diagnostics


def flink_sinks(enforcement_mode: str) -> dict[str, str]:
    """sink table -> contract topic key, for validation."""
    sinks = {RESULTS_SINK: "results"}
    if enforcement_mode == "blocking":
        sinks.update({GATE_SINK: "gate", QUARANTINE_SINK: "quarantine"})
    return sinks


def sample_slots_for(limit: Any) -> int:
    return max(0, min(int(limit), MAX_SAMPLE_SLOTS))
