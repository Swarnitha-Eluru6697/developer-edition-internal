"""Structured SQL plan extraction (DQ-FLINK-008).

Real Flink SQL parsing lives in the Flink SQL Gateway (a full Calcite
grammar) — this module does not reimplement that grammar, and nothing
here is used to decide whether a job's SQL is valid (validate_service's
EXPLAIN/execute round-trip through the Gateway still owns that). This
extracts exactly the structural facts a downstream caller needs to
reconstruct a source table — name, column list, WITH-clause connector
options — using a comment/quote/bracket-depth-aware scanner instead of a
single-shot regex.

Why a regex isn't enough: a non-greedy pattern like `\\((.*?)\\)` stops at
the FIRST closing paren it finds, so it silently truncates the moment a
column list or WITH clause contains ANY nested parens or angle brackets
(`ROW<a INT, b ARRAY<STRING>>`, `DECIMAL(10,2)`), a quoted identifier, or a
comment containing literal parens/keywords. It also has no notion of "this
table is read from" vs "this table is only ever written to" (so a script
with a Kafka source AND a Kafka sink can hand back the wrong one), or
"this table was never CREATEd here at all" (a catalog-defined table
referenced by name, with no local DDL to parse). Every function below
walks actual bracket/quote state instead of guessing where a clause ends.
"""
from __future__ import annotations
import re

_STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'")

_CREATE_TABLE_START_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(`[^`]+`|\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.IGNORECASE,
)
_WITH_START_RE = re.compile(r"\)\s*WITH\s*\(", re.IGNORECASE)
_INSERT_INTO_RE = re.compile(
    r"INSERT\s+INTO\s+(`[^`]+`|\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE,
)
_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(`[^`]+`|\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE,
)
_COLUMN_ENTRY_RE = re.compile(r"^(`[^`]+`|\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)\s+(.+)$", re.DOTALL)
_OPTION_ENTRY_RE = re.compile(r"^'((?:[^'\\]|\\.)*)'\s*=\s*'((?:[^'\\]|\\.)*)'$", re.DOTALL)
# Table-level constraints/watermarks share the column list's comma-separated
# slots but aren't columns — skipped by their leading keyword.
_NON_COLUMN_KEYWORDS = {"PRIMARY", "WATERMARK", "CONSTRAINT", "UNIQUE"}


def strip_comments(sql: str) -> str:
    """Remove `--` line comments and `/* */` block comments, without
    touching matching text that happens to appear inside a single-quoted
    string literal (e.g. a Kafka topic name containing `--`)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            m = _STRING_RE.match(sql, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        if sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _unquote(name: str) -> str:
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "`\"":
        return name[1:-1]
    return name


def _find_matching_close_paren(text: str, open_idx: int) -> int:
    """Return the index of the `)` matching the `(` at `open_idx`.

    Tracks `(`/`)` AND Flink's `<`/`>` type-parameter brackets in the same
    depth counter — every `<` in a type expression (ROW<...>, ARRAY<...>,
    MAP<...>) is closed by a `>` before the column list's own `)`, so this
    still lands on the correct outer close. String literals are skipped
    whole so a stray `)` or `>` inside one (e.g. an option value) can never
    be mistaken for structure.
    """
    depth = 0
    i, n = open_idx, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            m = _STRING_RE.match(text, i)
            i = m.end() if m else i + 1
            continue
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
            if depth == 0 and ch == ")":
                return i
        i += 1
    raise ValueError("unbalanced parentheses")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split `text` on top-level `,` — commas nested inside `()`/`<>` or a
    string literal (e.g. `DECIMAL(10,2)` or an option value) don't count."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            m = _STRING_RE.match(text, i)
            if m:
                buf.append(m.group(0))
                i = m.end()
                continue
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_columns(column_list_text: str) -> list[dict]:
    columns = []
    for entry in _split_top_level(column_list_text):
        m = _COLUMN_ENTRY_RE.match(entry.strip())
        if not m:
            continue
        raw_name, rest = m.group(1), m.group(2).strip()
        name = _unquote(raw_name)
        if name.upper() in _NON_COLUMN_KEYWORDS:
            continue  # a table constraint/watermark, not a column
        # The type is whatever precedes NOT NULL / COMMENT '...' / a
        # computed column's AS <expr> — none of those are part of the type.
        type_text = re.split(r"\bAS\b", rest, maxsplit=1, flags=re.IGNORECASE)[0]
        type_text = re.sub(r"\bCOMMENT\s+'(?:[^'\\]|\\.)*'", "", type_text, flags=re.IGNORECASE)
        type_text = re.sub(r"\bNOT\s+NULL\b", "", type_text, flags=re.IGNORECASE).strip()
        if type_text:
            columns.append({"name": name, "type": type_text})
    return columns


def _parse_options(options_text: str) -> dict[str, str]:
    options = {}
    for entry in _split_top_level(options_text):
        m = _OPTION_ENTRY_RE.match(entry.strip())
        if m:
            options[m.group(1)] = m.group(2)
    return options


def extract_create_tables(sql: str) -> list[dict]:
    """Return every `CREATE TABLE` in `sql` as `{name, columns, options}`,
    walking actual bracket depth instead of guessing where each clause
    ends (see module docstring)."""
    clean = strip_comments(sql)
    tables = []
    for m in _CREATE_TABLE_START_RE.finditer(clean):
        name = _unquote(m.group(1))
        col_open = m.end() - 1
        try:
            col_close = _find_matching_close_paren(clean, col_open)
        except ValueError:
            continue
        columns = _parse_columns(clean[col_open + 1:col_close])

        options: dict[str, str] = {}
        with_m = _WITH_START_RE.match(clean, col_close)
        if with_m:
            opt_open = with_m.end() - 1
            try:
                opt_close = _find_matching_close_paren(clean, opt_open)
                options = _parse_options(clean[opt_open + 1:opt_close])
            except ValueError:
                pass
        tables.append({"name": name, "columns": columns, "options": options})
    return tables


def extract_plan(sql: str) -> dict:
    """Partition every table referenced in `sql` into:

      sources           — CREATEd tables read from (a FROM/JOIN target,
                           or a table CREATEd but never referenced by
                           either an INSERT or a FROM/JOIN — kept as a
                           source, matching this contract's one reasonable
                           default for a declared-but-unreferenced table).
      sinks             — CREATEd tables written to (an INSERT INTO target).
      unresolved_tables — referenced (as a source or sink) but never
                          CREATEd anywhere in this script — e.g. a
                          catalog-defined table with no inline DDL to parse
                          at all. Callers must not assume a structured
                          schema/options exists for these.

    A table referenced as BOTH an INSERT INTO target and a FROM/JOIN
    source (self-referential SQL) is classified as a sink only — the
    common, deliberate case (a table this job writes to) outranks an
    incidental read for source-selection purposes.
    """
    clean = strip_comments(sql)
    defs = {t["name"]: t for t in extract_create_tables(sql)}
    sink_names = {_unquote(m.group(1)) for m in _INSERT_INTO_RE.finditer(clean)}
    source_names = {_unquote(m.group(1)) for m in _FROM_JOIN_RE.finditer(clean)} - sink_names

    sources: list[dict] = []
    sinks: list[dict] = []
    unresolved: list[str] = []
    seen: set[str] = set()

    for name in source_names:
        seen.add(name)
        if name in defs:
            sources.append(defs[name])
        else:
            unresolved.append(name)
    for name in sink_names:
        seen.add(name)
        if name in defs:
            sinks.append(defs[name])
        else:
            unresolved.append(name)
    for name, table in defs.items():
        if name not in seen:
            sources.append(table)
    return {"sources": sources, "sinks": sinks, "unresolved_tables": sorted(set(unresolved))}


_KAFKA_CONNECTORS = ("kafka", "upsert-kafka")


def kafka_consumer_groups(sql: str) -> list[str]:
    """The Kafka consumer group(s) this script's Kafka SOURCE tables read
    with — their 'properties.group.id' option (RUN-FLINK-015). Pass the
    placeholder-resolved SQL (what is actually submitted). Order-preserving,
    de-duplicated; [] when no Kafka source declares a group."""
    groups: list[str] = []
    for table in extract_plan(sql)["sources"]:
        opts = table.get("options") or {}
        group = opts.get("properties.group.id")
        if opts.get("connector") in _KAFKA_CONNECTORS and group and group not in groups:
            groups.append(group)
    return groups


def normalized_sql_hash(sql: str) -> str:
    """sha256 of the SQL with comments removed and whitespace collapsed —
    stable across formatting-only edits, different for any real change."""
    import hashlib
    normalized = " ".join(strip_comments(sql).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def structured_plan(sql: str) -> dict:
    """The StructuredJobPlan returned by /api/v1/validate (DQ-FLINK-008)."""
    from app.core.config import get_settings
    settings = get_settings()
    plan = extract_plan(sql)
    return {
        "plan_version": "1", "engine": "flink", "engine_version": settings.flink_version,
        "normalized_sql_hash": normalized_sql_hash(sql),
        "sources": plan["sources"], "sinks": plan["sinks"],
        "unresolved_tables": plan["unresolved_tables"],
        "quality_contract": {
            "version": settings.quality_contract_version,
            "topics": {"results": settings.quality_results_topic,
                       "gate": settings.quality_gate_topic,
                       "quarantine": settings.quality_quarantine_topic},
        },
    }
