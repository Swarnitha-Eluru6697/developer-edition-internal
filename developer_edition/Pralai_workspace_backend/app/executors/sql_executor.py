"""SQL executor.

Submits SQL to the Data Lake House query service in the data plane and streams
result chunks back. The lakehouse service is responsible for SQL execution
against Iceberg/pg_lake — Workspace does NOT execute SQL itself.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
import structlog

from app.core.config import get_settings
from app.executors.sql_split import split_statements

log = structlog.get_logger()


@dataclass
class SqlChunk:
    output_type: str
    payload: dict
    row_count: int
    is_final: bool


class SqlExecutor:
    """Streams SQL results from the Data Lake House service."""

    async def execute(
        self,
        *,
        execution_id: str,
        user_id: str,
        code_text: str,
        target: str | None,
        max_rows: int,
        timeout_seconds: int,
    ) -> AsyncIterator[SqlChunk]:
        settings = get_settings()
        # Lakehouse warehouses only. A `connection:<id>` target runs in the
        # worker itself (ConnectionSqlExecutor) so its credentials are never
        # sent to another service (WS-DATA-007); reaching here with one is a
        # routing bug, refused rather than resolved.
        if target and target.startswith("connection:"):
            raise ValueError("connection targets are executed by ConnectionSqlExecutor")
        resolved_target, secret = target, None

        statements = split_statements(code_text)
        if len(statements) <= 1:
            # One statement (or nothing splittable): exactly as before, text untouched.
            async for chunk in self._stream_one(
                sql=code_text, statement=None, execution_id=execution_id, user_id=user_id,
                target=resolved_target, secret=secret, max_rows=max_rows, timeout_seconds=timeout_seconds,
            ):
                yield chunk
            return

        # A script: run statement by statement and stream EVERY result, tagged with its index
        # (same protocol as ConnectionSqlExecutor). A failing statement stops the script.
        last_total = 0
        for i, stmt in enumerate(statements):
            last = i == len(statements) - 1
            first_rows = True
            async for chunk in self._stream_one(
                sql=stmt, statement=i, execution_id=execution_id, user_id=user_id,
                target=resolved_target, secret=secret, max_rows=max_rows, timeout_seconds=timeout_seconds,
            ):
                if chunk.output_type == "rows":
                    chunk.payload["statement"] = i
                    if first_rows:
                        chunk.payload["sql"] = stmt
                        first_rows = False
                    yield chunk
                elif chunk.output_type == "done":
                    last_total = chunk.payload.get("total_rows", 0)
                    yield SqlChunk(
                        output_type="statement",
                        payload={"statement": i, "sql": stmt, "total_rows": last_total},
                        row_count=0, is_final=False,
                    )
                else:  # error (final): name the statement, stop the script
                    chunk.payload["statement"] = i
                    yield chunk
                    return
            if last:
                yield SqlChunk(output_type="done",
                               payload={"total_rows": last_total, "statements": len(statements)},
                               row_count=0, is_final=True)

    async def _stream_one(
        self, *, sql: str, statement: int | None, execution_id: str, user_id: str,
        target: str | None, secret: str | None, max_rows: int, timeout_seconds: int,
    ) -> AsyncIterator[SqlChunk]:
        """One request to the lakehouse for ONE statement (or a whole unsplit text)."""
        settings = get_settings()
        url = f"{settings.lakehouse_query_url}/v1/queries/stream"
        body = {
            "sql": sql,
            "warehouse": target,
            "max_rows": max_rows,
            "chunk_size": settings.query_chunk_size,
            "user_id": user_id,
            "execution_id": execution_id,
        }

        columns_sent = False
        pending_columns = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds + 10, connect=10.0)) as client:
            async with client.stream(
                "POST", url,
                json=body,
                headers={"X-Workspace-Service": "true"},
            ) as response:
                if response.status_code >= 400:
                    error_body = await response.aread()
                    try:
                        err = json.loads(error_body)
                    except Exception:
                        err = {"message": error_body.decode("utf-8", "ignore")}
                    yield SqlChunk(
                        output_type="error",
                        payload={
                            "error_code": err.get("code", "LAKEHOUSE_ERROR"),
                            "error_message": _redact(err.get("message", "Lakehouse query failed"), secret),
                        },
                        row_count=0,
                        is_final=True,
                    )
                    return

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("invalid_chunk", line=line[:200])
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "schema":
                        # Save columns to send with the first rows chunk
                        pending_columns = msg["columns"]
                    elif msg_type == "rows":
                        rows = msg["rows"]
                        payload = {"rows": rows}
                        if not columns_sent:
                            payload["columns"] = pending_columns
                            columns_sent = True
                        yield SqlChunk(
                            output_type="rows",
                            payload=payload,
                            row_count=len(rows),
                            is_final=False,
                        )
                    elif msg_type == "done":
                        yield SqlChunk(
                            output_type="done",
                            payload={"total_rows": msg.get("total_rows", 0)},
                            # 0: the router sums row_count over chunks, and the
                            # "rows" chunks already counted these (was doubled).
                            row_count=0,
                            is_final=True,
                        )
                    elif msg_type == "error":
                        yield SqlChunk(
                            output_type="error",
                            payload={
                                "error_code": msg.get("code", "QUERY_ERROR"),
                                "error_message": _redact(msg.get("message", "Query failed"), secret),
                            },
                            row_count=0,
                            is_final=True,
                        )

    async def cancel(self, *, execution_id: str, user_id: str = "", target: str | None = None) -> None:
        settings = get_settings()
        url = f"{settings.lakehouse_query_url}/v1/queries/{execution_id}/cancel"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(url)
            except httpx.HTTPError as e:
                log.warning("cancel_failed", execution_id=execution_id, error=str(e))


def _redact(text: str, secret: str | None) -> str:
    """Scrub a resolved connection's password out of anything the lakehouse
    echoes back — e.g. a connection-refused message that includes the DSN it
    tried. Same pattern as app/api/git.py's PAT redaction."""
    s = str(text)
    if secret:
        s = s.replace(secret, "***")
    return s[:500]
