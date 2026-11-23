"""Python renderer: GX ExpectationSuite (JSON) + Soda checks (YAML).

Both are built as data and serialized (json / yaml.safe_dump); caller
values are never concatenated into YAML text. The outcome contract the
Python runner enforces (DQ-MODEL-002) is carried in the artifacts:
GX `meta.on_failure` and a Soda top comment, plus RenderOut.on_failure.
"""
from __future__ import annotations

import math
from typing import Any

import yaml

from app.renderers.capability import is_renderable
from app.renderers.params import effective_parameters
from app.renderers.security import RenderSecurityError, validate_identifier

ON_FAILURE = {"blocking": "block", "warn": "warn", "async": "async"}

_GX_TO_SODA_METRIC = {
    "expect_column_stdev_to_be_between": "stddev",
    "expect_column_mean_to_be_between": "avg",
    "expect_column_min_to_be_between": "min",
    "expect_column_max_to_be_between": "max",
    "expect_column_sum_to_be_between": "sum",
}


def _mostly(params: dict) -> float:
    return round(1.0 - float(params.get("threshold", 0.0)), 6)


def _meta(item: dict, ctx: dict) -> dict:
    return {"rule_name": item["rule_name"], "rule_id": str(item["rule_id"]),
            "rule_version": int(item["rule_version"]), "check_id": str(item["item_id"]),
            "severity": item["severity"], "dimension": item["dimension"],
            "binding_id": str(ctx["binding_id"])}


def _gx_expectation(item: dict, params: dict, ctx: dict) -> dict | None:
    rt = item["rule_type"]
    col = item.get("column_name") or ""
    meta = _meta(item, ctx)
    mostly = _mostly(params)
    match rt:
        case "not_null":
            return {"expectation_type": "expect_column_values_to_not_be_null",
                    "kwargs": {"column": col, "mostly": mostly}, "meta": meta}
        case "unique":
            return {"expectation_type": "expect_column_values_to_be_unique",
                    "kwargs": {"column": col, "mostly": mostly}, "meta": meta}
        case "accepted_values":
            return {"expectation_type": "expect_column_values_to_be_in_set",
                    "kwargs": {"column": col, "value_set": params["values"], "mostly": mostly},
                    "meta": meta}
        case "range":
            return {"expectation_type": "expect_column_values_to_be_between",
                    "kwargs": {"column": col, "min_value": params.get("min"),
                               "max_value": params.get("max"), "mostly": mostly}, "meta": meta}
        case "regex":
            return {"expectation_type": "expect_column_values_to_match_regex",
                    "kwargs": {"column": col, "regex": params["pattern"], "mostly": mostly},
                    "meta": meta}
        case "freshness":
            hours = float(params["max_age_hours"])
            return {"expectation_type": "expect_column_max_to_be_between",
                    "kwargs": {"column": col, "min_value": {
                        "$PARAMETER": f"now() - timedelta(hours={hours!r})"}},
                    "meta": meta}
        case "row_count":
            return {"expectation_type": "expect_table_row_count_to_be_between",
                    "kwargs": {"min_value": params["min_rows"], "max_value": params.get("max_rows")},
                    "meta": meta}
        case "custom_python":
            kwargs = {k: v for k, v in params.items() if k not in ("expectation", "threshold")}
            if col:
                kwargs.setdefault("column", col)
            return {"expectation_type": params["expectation"], "kwargs": kwargs, "meta": meta}
    return None   # custom_sql runs in Soda only


def _soda_col(col: str) -> str:
    validate_identifier(col, field="column_name")
    if '"' in col:
        raise RenderSecurityError("Soda column names cannot contain '\"'")
    return col if col.replace("_", "").isalnum() else f'"{col}"'


def _soda_check(item: dict, params: dict) -> dict | None:
    rt = item["rule_type"]
    col = item.get("column_name") or ""
    name = item["rule_name"]
    thr_pct = round(float(params.get("threshold", 0.0)) * 100, 6)
    c = _soda_col(col) if col else ""

    def invalid(extra: dict) -> dict:
        key = f"invalid_percent({c}) <= {thr_pct}" if thr_pct else f"invalid_count({c}) = 0"
        return {key: {"name": name, **extra}}

    match rt:
        case "not_null":
            key = f"missing_percent({c}) <= {thr_pct}" if thr_pct else f"missing_count({c}) = 0"
            return {key: {"name": name}}
        case "unique":
            return {f"duplicate_count({c}) = 0": {"name": name}}
        case "accepted_values":
            return invalid({"valid values": params["values"]})
        case "range":
            extra = {}
            if params.get("min") is not None:
                extra["valid min"] = params["min"]
            if params.get("max") is not None:
                extra["valid max"] = params["max"]
            return invalid(extra)
        case "regex":
            return invalid({"valid regex": params["pattern"]})
        case "row_count":
            if params.get("max_rows") is not None:
                return {f"row_count between {int(params['min_rows'])} and "
                        f"{int(params['max_rows'])}": {"name": name}}
            return {f"row_count >= {int(params['min_rows'])}": {"name": name}}
        case "freshness":
            minutes = max(1, int(math.ceil(float(params["max_age_hours"]) * 60)))
            return {f"freshness({c}) < {minutes}m": {"name": name}}
        case "custom_sql":
            return {"failed rows": {"name": name, "fail condition": params["condition"]}}
        case "custom_python":
            metric = _GX_TO_SODA_METRIC.get(params.get("expectation", ""))
            lo, hi = params.get("min_value"), params.get("max_value")
            if not metric or not col or (lo is None and hi is None):
                return None
            if lo is not None and hi is not None:
                return {f"{metric}({c}) between {lo!r} and {hi!r}": {"name": name}}
            if lo is not None:
                return {f"{metric}({c}) >= {lo!r}": {"name": name}}
            return {f"{metric}({c}) <= {hi!r}": {"name": name}}
    return None


def render_python(items: list[dict], *, target_table: str, enforcement_mode: str, ctx: dict
                  ) -> tuple[dict | None, str | None, list[dict]]:
    diagnostics: list[dict] = []
    expectations: list[dict] = []
    checks: list[dict] = []
    table = _soda_col(target_table.split(".")[-1])
    for item in items:
        if not item.get("enabled", True):
            continue
        reason = is_renderable("python_job", item)
        gx = soda = None
        if reason is None:
            try:
                params = effective_parameters(item["rule_type"], item.get("parameters"),
                                              item.get("parameter_overrides"))
                gx = _gx_expectation(item, params, ctx)
                soda = _soda_check(item, params)
            except RenderSecurityError as e:
                reason = f"rejected by injection guard: {e}"
        if reason:
            diagnostics.append({"stage": "compile", "severity": "error", "target": "python",
                                "check_id": str(item.get("item_id")),
                                "rule_name": item["rule_name"], "rule_type": item["rule_type"],
                                "column_name": item.get("column_name") or "", "message": reason})
            continue
        if gx:
            expectations.append(gx)
        else:
            diagnostics.append({"stage": "compile", "severity": "info", "target": "gx",
                                "check_id": str(item["item_id"]), "rule_name": item["rule_name"],
                                "message": "no GX expectation; enforced by the Soda checks only"})
        if soda:
            checks.append(soda)
        else:
            diagnostics.append({"stage": "compile", "severity": "info", "target": "soda",
                                "check_id": str(item["item_id"]), "rule_name": item["rule_name"],
                                "message": "no Soda equivalent; enforced by the GX suite only"})
    if not expectations and not checks:
        return None, None, diagnostics
    on_failure = ON_FAILURE.get(enforcement_mode, "warn")
    suite = {
        "expectation_suite_name": f"dq_{ctx['binding_id']}",
        "expectations": expectations,
        "meta": {"generated_by": "inaiera-data-quality", "great_expectations_version": "0.18.x",
                 "dq_enforcement_mode": enforcement_mode, "on_failure": on_failure,
                 "binding_id": str(ctx["binding_id"]),
                 "artifact_version_id": str(ctx["artifact_version_id"])},
    }
    soda_text = None
    if checks:
        soda_text = (
            "# Generated by inaiera-data-quality — do not edit manually.\n"
            f"# dq_enforcement_mode: {enforcement_mode}\n# on_failure: {on_failure}\n"
            + yaml.safe_dump({f"checks for {table}": checks}, sort_keys=False,
                             default_flow_style=False, allow_unicode=True))
    return suite, soda_text, diagnostics
