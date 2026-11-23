"""Local Data Lake House query stub.

Speaks the exact NDJSON protocol the workspace SQL executor expects, backed by
a real Postgres so `Run` returns real rows. NOT for production — a dev stand-in
for the Data Lake House query service.

Endpoints:
  GET  /healthz
  POST /v1/queries/stream                         -> NDJSON: schema, rows*, done|error
  GET  /v1/catalog/namespaces                     -> {"namespaces":[{"name":..}]}
  GET  /v1/catalog/namespaces/{ns}/tables         -> {"tables":[{"name":..}]}
  POST /v1/queries/{qid}/cancel                   -> {"cancelled":true}
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DSN = os.environ.get(
    "LAKEHOUSE_PG_DSN",
    "postgresql://workspace:workspace@postgres:5432/workspace",
)
app = FastAPI(title="Lakehouse Query Stub")
_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=5)
    return _pool


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "lakehouse-stub"}


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements: strips -- and /* */ comments (so a
    commented-out query never runs) and splits on ';' outside string literals."""
    stmts, cur = [], []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":            # line comment
            j = sql.find("\n", i); i = n if j == -1 else j; continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":            # block comment
            j = sql.find("*/", i + 2); i = n if j == -1 else j + 2; continue
        if c == "'":                                                # string literal
            cur.append(c); i += 1
            while i < n:
                cur.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        cur.append(sql[i + 1]); i += 2; continue
                    i += 1; break
                i += 1
            continue
        if c == ";":
            s = "".join(cur).strip()
            if s:
                stmts.append(s)
            cur = []; i += 1; continue
        cur.append(c); i += 1
    s = "".join(cur).strip()
    if s:
        stmts.append(s)
    return stmts


@app.post("/v1/queries/stream")
async def stream_query(req: Request) -> StreamingResponse:
    body = await req.json()
    sql_text = body.get("sql") or ""
    max_rows = int(body.get("max_rows", 10_000))
    chunk_size = int(body.get("chunk_size", 1000))
    # If the caller passed a target DSN (the selected connection), run against
    # THAT database; otherwise the default (workspace) DB.
    target_dsn = body.get("warehouse") or ""
    use_target = isinstance(target_dsn, str) and target_dsn.startswith(("postgresql://", "postgres://"))

    # Support multi-statement scripts: run each statement in order, return the
    # result set of the LAST statement (mirrors how SQL editors run a script).
    statements = _split_statements(sql_text)
    cols: list = []
    rows: list = []
    error: str | None = None
    if not statements:
        error = "No SQL provided"
    else:
        conn = None
        acquired_from_pool = False
        try:
            if use_target:
                conn = await asyncpg.connect(dsn=target_dsn, timeout=10)
            else:
                conn = await (await pool()).acquire()
                acquired_from_pool = True
            try:
                # No explicit transaction: each statement commits on its own, so
                # a failing statement surfaces its real error (not a generic
                # "transaction aborted") — matching psql/DBeaver script behavior.
                for idx, stmt in enumerate(statements):
                    is_last = idx == len(statements) - 1
                    if not is_last:
                        await conn.execute(stmt)
                        continue
                    prepared = await conn.prepare(stmt)
                    cols = [{"name": a.name, "type": str(a.type.name)}
                            for a in prepared.get_attributes()]
                    records = await prepared.fetch(timeout=60)
                    for rec in records[:max_rows]:
                        rows.append([_json_safe(v) for v in rec.values()])
            finally:
                if conn is not None:
                    if acquired_from_pool:
                        await (await pool()).release(conn)
                    else:
                        await conn.close()
        except Exception as e:  # noqa: BLE001
            error = str(e)[:500]

    async def gen() -> AsyncIterator[bytes]:
        if error is not None:
            yield (json.dumps({"type": "error", "code": "QUERY_ERROR", "message": error}) + "\n").encode()
            return
        yield (json.dumps({"type": "schema", "columns": cols}) + "\n").encode()
        for i in range(0, len(rows), chunk_size):
            yield (json.dumps({"type": "rows", "rows": rows[i:i + chunk_size]}) + "\n").encode()
        yield (json.dumps({"type": "done", "total_rows": len(rows)}) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/v1/catalog/namespaces")
async def namespaces() -> dict:
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' "
            "ORDER BY schema_name"
        )
    return {"namespaces": [{"name": r["schema_name"]} for r in rows]}


@app.get("/v1/catalog/namespaces/{ns}/tables")
async def tables(ns: str) -> dict:
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = $1 ORDER BY table_name", ns
        )
    return {"tables": [{"name": r["table_name"], "type": "table"} for r in rows]}


@app.post("/v1/queries/{qid}/cancel")
async def cancel(qid: str) -> JSONResponse:
    return JSONResponse({"cancelled": True})


def _json_safe(v):
    import datetime
    import decimal
    import uuid
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "ignore")
    return v
