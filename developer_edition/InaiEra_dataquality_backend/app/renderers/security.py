"""Injection safety for generated artifacts (DQ-SEC-006).

Three kinds of caller-controlled text reach generated dbt YAML / SQL and
FlinkSQL, each with its own engine-specific treatment:

  IDENTIFIERS (column / model / table names used as references)
    Always QUOTED for the target engine, never pasted raw:
      dbt/ANSI SQL  "name"   (embedded " doubled)
      FlinkSQL      `name`   (embedded ` doubled)
    Control characters / NUL and over-long names are rejected outright.
    dbt YAML carries the bare name as a YAML value (yaml.safe_dump) plus
    `quote: true` whenever the name is not a plain lower-case identifier.

  LITERALS (regex patterns, accepted values, FQNs and labels reported as data)
    Rendered by type (render_sql_literal): bool -> TRUE/FALSE, finite
    int/float -> numeric text, str -> single-quoted with ' doubled.
    Control characters are rejected (they have no legitimate place in a
    pattern/value and are the classic way to break out of comments).

  custom_sql CONDITIONS (a user-authored boolean predicate)
    Cannot be escaped without destroying it, so it is (a) privileged —
    dq.custom_sql.write permission + explicit review acknowledgment, stored
    as review_status='approved' (app.services.rule_service) — and (b)
    lexically validated here: string/identifier quoting must be balanced,
    parentheses balanced, and outside quotes there may be no statement
    separator, comment marker, or statement/sub-query keyword. Re-checked at
    every compile, and the compiled artifact is structurally validated
    before apply (app.renderers.validation).
"""
from __future__ import annotations

import math
import re
from typing import Any

MAX_IDENTIFIER_LENGTH = 255
MAX_LITERAL_LENGTH = 4000
MAX_CUSTOM_SQL_LENGTH = 2000

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PLAIN_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_FQN_PART_RE = re.compile(r"^[A-Za-z0-9_$\-]+$")


class RenderSecurityError(ValueError):
    """Base class for every rejection in this module."""


class UnsafeIdentifierError(RenderSecurityError):
    pass


class UnsafeLiteralError(RenderSecurityError):
    pass


class UnsafeCustomSqlError(RenderSecurityError):
    pass


class InvalidNumericParameterError(RenderSecurityError):
    pass


# ── identifiers ─────────────────────────────────────────────────────────

def validate_identifier(name: Any, *, field: str = "identifier") -> str:
    if not isinstance(name, str) or not name or not name.strip():
        raise UnsafeIdentifierError(f"{field} must be a non-empty string")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise UnsafeIdentifierError(f"{field} is longer than {MAX_IDENTIFIER_LENGTH} characters")
    if _CONTROL_RE.search(name):
        raise UnsafeIdentifierError(f"{field} {name!r} contains control characters")
    return name


def is_plain_identifier(name: str) -> bool:
    return bool(_PLAIN_IDENTIFIER_RE.match(name))


def quote_sql_identifier(name: str, *, field: str = "column_name") -> str:
    """ANSI / dbt (Trino, Postgres) identifier quoting."""
    validate_identifier(name, field=field)
    return '"' + name.replace('"', '""') + '"'


def quote_flink_identifier(name: str, *, field: str = "column_name") -> str:
    """FlinkSQL identifier quoting."""
    validate_identifier(name, field=field)
    return "`" + name.replace("`", "``") + "`"


def quote_sql_qualified(name: str, *, field: str = "target_table") -> str:
    """Each dot-separated part quoted separately (catalog.schema.table)."""
    validate_identifier(name, field=field)
    return ".".join(quote_sql_identifier(p, field=field) for p in name.split("."))


def validate_fqn(fqn: Any, *, field: str = "ucs_fqn") -> str:
    """UCS FQN: 2-4 dot-separated parts of [A-Za-z0-9_$-]."""
    if not isinstance(fqn, str):
        raise UnsafeIdentifierError(f"{field} must be a string")
    parts = fqn.split(".")
    if not 2 <= len(parts) <= 4 or not all(_FQN_PART_RE.match(p or "") for p in parts):
        raise UnsafeIdentifierError(
            f"{field} {fqn!r} is not a valid FQN (2-4 dot-separated parts of letters, digits, "
            "'_', '-', '$')")
    return fqn


# ── literals ────────────────────────────────────────────────────────────

def validate_literal_text(value: Any, *, field: str = "value") -> str:
    if not isinstance(value, str):
        raise UnsafeLiteralError(f"{field} must be a string")
    if len(value) > MAX_LITERAL_LENGTH:
        raise UnsafeLiteralError(f"{field} is longer than {MAX_LITERAL_LENGTH} characters")
    if _CONTROL_RE.search(value):
        raise UnsafeLiteralError(f"{field} contains control characters")
    return value


def escape_sql_literal(value: Any, *, field: str = "value") -> str:
    """Body of a single-quoted SQL / FlinkSQL string literal."""
    return validate_literal_text(str(value) if not isinstance(value, str) else value,
                                 field=field).replace("'", "''")


def sql_string(value: Any, *, field: str = "value") -> str:
    return "'" + escape_sql_literal(value, field=field) + "'"


def validate_numeric(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidNumericParameterError(
            f"{field} must be a number, got {type(value).__name__}: {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidNumericParameterError(f"{field} must be finite")
    return value


def render_sql_literal(value: Any, *, field: str = "value") -> str:
    """Typed literal for SQL / FlinkSQL."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(validate_numeric(value, field=field))
    if isinstance(value, str):
        return sql_string(value, field=field)
    raise UnsafeLiteralError(f"{field} has unsupported type {type(value).__name__}")


def comment_safe(text: Any) -> str:
    """Text placed in a `--` comment: single line, no comment terminators."""
    return " ".join(str(text).splitlines()).replace("*/", "* /").replace("\x00", "")


# ── custom_sql ──────────────────────────────────────────────────────────

_FORBIDDEN_KEYWORDS = frozenset({
    "select", "from", "join", "union", "intersect", "except", "with", "into", "values",
    "table", "lateral", "insert", "update", "delete", "merge", "upsert", "drop", "create",
    "alter", "truncate", "grant", "revoke", "exec", "execute", "call", "copy", "vacuum",
    "analyze", "set", "use", "load", "unload", "show", "describe", "explain", "commit",
    "rollback", "begin", "declare", "attach", "detach", "pragma",
})
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def validate_custom_sql_condition(condition: Any) -> str:
    """Lexical validation of a single boolean predicate (see module doc)."""
    if not isinstance(condition, str) or not condition.strip():
        raise UnsafeCustomSqlError("custom_sql condition must be a non-empty string")
    if len(condition) > MAX_CUSTOM_SQL_LENGTH:
        raise UnsafeCustomSqlError(f"custom_sql condition exceeds {MAX_CUSTOM_SQL_LENGTH} characters")
    if "\x00" in condition:
        raise UnsafeCustomSqlError("custom_sql condition contains NUL")
    i, n, depth = 0, len(condition), 0
    while i < n:
        ch = condition[i]
        if ch in "'\"`":
            j = i + 1
            while True:
                k = condition.find(ch, j)
                if k == -1:
                    raise UnsafeCustomSqlError("custom_sql condition has an unterminated quote")
                if k + 1 < n and condition[k + 1] == ch:   # doubled quote = escaped
                    j = k + 2
                    continue
                break
            i = k + 1
            continue
        two = condition[i:i + 2]
        if ch == ";" or two in ("--", "/*", "*/") or ch == "\\":
            bad = two if two in ("--", "/*", "*/") else ch
            raise UnsafeCustomSqlError(
                f"custom_sql condition contains a disallowed construct ({bad!r})"
                " — statement separators, comments and escapes are not permitted in a predicate")
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise UnsafeCustomSqlError("custom_sql condition has unbalanced parentheses")
        m = _WORD_RE.match(condition, i)
        if m and (i == 0 or not (condition[i - 1].isalnum() or condition[i - 1] in "_$")):
            word = m.group(0).lower()
            if word in _FORBIDDEN_KEYWORDS:
                raise UnsafeCustomSqlError(
                    f"custom_sql condition contains a disallowed construct ({m.group(0)!r}) — "
                    "statement and sub-query keywords are not permitted in a rule predicate")
            i = m.end()
            continue
        i += 1
    if depth != 0:
        raise UnsafeCustomSqlError("custom_sql condition has unbalanced parentheses")
    return condition


# A Flink type expression from the runner's structured plan (DECIMAL(10,2),
# ROW<a INT, b ARRAY<STRING>>) — allowlisted characters only.
_FLINK_TYPE_RE = re.compile(r"^[A-Za-z0-9_<>(),.\s`]+$")


def validate_flink_type(type_text: Any, *, field: str = "column_type") -> str:
    if not isinstance(type_text, str) or not type_text.strip() or len(type_text) > 500 \
            or not _FLINK_TYPE_RE.match(type_text):
        raise UnsafeIdentifierError(f"{field} {type_text!r} is not a safe Flink type expression")
    return type_text.strip()
