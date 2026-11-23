"""dbt renderer: compiled items -> one schema.yml document for one model.

Built entirely as Python data and serialized with yaml.safe_dump — no text
concatenation of caller values into YAML. SQL expressions inside
dbt_utils.expression_is_true quote identifiers for the project's adapter
dialect and render literals by type (app.renderers.security).

enforcement_mode -> test config (app.renderers.capability):
  blocking  severity: error
  warn      severity: warn
  async     severity: warn, tags: [dq_async]  (excluded from `dbt build`,
            run by the dbt runner as a separate `dbt test` job)
"""
from __future__ import annotations

import math
import re
from typing import Any

import yaml

from app.renderers.capability import is_renderable
from app.renderers.params import effective_parameters
from app.renderers.security import (
    RenderSecurityError, is_plain_identifier, quote_sql_identifier, render_sql_literal,
    sql_string, validate_custom_sql_condition, validate_identifier,
)

DQ_ASYNC_TAG = "dq_async"
_MODEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DbtCompileError(ValueError):
    pass


def dbt_model_name(target_table: str) -> str:
    """The dbt model a binding's target_table names (last dot-segment)."""
    name = target_table.split(".")[-1]
    if not _MODEL_NAME_RE.match(name or ""):
        raise DbtCompileError(f"target_table {target_table!r} does not name a valid dbt model")
    return name


def test_config(enforcement_mode: str) -> dict[str, Any]:
    if enforcement_mode == "blocking":
        return {"severity": "error"}
    if enforcement_mode == "async":
        return {"severity": "warn", "tags": [DQ_ASYNC_TAG]}
    return {"severity": "warn"}


def _regex_expr(qcol: str, pattern: str, dialect: str) -> str:
    if dialect == "pg_lake":
        return f"CAST({qcol} AS TEXT) ~ {sql_string(pattern, field='pattern')}"
    return f"REGEXP_LIKE(CAST({qcol} AS VARCHAR), {sql_string(pattern, field='pattern')})"


def _meta(item: dict, ctx: dict, params: dict) -> dict[str, Any]:
    return {
        "dq_rule": item["rule_name"],
        "dq_rule_id": str(item["rule_id"]),
        "dq_rule_version": int(item["rule_version"]),
        "dq_check_id": str(item["item_id"]),
        "dq_dimension": item["dimension"],
        "dq_rule_severity": str(item["severity"]).upper(),
        "dq_threshold": float(params.get("threshold", 0.0)),
        "dq_binding_id": str(ctx["binding_id"]),
        "dq_artifact_version_id": str(ctx["artifact_version_id"]),
        "dq_enforcement_mode": ctx["enforcement_mode"],
    }


def _test_for(item: dict, params: dict, config: dict, meta: dict, dialect: str
              ) -> tuple[str, dict]:
    """(placement, test) where placement is 'column' or 'model'."""
    rt = item["rule_type"]
    col = item.get("column_name") or ""
    body: dict[str, Any] = {"config": dict(config), "meta": meta}
    if rt == "not_null":
        return "column", {"not_null": body}
    if rt == "unique":
        return "column", {"unique": body}
    if rt == "accepted_values":
        values = params["values"]
        body = {"values": values, "quote": any(isinstance(v, str) for v in values), **body}
        return "column", {"accepted_values": body}
    qcol = quote_sql_identifier(col) if col else None
    if rt == "range":
        parts = []
        if params.get("min") is not None:
            parts.append(f"{qcol} >= {render_sql_literal(params['min'], field='min')}")
        if params.get("max") is not None:
            parts.append(f"{qcol} <= {render_sql_literal(params['max'], field='max')}")
        expr = f"{qcol} IS NULL OR (" + " AND ".join(parts) + ")"
        return "model", {"dbt_utils.expression_is_true": {"expression": expr, **body}}
    if rt == "regex":
        expr = f"{qcol} IS NULL OR {_regex_expr(qcol, params['pattern'], dialect)}"
        return "model", {"dbt_utils.expression_is_true": {"expression": expr, **body}}
    if rt == "freshness":
        minutes = max(1, int(math.ceil(float(params["max_age_hours"]) * 60)))
        return "model", {"dbt_utils.recency": {"datepart": "minute", "field": qcol,
                                               "interval": minutes, **body}}
    if rt == "custom_sql":
        expr = validate_custom_sql_condition(params["condition"])
        return "model", {"dbt_utils.expression_is_true": {"expression": expr, **body}}
    raise DbtCompileError(f"rule_type {rt!r} has no dbt rendering")


def render_dbt(items: list[dict], *, target_table: str, enforcement_mode: str,
               ctx: dict, dialect: str = "trino") -> tuple[str | None, list[dict]]:
    """Returns (schema_yml_text or None when nothing rendered, diagnostics)."""
    diagnostics: list[dict] = []
    model = dbt_model_name(target_table)
    config = test_config(enforcement_mode)
    model_tests: list[dict] = []
    columns: dict[str, dict] = {}

    for item in items:
        if not item.get("enabled", True):
            continue
        reason = is_renderable("dbt_project", item)
        if reason:
            diagnostics.append({"stage": "compile", "severity": "error", "target": "dbt",
                                "check_id": str(item.get("item_id")),
                                "rule_name": item["rule_name"], "rule_type": item["rule_type"],
                                "column_name": item.get("column_name") or "",
                                "message": reason})
            continue
        params = effective_parameters(item["rule_type"], item.get("parameters"),
                                      item.get("parameter_overrides"))
        try:
            placement, test = _test_for(item, params, config, _meta(item, ctx, params), dialect)
        except RenderSecurityError as e:
            diagnostics.append({"stage": "compile", "severity": "error", "target": "dbt",
                                "check_id": str(item.get("item_id")),
                                "rule_name": item["rule_name"], "rule_type": item["rule_type"],
                                "column_name": item.get("column_name") or "",
                                "message": f"rejected by injection guard: {e}"})
            continue
        if params.get("threshold"):
            diagnostics.append({"stage": "compile", "severity": "warning", "target": "dbt",
                                "check_id": str(item.get("item_id")),
                                "rule_name": item["rule_name"],
                                "message": "dbt generic tests count failing rows; the rate "
                                           f"threshold {params['threshold']} is recorded in "
                                           "meta.dq_threshold but dbt fails on any failing row"})
        if placement == "column":
            col = validate_identifier(item["column_name"], field="column_name")
            entry = columns.setdefault(col, {"name": col, "tests": []})
            if not is_plain_identifier(col):
                entry["quote"] = True
            entry["tests"].append(test)
        else:
            model_tests.append(test)

    if not model_tests and not columns:
        return None, diagnostics

    model_entry: dict[str, Any] = {"name": model}
    if model_tests:
        model_entry["tests"] = model_tests
    if columns:
        model_entry["columns"] = list(columns.values())
    doc = {"version": 2, "models": [model_entry]}
    text = ("# Generated by inaiera-data-quality — do not edit manually.\n"
            + yaml.safe_dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True))
    return text, diagnostics
