"""
Deterministic schema inference from a batch of sample records.

Every rule below is explicit and order-independent (running the same sample
set through this module twice, or in a different record order, always
produces the same result) — the previous implementation resolved a field's
type by plurality vote (`Counter.most_common`), which is order-dependent on
ties and had no defined behavior for nesting, arrays, numeric precision, or
timestamps beyond a single `datetime.fromisoformat` guess.

Rules:

1. Sample/payload size: this module has no opinion — it infers whatever
   `records` it's given. Callers (schema_service.infer) are responsible for
   enforcing settings.schema_infer_max_records / schema_infer_max_payload_bytes
   before calling in, since those are request-shape concerns, not inference
   concerns (mirrors the quality preview/execution-job convention).

2. Nested depth: an object's own fields, or an array's element type, are
   inferred recursively up to settings.schema_infer_max_depth levels. Beyond
   that ceiling, the field is reported as a bare "object"/"array" with no
   `fields`/`items` — never silently truncated, never recursed without
   bound (unbounded recursion on adversarial deeply-nested JSON is itself a
   resource-exhaustion risk).

3. Array handling: every element observed across every array instance for a
   field is flattened into one pool and inferred as a single `items` schema,
   using the exact same type-combination rule as a top-level field (rule 5).
   An array field that never contained any elements gets a null-only `items`
   placeholder (rule 8).

4. Shape conflicts: if a field is an object in some records/elements and a
   list or scalar in others (or a list in some and a scalar in others), the
   shapes cannot be reconciled into one nested schema — the field falls back
   to "string" (the universal fallback, same as rule 5's scalar conflicts).

5. Mixed-type (scalar) conflict rules: every non-null scalar value's type is
   combined pairwise via `_combine_types`, which is commutative and
   associative (so the result never depends on record order):
     - identical types combine to themselves.
     - integer/number/decimal widen along that order (decimal is the most
       general numeric type here — see rule 6).
     - date/timestamp widen to timestamp (the more general of the two).
     - any other combination (e.g. boolean vs string, string vs date)
       collapses to "string" — the safe, universal representation.

6. Decimal precision/scale: only actual `decimal.Decimal` values are typed
   "decimal" (a JSON/HTTP payload never round-trips one — this matters for
   internal callers, e.g. DB introspection, that pass real Decimal values
   from a NUMERIC/DECIMAL source column). `precision` (total significant
   digits) and `scale` (digits right of the point) are each the max observed
   across every decimal-typed value for that field, and precision is capped
   at settings.schema_infer_decimal_max_precision. A field that mixes
   decimals with plain ints/floats still combines to "decimal" (rule 5), but
   only the decimal-typed observations contribute to precision/scale.

7. Timestamp/date detection: a string is "date" if it matches
   `^\\d{4}-\\d{2}-\\d{2}$` exactly, or "timestamp" if it matches
   `^\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:?\\d{2})?$`
   exactly; otherwise it's "string". This is a pure shape check (it does not
   validate the value is a real calendar date/time, e.g. "2024-13-40" still
   matches "date") — deliberately, so the rule has no Python-version-
   dependent calendar-parsing behavior baked in.

8. Null-only fields: a field that is None in every record it appears in has
   no observations to type from, so it's reported as "string" (the same
   universal-fallback type used for shape/scalar conflicts) with an explicit
   `null_only=True` flag, distinguishing "we have no idea, this is a
   placeholder" from an actual observed string field.
"""

import re
from decimal import Decimal
from functools import reduce
from typing import Any

from app.core.config import settings

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:?\d{2})?$")

_NUMERIC_ORDER = ("integer", "number", "decimal")
_TEMPORAL_ORDER = ("date", "timestamp")


def infer_schema(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"fields": [], "confidence": 0.0, "sample_size": 0}

    field_names = sorted({key for record in records for key in record.keys()})
    fields = [
        _infer_field(name, [record.get(name) for record in records], depth=0) for name in field_names
    ]
    confidence = round(sum(f["confidence"] for f in fields) / len(fields), 2) if fields else 0.0
    return {"fields": fields, "confidence": confidence, "sample_size": len(records)}


def _empty_field(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "string",
        "nullable": True,
        "primary_key": False,
        "confidence": 0.0,
        "null_only": True,
        "fields": None,
        "items": None,
        "precision": None,
        "scale": None,
    }


def _infer_field(name: str, values: list[Any], *, depth: int) -> dict[str, Any]:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return _empty_field(name)  # rule 8

    field: dict[str, Any] = {
        "name": name,
        "nullable": len(non_null) < len(values),
        "primary_key": False,
        "confidence": round(len(non_null) / len(values), 2),
        "null_only": False,
        "fields": None,
        "items": None,
        "precision": None,
        "scale": None,
    }

    is_object = all(isinstance(v, dict) for v in non_null)
    is_array = all(isinstance(v, list) for v in non_null)
    can_recurse = depth < settings.schema_infer_max_depth

    if is_object:
        field["type"] = "object"
        if can_recurse:  # rule 2
            child_names = sorted({key for v in non_null for key in v.keys()})
            field["fields"] = [
                _infer_field(child, [v.get(child) for v in non_null], depth=depth + 1)
                for child in child_names
            ]
        return field

    if is_array:
        field["type"] = "array"
        if can_recurse:  # rule 2 + 3
            elements = [element for v in non_null for element in v]
            field["items"] = (
                _infer_field("items", elements, depth=depth + 1) if elements else _empty_field("items")
            )
        return field

    # Scalar values, or a shape conflict (some values object/array, others
    # not) — rule 4 folds shape conflicts into the same "string" fallback
    # that rule 5 uses for scalar type conflicts.
    scalar_types: set[str] = set()
    decimal_observations: list[dict[str, int]] = []
    for value in non_null:
        if isinstance(value, dict) or isinstance(value, list):
            scalar_types.add("string")
            continue
        detected, meta = _detect_scalar_type(value)
        scalar_types.add(detected)
        if detected == "decimal":
            decimal_observations.append(meta)

    field["type"] = reduce(_combine_types, scalar_types)
    if field["type"] == "decimal" and decimal_observations:
        field["precision"] = min(
            max(obs["precision"] for obs in decimal_observations),
            settings.schema_infer_decimal_max_precision,
        )
        field["scale"] = max(obs["scale"] for obs in decimal_observations)
    return field


def _detect_scalar_type(value: Any) -> tuple[str, dict[str, int]]:
    if isinstance(value, bool):
        return "boolean", {}
    if isinstance(value, Decimal):
        _sign, digits, exponent = value.as_tuple()
        scale = max(-exponent, 0) if isinstance(exponent, int) else 0
        return "decimal", {"precision": len(digits), "scale": scale}
    if isinstance(value, int):
        return "integer", {}
    if isinstance(value, float):
        return "number", {}
    if isinstance(value, str):
        if _DATE_RE.match(value):
            return "date", {}
        if _TIMESTAMP_RE.match(value):
            return "timestamp", {}
        return "string", {}
    return "string", {}


def _combine_types(a: str, b: str) -> str:
    """Commutative, associative type-combination rule (rule 5) — the result
    of folding this over any set of observed types never depends on order."""
    if a == b:
        return a
    if a in _NUMERIC_ORDER and b in _NUMERIC_ORDER:
        return _NUMERIC_ORDER[max(_NUMERIC_ORDER.index(a), _NUMERIC_ORDER.index(b))]
    if a in _TEMPORAL_ORDER and b in _TEMPORAL_ORDER:
        return _TEMPORAL_ORDER[max(_TEMPORAL_ORDER.index(a), _TEMPORAL_ORDER.index(b))]
    return "string"
