"""The ONE lexically-aware Flink SQL script splitter (RUN-FLINK-012).

Every place that turns a multi-statement script into statements —
execute_statements() (deploy), validate_service (validate) — uses
split_sql_script() from here. `sql.split(";")` is never acceptable: it
corrupts a `;` inside a string literal ('a;b'), a double-quoted string or
identifier ("a;b"), a backtick identifier (`a;b`), a `--` line comment, a
`/* */` block comment, and it shreds an explicit

    EXECUTE STATEMENT SET BEGIN
      INSERT INTO a ...;
      INSERT INTO b ...;
    END;

into broken pieces. This walks the script tracking all of those and only
treats `;` as a terminator outside every one of them — and, inside a
STATEMENT SET ... BEGIN block, only at the `;` that follows its END.

Not a SQL parser: it only needs to know where statements end.
"""
from __future__ import annotations

import re

_STATEMENT_SET_START_RE = re.compile(r"^\s*(?:EXECUTE\s+)?STATEMENT\s+SET\s+BEGIN\b", re.IGNORECASE)
_ENDS_WITH_END_RE = re.compile(r"\bEND\s*$", re.IGNORECASE)


def split_sql_script(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    # Code-only shadow of `current`: comments dropped, quoted contents
    # blanked — used to recognise STATEMENT SET ... BEGIN / END keywords
    # without being fooled by text inside strings or comments.
    code: list[str] = []
    quote: str | None = None          # one of ' " ` while inside a quoted token
    in_line_comment = False
    in_block_comment = False

    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            current.append(ch)
            if ch == "\n":
                in_line_comment = False
                code.append(" ")
            i += 1
            continue

        if in_block_comment:
            current.append(ch)
            if ch == "*" and nxt == "/":
                current.append(nxt)
                i += 2
                in_block_comment = False
                code.append(" ")
                continue
            i += 1
            continue

        if quote is not None:
            current.append(ch)
            i += 1
            if ch == quote:
                if nxt == quote:      # doubled quote = escaped quote, not the end
                    current.append(nxt)
                    i += 1
                else:
                    quote = None
                    code.append(ch)
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            current.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            current.append(ch)
            current.append(nxt)
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            current.append(ch)
            code.append(ch)
            i += 1
            continue
        if ch == ";":
            code_text = "".join(code)
            if _STATEMENT_SET_START_RE.match(code_text) and not _ENDS_WITH_END_RE.search(code_text):
                # A `;` between statements INSIDE a STATEMENT SET block.
                current.append(ch)
                code.append(ch)
                i += 1
                continue
            stmt = "".join(current).strip()
            if stmt and "".join(code).strip():
                statements.append(stmt)
            current, code = [], []
            i += 1
            continue

        current.append(ch)
        code.append(ch)
        i += 1

    tail = "".join(current).strip()
    if tail and "".join(code).strip():
        statements.append(tail)
    return statements


def strip_sql_comments(stmt: str) -> str:
    """The statement with `--` and `/* */` comments removed (quoted text
    untouched)."""
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(stmt)
    while i < n:
        ch = stmt[i]
        nxt = stmt[i + 1] if i + 1 < n else ""
        if quote is not None:
            out.append(ch)
            if ch == quote:
                if nxt == quote:
                    out.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":
            j = stmt.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and nxt == "*":
            j = stmt.find("*/", i + 2)
            out.append(" ")
            i = n if j == -1 else j + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def leading_keyword(stmt: str) -> str:
    """First SQL keyword of a statement, ignoring leading comments/blanks."""
    body = strip_sql_comments(stmt).strip()
    return body.split()[0].upper() if body else ""


def statement_kind(stmt: str) -> str:
    """Classify a statement: "INSERT", "STATEMENT_SET", "SELECT", or its
    leading keyword. EXECUTE INSERT ... counts as INSERT."""
    words = strip_sql_comments(stmt).strip().upper().split()
    if not words:
        return ""
    if words[0] == "EXECUTE" and len(words) > 1:
        words = words[1:]
    if words[0] == "STATEMENT" and len(words) > 1 and words[1] == "SET":
        return "STATEMENT_SET"
    return words[0]


JOB_SUBMITTING_KINDS = frozenset({"INSERT", "STATEMENT_SET"})


def is_job_submitting(stmt: str) -> bool:
    return statement_kind(stmt) in JOB_SUBMITTING_KINDS


def strip_leading_comments(stmt: str) -> str:
    """Drop leading blank lines and leading comments, keeping the rest intact."""
    s = stmt
    while True:
        t = s.lstrip()
        if t.startswith("--"):
            j = t.find("\n")
            s = "" if j == -1 else t[j + 1:]
            continue
        if t.startswith("/*"):
            j = t.find("*/")
            s = "" if j == -1 else t[j + 2:]
            continue
        return t.strip()
