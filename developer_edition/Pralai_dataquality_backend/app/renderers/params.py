"""Typed rule parameters (DQ-SEC-006 "typed params").

Every rule_type has a closed parameter schema. Parameters are validated
when a rule is saved, when an item override is saved, and again at compile
time on the EFFECTIVE (rule + override) parameters — so nothing untyped can
reach a renderer. Unknown keys are rejected.

Common to every type:
  threshold  float in [0, 1] — the maximum tolerated violation RATE before
             a run is a breach (default 0: any violation breaches).
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.renderers.security import (
    RenderSecurityError, validate_custom_sql_condition, validate_literal_text,
)

MAX_ACCEPTED_VALUES = 1000
MAX_PATTERN_LENGTH = 1000
_EXPECTATION_RE = re.compile(r"^expect_[a-z0-9_]{1,100}$")
_KWARG_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


class ParameterError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _number(params: dict, key: str, *, allow_none: bool = True, minimum: float | None = None,
            maximum: float | None = None, integer: bool = False) -> Any:
    v = params.get(key)
    if v is None:
        if allow_none:
            return None
        raise ParameterError(key, "is required")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or \
            (isinstance(v, float) and not math.isfinite(v)):
        raise ParameterError(key, f"must be a number, got {type(v).__name__}")
    if integer and not (isinstance(v, int) or float(v).is_integer()):
        raise ParameterError(key, "must be an integer")
    if minimum is not None and v < minimum:
        raise ParameterError(key, f"must be >= {minimum}")
    if maximum is not None and v > maximum:
        raise ParameterError(key, f"must be <= {maximum}")
    return int(v) if integer else v


_ALLOWED_KEYS: dict[str, set[str]] = {
    "not_null": set(),
    "unique": set(),
    "accepted_values": {"values"},
    "range": {"min", "max"},
    "regex": {"pattern"},
    "freshness": {"max_age_hours"},
    "row_count": {"min_rows", "max_rows"},
    "row_count_anomaly": {"pct_threshold"},
    "custom_sql": {"condition", "engine"},
    "custom_python": None,  # type: ignore[dict-item]  (expectation kwargs, checked below)
}


def validate_parameters(rule_type: str, params: Any, *, strict: bool = False) -> dict[str, Any]:
    """Validate and return normalized parameters.

    strict=True is used at compile time: parameters that are acceptable on a
    reusable rule TEMPLATE (e.g. accepted_values with an empty list, a range
    with neither bound) but cannot produce a meaningful check are rejected.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ParameterError("parameters", "must be an object")
    if rule_type not in _ALLOWED_KEYS:
        raise ParameterError("rule_type", f"unknown rule_type {rule_type!r}")
    out: dict[str, Any] = {}
    if "threshold" in params:
        out["threshold"] = float(_number(params, "threshold", minimum=0, maximum=1))
    allowed = _ALLOWED_KEYS[rule_type]
    if allowed is not None:
        unknown = sorted(set(params) - allowed - {"threshold"})
        if unknown:
            raise ParameterError(unknown[0], f"is not a parameter of rule_type {rule_type!r}")

    match rule_type:
        case "accepted_values":
            values = params.get("values", [])
            if not isinstance(values, list):
                raise ParameterError("values", "must be a list")
            if len(values) > MAX_ACCEPTED_VALUES:
                raise ParameterError("values", f"at most {MAX_ACCEPTED_VALUES} values")
            clean = []
            for i, v in enumerate(values):
                if isinstance(v, bool) or isinstance(v, (int, float)):
                    if isinstance(v, float) and not math.isfinite(v):
                        raise ParameterError(f"values[{i}]", "must be finite")
                    clean.append(v)
                elif isinstance(v, str):
                    try:
                        validate_literal_text(v, field=f"values[{i}]")
                    except RenderSecurityError as e:
                        raise ParameterError(f"values[{i}]", str(e))
                    clean.append(v)
                else:
                    raise ParameterError(f"values[{i}]", "must be a string, number or boolean")
            if strict and not clean:
                raise ParameterError("values", "at least one accepted value is required")
            out["values"] = clean
        case "range":
            mn = _number(params, "min")
            mx = _number(params, "max")
            if mn is not None and mx is not None and mn > mx:
                raise ParameterError("min", "must be <= max")
            if strict and mn is None and mx is None:
                raise ParameterError("min", "at least one of min/max is required")
            out.update(min=mn, max=mx)
        case "regex":
            pattern = params.get("pattern", ".*")
            if not isinstance(pattern, str) or not pattern:
                raise ParameterError("pattern", "must be a non-empty string")
            if len(pattern) > MAX_PATTERN_LENGTH:
                raise ParameterError("pattern", f"at most {MAX_PATTERN_LENGTH} characters")
            try:
                validate_literal_text(pattern, field="pattern")
                re.compile(pattern)
            except (RenderSecurityError, re.error) as e:
                raise ParameterError("pattern", f"is not a valid regular expression ({e})")
            out["pattern"] = pattern
        case "freshness":
            out["max_age_hours"] = _number(params, "max_age_hours", allow_none=True,
                                           minimum=0.001, maximum=87600) or 24
        case "row_count":
            mn = _number(params, "min_rows", integer=True, minimum=0)
            mx = _number(params, "max_rows", integer=True, minimum=0)
            mn = 1 if mn is None else mn
            if mx is not None and mx < mn:
                raise ParameterError("max_rows", "must be >= min_rows")
            out.update(min_rows=mn, max_rows=mx)
        case "row_count_anomaly":
            out["pct_threshold"] = _number(params, "pct_threshold", minimum=0.0001,
                                           maximum=1000) or 20
        case "custom_sql":
            try:
                out["condition"] = validate_custom_sql_condition(params.get("condition"))
            except RenderSecurityError as e:
                raise ParameterError("condition", str(e))
            engine = params.get("engine")
            if engine is not None:
                if engine not in ("trino", "pg_lake", "flink", "any"):
                    raise ParameterError("engine", "must be one of trino, pg_lake, flink, any")
                out["engine"] = engine
        case "custom_python":
            expectation = params.get("expectation")
            if expectation is not None:
                if not isinstance(expectation, str) or not _EXPECTATION_RE.match(expectation):
                    raise ParameterError("expectation", "must name a GX expectation (expect_...)")
                out["expectation"] = expectation
            elif strict:
                raise ParameterError("expectation", "is required")
            for k, v in params.items():
                if k in ("expectation", "threshold"):
                    continue
                if not _KWARG_RE.match(str(k)):
                    raise ParameterError(str(k), "is not a valid expectation kwarg name")
                if not _scalar_or_list(v):
                    raise ParameterError(str(k), "must be a scalar or a list of scalars")
                if k == "column" and not isinstance(v, str):
                    raise ParameterError("column", "must be a string")
                out[k] = v
    return out


def _scalar_or_list(v: Any) -> bool:
    def scalar(x: Any) -> bool:
        if isinstance(x, float):
            return math.isfinite(x)
        if isinstance(x, str):
            try:
                validate_literal_text(x)
            except RenderSecurityError:
                return False
            return True
        return x is None or isinstance(x, (bool, int))
    if isinstance(v, list):
        return len(v) <= MAX_ACCEPTED_VALUES and all(scalar(x) for x in v)
    return scalar(v)


def effective_parameters(rule_type: str, rule_params: dict | None,
                         overrides: dict | None, *, strict: bool = True) -> dict[str, Any]:
    return validate_parameters(rule_type, {**(rule_params or {}), **(overrides or {})},
                               strict=strict)
