"""Run a SQL job's statements on a Workspace connection (Postgres / pg_lake), all or nothing.

Same connection rules as interactive SQL (app/executors/connection_sql_executor.py): the egress policy applies to the host, the
credentials are resolved here just in time and never leave this process, and the server-side statement timeout is set. Differences,
because this is a job and not an editor: the statements run in ONE transaction (the first failure rolls everything back and the
remaining statements are reported as skipped), and no rows are returned — each statement is reported by its kind and row count.
The result has the shape the dbt runner records for every job: success, errors, nodes (`statement.<n>`).
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog

from app.core.egress_policy import EgressPolicyError, assert_host_allowed

log = structlog.get_logger()

PG_CONNECTION_TYPES = ("postgres", "pg_lake")
EXCERPT = 160
_COUNT = re.compile(r"^(?:INSERT \d+|UPDATE|DELETE|MERGE|SELECT|COPY|MOVE|FETCH) (\d+)$")


def _kind(statement: str) -> str:
    stripped = re.sub(r"(?s)/\*.*?\*/|--[^\n]*", " ", statement).strip()
    return (stripped.split(None, 1)[0].upper() if stripped else "SQL")


def _excerpt(statement: str) -> str:
    one = " ".join(statement.split())
    return one if len(one) <= EXCERPT else one[: EXCERPT - 1] + "…"


def _node(i: int, statement: str, status: str, message: str, seconds: float | None = None) -> dict[str, Any]:
    return {"node_id": f"statement.{i}", "status": status, "message": message, "execution_time": seconds,
            "statement_kind": _kind(statement), "sql": _excerpt(statement)}


def _done_message(statement: str, status_text: str | None) -> str:
    m = _COUNT.match(status_text or "")
    rows = f" · {int(m.group(1))} row{'s' if int(m.group(1)) != 1 else ''}" if m else ""
    return _kind(statement) + rows


async def run_job_statements(*, conn, statements: list[str], timeout_seconds: int) -> dict[str, Any]:
    import asyncpg

    from app.api.catalog import _load_db_creds
    from app.services.catalog_adapter import explain_connect_error

    if conn.connection_type not in PG_CONNECTION_TYPES:
        return {"success": False, "nodes": [], "errors": [f"SQL jobs run on Postgres connections only, not '{conn.connection_type}'."]}
    try:
        await assert_host_allowed(conn.host or "")
    except EgressPolicyError as e:
        return {"success": False, "nodes": [], "errors": [f"Host not permitted: {e.reason}"]}
    creds = await _load_db_creds(conn.vault_secret_path)
    password = creds.get("password") or ""

    def scrub(text: str) -> str:
        return text.replace(password, "***") if password else text

    try:
        c = await asyncpg.connect(
            host=conn.host or "localhost", port=conn.port or 5432, user=creds.get("username") or "postgres", password=password,
            database=conn.database_name or "postgres", ssl="require" if conn.use_tls else None, timeout=10,
            server_settings={"statement_timeout": str(timeout_seconds * 1000), "application_name": "inaiera-sql-job"})
    except Exception as e:  # noqa: BLE001
        return {"success": False, "nodes": [], "errors": [scrub(explain_connect_error(e))]}

    nodes: list[dict[str, Any]] = []
    try:
        tr = c.transaction()
        await tr.start()
        for i, statement in enumerate(statements, start=1):
            started = time.monotonic()
            try:
                async with asyncio.timeout(timeout_seconds):
                    status_text = await c.execute(statement)
            except Exception as e:  # noqa: BLE001
                await tr.rollback()
                if isinstance(e, TimeoutError):
                    error = f"exceeded {timeout_seconds}s"
                else:
                    error = scrub(str(getattr(e, "message", None) or e)).strip()
                nodes.append(_node(i, statement, "error", error, time.monotonic() - started))
                nodes += [_node(j, s, "skipped", "not run: an earlier statement failed (nothing was committed)")
                          for j, s in enumerate(statements[i:], start=i + 1)]
                return {"success": False, "nodes": nodes, "errors": [f"statement {i} failed, nothing was committed: {error}"]}
            nodes.append(_node(i, statement, "success", _done_message(statement, status_text), time.monotonic() - started))
        await tr.commit()
        return {"success": True, "nodes": nodes, "errors": []}
    finally:
        try:
            await asyncio.shield(c.close(timeout=5))
        except Exception:  # noqa: BLE001
            c.terminate()
