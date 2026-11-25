"""SQL against a registered Postgres / pg_lake DatabaseConnection.

Runs inside the execution worker, which is the only place the connection's
credentials are resolved — just in time, from the secrets backend — and they
never leave this process. Previously the worker built a password-bearing DSN
and POSTed it to the lakehouse service as the query "warehouse", so the
credential crossed a service boundary and landed in another service's
request handling, logs and traces (WS-DATA-007).

Speaks the same chunk protocol as SqlExecutor (schema+rows chunks, then a
final done/error chunk), so the router and UI don't care which ran it.
Scripts run statements in order, autocommit, and EVERY statement's result is
streamed: its rows chunks carry ``statement`` (0-based index; the first also
carries ``columns`` and the statement's ``sql``) and a non-final ``statement``
chunk closes it with its row count / status. A failing statement stops the
script; the error chunk names it, and earlier results are kept.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import decimal
import uuid
from typing import AsyncIterator

import structlog

from app.core.egress_policy import EgressPolicyError, assert_host_allowed
from app.executors.sql_executor import SqlChunk
from app.executors.sql_split import split_statements  # noqa: F401 — re-exported (was defined here)

log = structlog.get_logger()

PG_CONNECTION_TYPES = ("postgres", "pg_lake")


def _json_safe(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    return str(v)


def _error(code: str, message: str, statement: int | None = None) -> SqlChunk:
    payload: dict = {"error_code": code, "error_message": message[:500]}
    if statement is not None:
        payload["statement"] = statement   # which statement of the script failed
    return SqlChunk(output_type="error", payload=payload, row_count=0, is_final=True)


class ConnectionSqlExecutor:
    async def execute(
        self, *, conn, code_text: str, max_rows: int, timeout_seconds: int, chunk_size: int,
    ) -> AsyncIterator[SqlChunk]:
        import asyncpg
        from app.api.catalog import _load_db_creds
        from app.services.catalog_adapter import explain_connect_error

        if conn.connection_type not in PG_CONNECTION_TYPES:
            yield _error("UNSUPPORTED_CONNECTION",
                         f"Running SQL is supported for Postgres connections only, not '{conn.connection_type}'.")
            return
        statements = split_statements(code_text)
        if not statements:
            yield _error("EMPTY_QUERY", "No SQL provided")
            return
        try:
            await assert_host_allowed(conn.host or "")
        except EgressPolicyError as e:
            yield _error("HOST_NOT_PERMITTED", f"Host not permitted: {e.reason}")
            return

        creds = await _load_db_creds(conn.vault_secret_path)
        password = creds.get("password") or ""

        def scrub(text: str) -> str:
            return text.replace(password, "***") if password else text

        try:
            c = await asyncpg.connect(
                host=conn.host or "localhost", port=conn.port or 5432,
                user=creds.get("username") or "postgres", password=password,
                database=conn.database_name or "postgres",
                ssl="require" if conn.use_tls else None, timeout=10,
                # Server-side cap too, so a runaway query stops even if this
                # process dies mid-run.
                server_settings={"statement_timeout": str(timeout_seconds * 1000)},
            )
        except Exception as e:  # noqa: BLE001
            yield _error("CONNECTION_FAILED", scrub(explain_connect_error(e)))
            return

        current = 0          # index of the statement being run, for error attribution
        last_total, last_truncated = 0, False
        try:
            async with asyncio.timeout(timeout_seconds):
                for current, stmt in enumerate(statements):
                    prepared = await c.prepare(stmt)
                    attrs = prepared.get_attributes()
                    if not attrs:
                        await prepared.fetch()  # DDL/DML: run it, no rows
                        status = prepared.get_statusmsg()
                        yield SqlChunk(
                            output_type="statement",
                            payload={"statement": current, "sql": stmt, "total_rows": 0,
                                     "status": str(status) if status else None},
                            row_count=0, is_final=False)
                        last_total, last_truncated = 0, False
                        continue
                    columns = [{"name": a.name, "type": str(a.type.name)} for a in attrs]
                    first = True
                    total = 0
                    async with c.transaction(readonly=False):
                        cursor = await prepared.cursor()
                        while total < max_rows:
                            batch = await cursor.fetch(min(chunk_size, max_rows - total))
                            if not batch:
                                break
                            payload: dict = {"rows": [[_json_safe(v) for v in r.values()] for r in batch],
                                             "statement": current}
                            if first:
                                payload["columns"] = columns
                                payload["sql"] = stmt
                                first = False
                            total += len(batch)
                            yield SqlChunk(output_type="rows", payload=payload, row_count=len(batch), is_final=False)
                    if first:  # zero rows: still tell the UI the columns
                        yield SqlChunk(output_type="rows",
                                       payload={"rows": [], "columns": columns, "statement": current, "sql": stmt},
                                       row_count=0, is_final=False)
                    last_total, last_truncated = total, total >= max_rows
                    yield SqlChunk(
                        output_type="statement",
                        payload={"statement": current, "sql": stmt, "total_rows": total, "truncated": last_truncated},
                        row_count=0, is_final=False)
                yield SqlChunk(output_type="done",
                               payload={"total_rows": last_total, "truncated": last_truncated,
                                        "statements": len(statements)},
                               row_count=0, is_final=True)
        except TimeoutError:
            yield _error("TIMEOUT", f"Query exceeded {timeout_seconds}s", current)
        except asyncpg.QueryCanceledError as e:
            code = "TIMEOUT" if "statement timeout" in str(e) else "CANCELLED"
            yield _error(code, scrub(str(e)), current)
        except asyncpg.PostgresError as e:
            yield _error("QUERY_ERROR", scrub(str(e)), current)
        finally:
            try:
                await asyncio.shield(c.close(timeout=5))
            except Exception:  # noqa: BLE001
                c.terminate()
