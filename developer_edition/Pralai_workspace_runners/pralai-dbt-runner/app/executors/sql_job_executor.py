"""SQL jobs: run one .sql file of the repository, statement by statement, against the project's engine.

  pg_lake (Postgres wire)  ONE transaction: every statement commits together, or — on the first error — none does (Postgres DDL
                           is transactional too). The statements after the failing one are reported as skipped.
  trino                    one statement at a time and no transaction (Trino has none across statements): the first error stops
                           the job; what ran before it stays done.
  clickhouse               like trino: one statement at a time over ClickHouse's HTTP interface, no transaction across statements,
                           the first error stops the job and what ran before it stays done.

The result has the shape of a dbt result (success, errors, nodes) so everything after execution — finalize, run events, Dagster,
Dependents — treats a SQL job's run like a dbt run: one node per statement, `statement.<n>`, with its kind and row count.
The engine credentials are the runner's own (the same ones dbt builds with); nothing is read from the SQL file but SQL.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import sqlparse
import structlog

from app.core.config import get_settings
from app.services.data_dependencies import statement_lineage

log = structlog.get_logger()

EXCERPT = 160


def split_statements(sql: str) -> list[str]:
    """The file's statements, in order: blank ones and ones that are only comments are dropped; a trailing ';' is removed."""
    out = []
    for raw in sqlparse.split(sql or ""):
        if not sqlparse.format(raw, strip_comments=True).strip().rstrip(";").strip():
            continue
        out.append(raw.strip().rstrip(";").strip())
    return out


def _kind(statement: str) -> str:
    kind = sqlparse.parse(statement)[0].get_type()
    return kind if kind and kind != "UNKNOWN" else "SQL"


def _excerpt(statement: str) -> str:
    one_line = " ".join(statement.split())
    return one_line if len(one_line) <= EXCERPT else one_line[: EXCERPT - 1] + "…"


def _node(i: int, statement: str, status: str, message: str, seconds: float | None = None) -> dict[str, Any]:
    return {"node_id": f"statement.{i}", "status": status, "message": message, "execution_time": seconds,
            "statement_kind": _kind(statement), "sql": _excerpt(statement)}


def _rows(rowcount: int | None) -> str:
    return "" if rowcount is None or rowcount < 0 else f" · {rowcount} row{'s' if rowcount != 1 else ''}"


def _pg_connect(settings):
    import psycopg2  # type: ignore[import-untyped]  # no type stubs are published for it

    u = urlparse(settings.pg_lake_dsn or "")
    if not u.hostname:
        raise RuntimeError("the pg_lake engine is not configured on this runner (PG_LAKE_DSN)")
    return psycopg2.connect(host=u.hostname, port=u.port or 5432, user=unquote(u.username or ""),
                            password=unquote(u.password or ""), dbname=(u.path or "/").lstrip("/") or "postgres",
                            connect_timeout=30, application_name="inaiera-sql-job")


def _trino_connect(settings):
    import trino

    if not settings.trino_host:
        raise RuntimeError("the Trino engine is not configured on this runner (TRINO_HOST)")
    token = "" if settings.trino_auth_method == "none" else os.environ.get("TRINO_JWT") or ""
    return trino.dbapi.connect(host=settings.trino_host, port=settings.trino_port, catalog=settings.trino_catalog,
                               http_scheme=settings.trino_http_scheme,
                               user=settings.trino_user if settings.trino_auth_method == "none" else "inaiera-sql-job",
                               auth=trino.auth.JWTAuthentication(token) if token else None,
                               request_timeout=settings.sql_job_statement_timeout_seconds)


def _clickhouse_connect(settings):
    if not settings.clickhouse_host:
        raise RuntimeError("the ClickHouse engine is not configured on this runner (CLICKHOUSE_HOST)")
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=settings.clickhouse_host, port=settings.clickhouse_port, secure=settings.clickhouse_secure,
        username=settings.clickhouse_user, password=settings.clickhouse_password or os.environ.get("CLICKHOUSE_PASSWORD") or "",
        database=settings.clickhouse_database, connect_timeout=30, send_receive_timeout=settings.sql_job_statement_timeout_seconds,
        client_name="inaiera-sql-job")


def run_sql_file(*, engine: str, sql_path: Path) -> dict[str, Any]:
    settings = get_settings()
    try:
        statements = split_statements(sql_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return {"success": False, "errors": [f"could not read the SQL file: {e}"], "nodes": []}
    if not statements:
        return {"success": False, "errors": ["the SQL file has no statements"], "nodes": []}
    try:
        if engine == "pg_lake":
            result = _run_postgres(statements, settings)
        elif engine == "trino":
            result = _run_trino(statements, settings)
        elif engine == "clickhouse":
            result = _run_clickhouse(statements, settings)
        else:
            return {"success": False, "errors": [f"unsupported engine for a SQL job: {engine}"], "nodes": []}
        if result.get("success"):
            result["lineage"] = statement_lineage(statements)       # which tables it read and wrote: the data lineage (data_dependencies.py)
        return result
    except Exception as e:  # noqa: BLE001 — a connection problem is the run's failure, with its reason
        log.warning("sql_job_failed_to_start", engine=engine, error=str(e))
        return {"success": False, "errors": [f"could not run the SQL job: {e}"], "nodes": []}


def _run_postgres(statements: list[str], settings) -> dict[str, Any]:
    conn = _pg_connect(settings)
    nodes: list[dict[str, Any]] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s", (int(settings.sql_job_statement_timeout_seconds * 1000),))
            for i, statement in enumerate(statements, start=1):
                started = time.monotonic()
                try:
                    cur.execute(statement)
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    error = str(getattr(e, "pgerror", None) or e).strip()
                    nodes.append(_node(i, statement, "error", error, time.monotonic() - started))
                    nodes += [_node(j, s, "skipped", "not run: an earlier statement failed (nothing was committed)")
                              for j, s in enumerate(statements[i:], start=i + 1)]
                    return {"success": False, "nodes": nodes,
                            "errors": [f"statement {i} failed, nothing was committed: {error}"]}
                nodes.append(_node(i, statement, "success", _kind(statement) + _rows(cur.rowcount), time.monotonic() - started))
        conn.commit()
        return {"success": True, "errors": [], "nodes": nodes}
    finally:
        conn.close()


def _run_trino(statements: list[str], settings) -> dict[str, Any]:
    conn = _trino_connect(settings)
    nodes: list[dict[str, Any]] = []
    try:
        cur = conn.cursor()
        for i, statement in enumerate(statements, start=1):
            started = time.monotonic()
            try:
                cur.execute(statement)
                cur.fetchall()                                  # Trino runs a statement as its result is consumed
            except Exception as e:  # noqa: BLE001
                error = str(e).strip()
                nodes.append(_node(i, statement, "error", error, time.monotonic() - started))
                nodes += [_node(j, s, "skipped", "not run: an earlier statement failed")
                          for j, s in enumerate(statements[i:], start=i + 1)]
                done = f" (statements 1-{i - 1} had already run)" if i > 1 else ""
                return {"success": False, "nodes": nodes, "errors": [f"statement {i} failed{done}: {error}"]}
            nodes.append(_node(i, statement, "success", _kind(statement) + _rows(cur.rowcount), time.monotonic() - started))
        return {"success": True, "errors": [], "nodes": nodes}
    finally:
        conn.close()


def _run_clickhouse(statements: list[str], settings) -> dict[str, Any]:
    client = _clickhouse_connect(settings)
    nodes: list[dict[str, Any]] = []
    try:
        for i, statement in enumerate(statements, start=1):
            started = time.monotonic()
            try:
                summary = client.command(statement)               # DDL, INSERT ... SELECT and plain queries alike
            except Exception as e:  # noqa: BLE001
                error = str(e).strip()
                nodes.append(_node(i, statement, "error", error, time.monotonic() - started))
                nodes += [_node(j, s, "skipped", "not run: an earlier statement failed")
                          for j, s in enumerate(statements[i:], start=i + 1)]
                done = f" (statements 1-{i - 1} had already run)" if i > 1 else ""
                return {"success": False, "nodes": nodes, "errors": [f"statement {i} failed{done}: {error}"]}
            written = getattr(summary, "written_rows", None)       # INSERT and friends return a QuerySummary; other statements a value
            nodes.append(_node(i, statement, "success", _kind(statement) + _rows(written if isinstance(written, int) and written > 0 else None),
                               time.monotonic() - started))
        return {"success": True, "errors": [], "nodes": nodes}
    finally:
        client.close()
