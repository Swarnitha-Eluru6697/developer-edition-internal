"""Splitting a SQL script into statements, shared by both SQL executors."""
from __future__ import annotations


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements: strips -- and /* */ comments (so a
    commented-out query never runs) and splits on ';' outside string literals
    and double-quoted identifiers. Same rules as the lakehouse service."""
    stmts: list[str] = []
    cur: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            while i < n:
                cur.append(sql[i])
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:   # escaped '' / ""
                        cur.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == ";":
            s = "".join(cur).strip()
            if s:
                stmts.append(s)
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    s = "".join(cur).strip()
    if s:
        stmts.append(s)
    return stmts
