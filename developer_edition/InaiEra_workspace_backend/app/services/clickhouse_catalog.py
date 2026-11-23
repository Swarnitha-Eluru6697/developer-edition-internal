"""ClickHouse as a source of table metadata and a syntax/semantic check for the code assistant (engine "clickhouse").

Talks to ClickHouse's HTTP interface with httpx (no extra client library). Metadata only, never row data: table and column names come from
`system.tables` / `system.columns`, and a candidate is checked with EXPLAIN, which analyses the query without running it. Every request is
sent with `readonly=1`, so nothing here can change data even by mistake.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.config import get_settings

_SYSTEM_DATABASES = ("system", "INFORMATION_SCHEMA", "information_schema")
_SELECTISH = re.compile(r"^\s*(\(\s*)*(select|with)\b", re.IGNORECASE)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ClickHouseNotConfigured(RuntimeError):
    """CLICKHOUSE_URL is not set on the Workspace."""


def _quote(value: str) -> str:
    """A ClickHouse string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class ClickHouseCatalog:
    def __init__(self) -> None:
        self._s = get_settings()
        if not self._s.clickhouse_url:
            raise ClickHouseNotConfigured("ClickHouse is not configured on this Workspace (CLICKHOUSE_URL).")

    async def _post(self, query: str, *, fmt: str | None = "JSONEachRow") -> httpx.Response:
        s = self._s
        auth = (s.clickhouse_user, s.clickhouse_password or "")
        params = {"readonly": "1", "database": s.clickhouse_database}
        async with httpx.AsyncClient(timeout=float(s.clickhouse_timeout_seconds)) as client:
            return await client.post(str(s.clickhouse_url).rstrip("/") + "/", params=params, auth=auth,
                                     content=(query + (f" FORMAT {fmt}" if fmt else "")).encode())

    async def _rows(self, query: str) -> list[dict[str, Any]]:
        resp = await self._post(query)
        resp.raise_for_status()
        import json
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    async def list_tables(self) -> dict:
        """{"tables": [{"fqn": "db.table", "description": ...}]}, user databases only (the shape the grounder reads)."""
        skip = ", ".join(_quote(d) for d in _SYSTEM_DATABASES)
        rows = await self._rows(f"SELECT database, name, comment FROM system.tables WHERE database NOT IN ({skip}) "
                                "ORDER BY database, name LIMIT 500")
        return {"tables": [{"fqn": f"{r['database']}.{r['name']}", "description": r.get("comment") or None} for r in rows]}

    async def table_schema(self, table: str) -> dict:
        """{"columns": [{"name", "type", "nullable"}], "partition_keys": [...]} for `db.table` (or `table` in the default database)."""
        database, _, name = table.rpartition(".")
        database = database or self._s.clickhouse_database
        rows = await self._rows("SELECT name, type, is_in_partition_key FROM system.columns "
                                f"WHERE database = {_quote(database)} AND table = {_quote(name)} ORDER BY position")
        cols = [{"name": r["name"], "type": r["type"], "nullable": str(r["type"]).startswith("Nullable(")} for r in rows]
        keys = [r["name"] for r in rows if r.get("is_in_partition_key")]
        return {"columns": cols, "partition_keys": keys}

    async def explain(self, sql: str) -> dict:
        """{"ok", "errors"}: SELECTs are analysed with EXPLAIN PLAN (unknown tables and columns are found, nothing runs); anything else gets a
        parse check with EXPLAIN AST. A server that cannot be reached is reported as skipped, not as a fault in the SQL."""
        statement = sql.strip().rstrip(";").strip()
        if ";" in statement:
            statement = statement.split(";")[0]          # one statement at a time: the first is checked
        kind = "PLAN" if _SELECTISH.match(statement) else "AST"
        try:
            resp = await self._post(f"EXPLAIN {kind} {statement}", fmt=None)
        except httpx.HTTPError as e:
            return {"ok": True, "skipped": True, "errors": [], "reason": f"ClickHouse unreachable: {type(e).__name__}"}
        if resp.status_code == 200:
            return {"ok": True, "errors": []}
        if resp.status_code in (401, 403) or resp.status_code >= 502:
            return {"ok": True, "skipped": True, "errors": [], "reason": f"ClickHouse cannot validate (HTTP {resp.status_code})"}
        return {"ok": False, "errors": [resp.text.strip()[:500]]}
