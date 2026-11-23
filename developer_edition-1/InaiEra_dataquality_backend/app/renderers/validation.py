"""Structural validation of COMPILED artifacts before they are stored as
valid or applied (DQ-SEC-006 "compiled artifacts validated before apply").

These validators check DQ's own output against a strict allowlist of the
shapes the renderers emit — a renderer bug or a gap in a per-field guard
cannot smuggle an extra statement, an unexpected table, a foreign topic or
an unknown dbt test into an artifact that will run under engine credentials.
They never touch parent-job SQL (that only ever arrives as the runner's
structured plan).
"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

from app.renderers.security import RenderSecurityError, validate_custom_sql_condition

_ALLOWED_DBT_TESTS = {"not_null", "unique", "accepted_values", "dbt_utils.expression_is_true",
                      "dbt_utils.recency"}
_SEVERITY_FOR_MODE = {"blocking": "error", "warn": "warn", "async": "warn"}
_QUOTED_IDENT_RE = re.compile(r'^"(?:[^"]|"")+"$')


def validate_dbt_artifact(text: str, *, model_name: str, enforcement_mode: str) -> list[str]:
    errors: list[str] = []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"dbt artifact is not valid YAML: {e}"]
    if not isinstance(doc, dict) or set(doc) != {"version", "models"} or doc.get("version") != 2:
        return ["dbt artifact must be exactly {version: 2, models: [...]}"]
    models = doc["models"]
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        return ["dbt artifact must describe exactly one model"]
    model = models[0]
    if model.get("name") != model_name:
        errors.append(f"dbt artifact describes model {model.get('name')!r}, expected {model_name!r}")
    if set(model) - {"name", "tests", "columns"}:
        errors.append(f"unexpected model keys {sorted(set(model) - {'name', 'tests', 'columns'})}")
    tests = list(model.get("tests") or [])
    for col in model.get("columns") or []:
        if not isinstance(col, dict) or set(col) - {"name", "tests", "quote"}:
            errors.append("unexpected column entry shape")
            continue
        tests.extend(col.get("tests") or [])
    want = _SEVERITY_FOR_MODE[enforcement_mode]
    for t in tests:
        if not isinstance(t, dict) or len(t) != 1:
            errors.append("each test must be a single-key mapping")
            continue
        (name, body), = t.items()
        if name not in _ALLOWED_DBT_TESTS:
            errors.append(f"test {name!r} is not an allowed DQ test")
            continue
        if not isinstance(body, dict) or "dq_rule" not in (body.get("meta") or {}):
            errors.append(f"test {name!r} lacks meta.dq_rule")
            continue
        if (body.get("config") or {}).get("severity") != want:
            errors.append(f"test {name!r} severity does not match enforcement_mode "
                          f"{enforcement_mode!r}")
        if enforcement_mode == "async" and "dq_async" not in ((body.get("config") or {})
                                                              .get("tags") or []):
            errors.append(f"async test {name!r} is not tagged dq_async")
        if "expression" in body:
            try:
                validate_custom_sql_condition(body["expression"])
            except RenderSecurityError as e:
                errors.append(f"expression rejected: {e}")
        if name == "dbt_utils.recency" and not _QUOTED_IDENT_RE.match(str(body.get("field", ""))):
            errors.append("recency field must be a quoted identifier")
    return errors


# ── FlinkSQL ───────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"\s+|--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|`(?:[^`]|``)*`|[A-Za-z_][A-Za-z0-9_$]*|\$\{|.",
    re.DOTALL)
# Words that only ever begin a statement DQ never emits (statement SHAPES
# are additionally allowlisted below, which covers CREATE FUNCTION etc.).
_FORBIDDEN_FLINK_WORDS = {
    "drop", "alter", "delete", "update", "truncate", "grant", "revoke", "load", "unload",
    "jar", "catalog", "database", "module", "call", "reset", "use", "show", "describe",
    "explain", "merge", "upsert",
}
_VIEW_RE = re.compile(r"^dq_check_\d+$")


def _tokens(sql: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(sql):
        m = _TOKEN_RE.match(sql, pos)
        if not m:
            raise ValueError(f"untokenizable input at {pos}")
        tok = m.group(0)
        pos = m.end()
        if tok.isspace() or tok.startswith("--") or tok.startswith("/*"):
            continue
        if tok.startswith("'"):
            out.append(("str", tok[1:-1].replace("''", "'")))
        elif tok.startswith("`"):
            out.append(("ident", tok[1:-1].replace("``", "`")))
        elif tok[0].isalpha() or tok[0] == "_":
            out.append(("word", tok.lower()))
        else:
            out.append(("punct", tok))
    return out


def validate_flink_artifact(sql: str, *, sinks: dict[str, str], topics: dict[str, str]) -> list[str]:
    """sinks: sink table -> contract topic key; topics: key -> topic name."""
    errors: list[str] = []
    try:
        toks = _tokens(sql)
    except ValueError as e:
        return [f"FlinkSQL artifact could not be tokenized: {e}"]
    if any(t == ("punct", "'") for t in toks):
        return ["unterminated string literal"]

    for i, (kind, val) in enumerate(toks):
        if kind == "word" and val in _FORBIDDEN_FLINK_WORDS:
            errors.append(f"disallowed keyword {val.upper()!r} in FlinkSQL artifact")
        if kind == "word" and val == "set" and not (i > 0 and toks[i - 1] == ("word", "statement")):
            errors.append("disallowed SET statement in FlinkSQL artifact")

    statements: list[list[tuple[str, str]]] = [[]]
    for t in toks:
        if t == ("punct", ";"):
            statements.append([])
        else:
            statements[-1].append(t)
    statements = [s for s in statements if s]

    created: dict[str, list[tuple[str, str]]] = {}
    inserted: set[str] = set()
    for st in statements:
        words = [v for k, v in st[:6] if k == "word"]
        if st[:2] == [("word", "create"), ("word", "table")] and len(st) > 2 and st[2][0] == "ident":
            created[st[2][1]] = st
        elif st[:3] == [("word", "create"), ("word", "temporary"), ("word", "view")] \
                and len(st) > 3 and st[3][0] == "ident" and _VIEW_RE.match(st[3][1]):
            if any(t == ("word", "insert") for t in st):
                errors.append("INSERT inside a view definition")
        elif words[:4] == ["execute", "statement", "set", "begin"] or st[:2] == [
                ("word", "insert"), ("word", "into")]:
            idx = next(i for i, t in enumerate(st) if t == ("word", "into"))
            target = st[idx + 1] if idx + 1 < len(st) else ("", "")
            if target[0] != "ident" or target[1] not in sinks:
                errors.append(f"INSERT INTO non-contract table {target[1]!r}")
            else:
                inserted.add(target[1])
        elif st == [("word", "end")]:
            pass
        else:
            errors.append(f"unexpected statement starting {' '.join(v for _, v in st[:4])!r}")

    allowed_tables = {"dq_source", *sinks}
    for name in created:
        if name not in allowed_tables:
            errors.append(f"CREATE TABLE {name!r} is not an allowed quality-job table")
    if "dq_source" not in created:
        errors.append("missing dq_source table")
    for sink, topic_key in sinks.items():
        st = created.get(sink)
        if st is None:
            errors.append(f"missing sink table {sink!r}")
            continue
        opts = {st[i][1]: st[i + 2][1] for i in range(len(st) - 2)
                if st[i][0] == "str" and st[i + 1] == ("punct", "=") and st[i + 2][0] == "str"}
        if opts.get("topic") != topics[topic_key]:
            errors.append(f"sink {sink!r} writes topic {opts.get('topic')!r}, contract requires "
                          f"{topics[topic_key]!r}")
        if opts.get("connector") != "kafka":
            errors.append(f"sink {sink!r} is not a kafka sink")
        if sink not in inserted:
            errors.append(f"sink {sink!r} is never written")
    return errors


# ── Python ─────────────────────────────────────────────────────────────

_EXPECTATION_RE = re.compile(r"^expect_[a-z0-9_]+$")


def validate_python_artifacts(gx: dict | None, soda: str | None) -> list[str]:
    errors: list[str] = []
    if gx is not None:
        try:
            json.dumps(gx)
        except (TypeError, ValueError) as e:
            errors.append(f"GX suite is not JSON-serializable: {e}")
        for e in gx.get("expectations", []):
            if not _EXPECTATION_RE.match(str(e.get("expectation_type", ""))):
                errors.append(f"invalid expectation_type {e.get('expectation_type')!r}")
        if gx.get("meta", {}).get("on_failure") not in ("block", "warn", "async"):
            errors.append("GX suite lacks a valid meta.on_failure")
    if soda is not None:
        try:
            doc: Any = yaml.safe_load(soda)
        except yaml.YAMLError as e:
            return errors + [f"Soda checks are not valid YAML: {e}"]
        if not isinstance(doc, dict) or len(doc) != 1 or not str(next(iter(doc))).startswith(
                "checks for "):
            errors.append("Soda document must have exactly one `checks for <table>` key")
    return errors
