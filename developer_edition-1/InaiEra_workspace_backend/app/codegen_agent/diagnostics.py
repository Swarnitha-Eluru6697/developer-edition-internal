"""Error normalisation (§9).

Turns the messy, engine-specific error strings that come back from the
lakehouse EXPLAIN / dbt validate / flink validate / python compile checks into
normalised Diagnostic objects with an error_class and a route. This is the
seam that makes "route the failure to the stage that caused it" work: the
repair controller never parses stderr, it reads Diagnostic.route_to.

Heuristic pattern-matching handles the common cases cheaply. Anything that
doesn't match a known pattern falls through to UNKNOWN -> GENERATE, which is
the safe default (retry generation with the raw error attached).
"""
from __future__ import annotations

import re

from app.codegen_agent.contracts import Diagnostic, ErrorClass

# Patterns are ordered most-specific first. Each maps a regex over the raw
# engine message to an error class; the offending identifier is pulled from
# the first capture group when present.
_SQL_PATTERNS: list[tuple[re.Pattern[str], ErrorClass]] = [
    (re.compile(r"column ['\"]?([\w.]+)['\"]? cannot be resolved", re.I), ErrorClass.SCHEMA_UNKNOWN_IDENTIFIER),
    (re.compile(r"(?:column|field) ['\"]?([\w.]+)['\"]? (?:does not exist|not found|unknown)", re.I), ErrorClass.SCHEMA_UNKNOWN_IDENTIFIER),
    (re.compile(r"(?:table|relation) ['\"]?([\w.]+)['\"]? (?:does not exist|not found)", re.I), ErrorClass.SCHEMA_UNKNOWN_IDENTIFIER),
    (re.compile(r"ambiguous (?:column|reference) ['\"]?([\w.]+)", re.I), ErrorClass.SCHEMA_AMBIGUOUS_REF),
    (re.compile(r"(?:function|Function) ['\"]?([\w.]+)['\"]? not (?:registered|found)", re.I), ErrorClass.DIALECT_UNSUPPORTED_FN),
    (re.compile(r"unexpected (?:token|input)|mismatched input|syntax error|parse error", re.I), ErrorClass.SYNTAX_ERROR),
    (re.compile(r"type mismatch|cannot be applied to|incompatible types|no viable coercion", re.I), ErrorClass.PLAN_TYPE_MISMATCH),
]

_FLINK_PATTERNS: list[tuple[re.Pattern[str], ErrorClass]] = [
    (re.compile(r"unsupported (?:option|property) ['\"]?([\w.]+)", re.I), ErrorClass.CONNECTOR_OPTION_INVALID),
    (re.compile(r"(?:connector|option) ['\"]?([\w.]+)['\"]? (?:is not|unknown|invalid)", re.I), ErrorClass.CONNECTOR_OPTION_INVALID),
    (re.compile(r"changelog mode|does not support (?:update|delete)|append.only", re.I), ErrorClass.CHANGELOG_MODE_INCOMPATIBLE),
    (re.compile(r"table ['\"]?([\w.]+)['\"]? (?:not found|does not exist)", re.I), ErrorClass.SCHEMA_UNKNOWN_IDENTIFIER),
]


def normalise_sql_error(raw: str, *, rung: str = "L2") -> Diagnostic:
    """Normalise a Trino/PG EXPLAIN or dbt compile error."""
    return _match(raw, _SQL_PATTERNS, rung=rung, fallback=ErrorClass.SYNTAX_ERROR)


def normalise_flink_error(raw: str, *, rung: str = "L2") -> Diagnostic:
    return _match(raw, _FLINK_PATTERNS + _SQL_PATTERNS, rung=rung, fallback=ErrorClass.SYNTAX_ERROR)


def _match(
    raw: str,
    patterns: list[tuple[re.Pattern[str], ErrorClass]],
    *,
    rung: str,
    fallback: ErrorClass,
) -> Diagnostic:
    msg = (raw or "").strip()
    for pattern, cls in patterns:
        m = pattern.search(msg)
        if m:
            ident = m.group(1) if m.groups() else None
            ctx = [ident] if ident else []
            return Diagnostic.of(
                rung=rung,
                error_class=cls,
                message=msg[:500],
                offending_identifier=ident,
                suggested_context=ctx,
            )
    return Diagnostic.of(rung=rung, error_class=fallback, message=msg[:500] or "validation failed")
