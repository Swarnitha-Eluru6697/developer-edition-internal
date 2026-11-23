"""
Live metadata discovery — connects to the real source with the given connection
details and pulls back databases / schemas / tables (or collections / indices /
files), instead of just echoing configured names.

Each source uses its native driver. On any failure (unreachable, bad creds,
driver missing, or a source with no live introspection) it falls back to the
configured object lists so the endpoint never hard-fails.
"""

import asyncio
import os
from typing import Any

import structlog

from app.core.config import settings
from app.core.http_probe import safe_get
from app.core.network_policy import (
    DestinationBlockedError,
    RateLimitExceededError,
    outbound_guard,
    validate_destination,
)
from app.core.redaction import redact_secrets

logger = structlog.get_logger(__name__)


def _is_policy_block(exc: Exception) -> bool:
    """A destination the platform CHOSE not to connect to (a deliberate skip), as opposed to one it could not resolve:
    network_policy reports an unresolvable host as DestinationBlockedError too, but that is a failure to reach the
    source — a mistyped host name — not a policy decision."""
    return isinstance(exc, DestinationBlockedError) and not isinstance(exc.__cause__, OSError)


def _safe_failure(exc: Exception, *, event: str, timeout: float | None = None, **context: Any) -> str:
    """BUG-11: what a failed probe reports back — a fixed message, a stable code and a reference to the redacted log
    entry holding the real cause (SafeError.describe). The driver's own text (host, user, SQL, paths) is never part of
    it: this result is persisted (a discovery run's error, a pipeline's introspection status, a skipped quality result)
    and returned to callers. Imported here, not at module level: safe_error depends on modules that import this one."""
    from app.services.safe_error import to_safe_error

    return to_safe_error(exc, event=event, timeout_seconds=timeout, **context).describe()


def _result(connected, message, *, databases=None, schemas=None, tables=None, names=None, otype="object"):
    return {
        "connected": connected,
        "message": message,
        "databases": databases or [],
        "schemas": schemas or [],
        "tables": tables or [],
        "objects": [{"name": n, "type": otype} for n in (names or [])],
    }


def _fallback(cfg, connected, message):
    names = cfg.get("tables") or cfg.get("objects") or cfg.get("endpoints") or []
    return _result(connected, message, names=list(names))


def _cap() -> int:
    """GAP-10: how many objects a driver fetches at most — one more than the stored limit, so the caller can tell
    "exactly the limit" from "more than the limit" (truncated). Applied in the source query where there is one, so a
    huge catalogue is never pulled into memory only to be thrown away."""
    return settings.discovery_max_objects + 1


def _host(cfg):
    return cfg.get("hostname") or cfg.get("host")


# ── PostgreSQL ────────────────────────────────────────────────────────────
async def _pg(cfg, timeout):
    import asyncpg

    conn = await asyncio.wait_for(
        asyncpg.connect(host=_host(cfg), port=int(cfg.get("port") or 5432), user=cfg.get("username"),
                        password=cfg.get("password"), database=cfg.get("database"), timeout=timeout),
        timeout=timeout + 1,
    )
    try:
        dbs = [r[0] for r in await conn.fetch("SELECT datname FROM pg_database WHERE datistemplate=false ORDER BY 1")]
        rows = await conn.fetch(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1,2 "
            f"LIMIT {_cap()}"
        )
    finally:
        await conn.close()
    tables = [{"schema": r[0], "name": r[1]} for r in rows]
    schemas = sorted({t["schema"] for t in tables})
    return _result(True, f"Found {len(tables)} tables across {len(schemas)} schemas",
                   databases=dbs, schemas=schemas, tables=tables,
                   names=[f"{t['schema']}.{t['name']}" for t in tables], otype="table")


# ── MySQL ─────────────────────────────────────────────────────────────────
async def _mysql(cfg, timeout):
    import aiomysql

    conn = await asyncio.wait_for(
        aiomysql.connect(host=_host(cfg), port=int(cfg.get("port") or 3306), user=cfg.get("username"),
                         password=cfg.get("password") or "", db=cfg.get("database"), connect_timeout=timeout),
        timeout=timeout + 1,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('mysql','information_schema','performance_schema','sys') ORDER BY 1"
            )
            dbs = [r[0] for r in await cur.fetchall()]
            await cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys') ORDER BY 1,2 "
                f"LIMIT {_cap()}"
            )
            rows = await cur.fetchall()
    finally:
        conn.close()
    tables = [{"schema": r[0], "name": r[1]} for r in rows]
    schemas = sorted({t["schema"] for t in tables}) or dbs
    return _result(True, f"Found {len(tables)} tables", databases=dbs, schemas=schemas, tables=tables,
                   names=[f"{t['schema']}.{t['name']}" for t in tables], otype="table")


# ── SQL Server ────────────────────────────────────────────────────────────
async def _sqlserver(cfg, timeout):
    import pytds

    def _run():
        c = pytds.connect(server=_host(cfg), port=int(cfg.get("port") or 1433), user=cfg.get("username"),
                          password=cfg.get("password"), database=cfg.get("database"), login_timeout=timeout, timeout=timeout)
        try:
            cur = c.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name")
            dbs = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE' ORDER BY 1,2 "
                        f"OFFSET 0 ROWS FETCH NEXT {_cap()} ROWS ONLY")
            rows = [(r[0], r[1]) for r in cur.fetchall()]
            return dbs, rows
        finally:
            c.close()

    dbs, rows = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 3)
    tables = [{"schema": s, "name": n} for s, n in rows]
    schemas = sorted({t["schema"] for t in tables})
    return _result(True, f"Found {len(tables)} tables", databases=dbs, schemas=schemas, tables=tables,
                   names=[f"{t['schema']}.{t['name']}" for t in tables], otype="table")


# ── Oracle (thin mode) ────────────────────────────────────────────────────
async def _oracle(cfg, timeout):
    import oracledb

    def _run():
        port = int(cfg.get("port") or 1521)
        dsn = oracledb.makedsn(_host(cfg), port, service_name=cfg.get("database")) if cfg.get("database") else f"{_host(cfg)}:{port}"
        c = oracledb.connect(user=cfg.get("username"), password=cfg.get("password"), dsn=dsn)
        try:
            cur = c.cursor()
            cur.execute(
                "SELECT owner, table_name FROM all_tables WHERE owner NOT IN "
                "('SYS','SYSTEM','OUTLN','XDB','MDSYS','CTXSYS','DBSNMP','APPQOSSYS','ORDSYS','ORDDATA',"
                "'WMSYS','LBACSYS','GSMADMIN_INTERNAL','AUDSYS','DVSYS','OJVMSYS') "
                f"ORDER BY owner, table_name FETCH FIRST {_cap()} ROWS ONLY"
            )
            return [(r[0], r[1]) for r in cur.fetchall()]
        finally:
            c.close()

    rows = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 3)
    tables = [{"schema": s, "name": n} for s, n in rows]
    schemas = sorted({t["schema"] for t in tables})
    return _result(True, f"Found {len(tables)} tables across {len(schemas)} schemas",
                   databases=schemas, schemas=schemas, tables=tables,
                   names=[f"{t['schema']}.{t['name']}" for t in tables], otype="table")


# ── MongoDB ───────────────────────────────────────────────────────────────
async def _mongo(cfg, timeout):
    import pymongo

    def _run():
        c = pymongo.MongoClient(host=_host(cfg), port=int(cfg.get("port") or 27017),
                                username=cfg.get("username") or None, password=cfg.get("password") or None,
                                authSource=cfg.get("auth_source") or "admin",
                                serverSelectionTimeoutMS=int(timeout * 1000))
        try:
            dbs = list(c.list_database_names())
            target = cfg.get("database")
            db_list = [target] if target else [d for d in dbs if d not in ("admin", "local", "config")]
            colls = []
            for d in db_list:
                for coll in c[d].list_collection_names():
                    colls.append({"schema": d, "name": coll})
                if len(colls) >= _cap():
                    break
            return dbs, colls[:_cap()]
        finally:
            c.close()

    dbs, colls = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 2)
    schemas = sorted({t["schema"] for t in colls})
    return _result(True, f"Found {len(colls)} collections across {len(schemas)} databases",
                   databases=dbs, schemas=schemas, tables=colls,
                   names=[f"{t['schema']}.{t['name']}" for t in colls], otype="collection")


# ── Elasticsearch (HTTP _cat/indices) ──────────────────────────────────────
async def _es(cfg, timeout, org_id=None):
    host = _host(cfg)
    scheme = "https" if str(cfg.get("scheme", "http")).lower() == "https" else "http"
    base = cfg.get("base_url") or f"{scheme}://{host}:{int(cfg.get('port') or 9200)}"
    user = cfg.get("username")
    auth = (user, cfg.get("password")) if user else None
    resp = await safe_get(base.rstrip("/") + "/_cat/indices?format=json&h=index", org_id=org_id, auth=auth)
    if resp.status_code >= 400:
        raise RuntimeError(f"Elasticsearch returned HTTP {resp.status_code}")
    data = resp.json()
    indices = sorted(d.get("index") for d in data if d.get("index"))[:_cap()]
    tables = [{"schema": "", "name": i} for i in indices]
    return _result(True, f"Found {len(indices)} indices", tables=tables, names=indices, otype="index")


# ── FTP (stdlib) ───────────────────────────────────────────────────────────
async def _ftp(cfg, timeout):
    from ftplib import FTP

    def _run():
        ftp = FTP()
        ftp.connect(_host(cfg), int(cfg.get("port") or 21), timeout=timeout)
        try:
            ftp.login(cfg.get("username") or "anonymous", cfg.get("password") or "")
            return ftp.nlst(cfg.get("path") or ".")[:_cap()]
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    names = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 3)
    return _result(True, f"Found {len(names)} entries", names=list(names), otype="file")


# ── Local file / NFS ────────────────────────────────────────────────────────
def _file(cfg):
    path = cfg.get("path")
    if not path or not os.path.exists(path):
        return _result(False, f"Path not found on server: {path}", names=list(cfg.get("objects") or []))
    if os.path.isdir(path):
        names = sorted(os.listdir(path))[:_cap()]
        return _result(True, f"{len(names)} entries in {path}", names=names, otype="file")
    return _result(True, f"File exists: {path}", names=[os.path.basename(path)], otype="file")


_DB = {"postgres": _pg, "postgresql": _pg, "mysql": _mysql, "sqlserver": _sqlserver, "oracle": _oracle, "mongodb": _mongo}


async def _guard_destination(cfg: dict, org_id: str | None, purpose: str) -> None:
    """Validate cfg's declared host against network policy before any driver
    connects. A source with no host (e.g. a local file) is a no-op. Raises
    DestinationBlockedError — callers convert that to their normal
    "couldn't connect" shape, same as any other connection failure."""
    host = _host(cfg)
    if host:
        await validate_destination(host, org_id=org_id, purpose=purpose)


async def discover(connection: dict[str, Any], timeout: float = 8.0, *, org_id: str | None = None) -> dict[str, Any]:
    st = (connection.get("source_type") or "").lower()
    cfg = connection.get("connection_config", {}) or {}
    org_id = org_id or connection.get("org_id")
    try:
        async with outbound_guard.guard(org_id):
            await _guard_destination(cfg, org_id, "discover")
            if st in _DB:
                result = await _DB[st](cfg, timeout)
            elif st == "elasticsearch":
                result = await _es(cfg, timeout, org_id)
            elif st == "ftp":
                result = await _ftp(cfg, timeout)
            elif st == "file":
                result = _file(cfg)
            else:
                # s3 / gcs / azure_blob / sftp / api / rest / webhook — no built-in live introspection
                result = _fallback(
                    cfg, None, "Live metadata introspection not supported for this source; showing configured objects."
                )
    except RateLimitExceededError:
        raise  # not a connection failure — the caller is being throttled; let it surface as such
    except Exception as exc:
        if _is_policy_block(exc):
            result = _fallback(cfg, False, redact_secrets(str(exc)))
        else:
            result = _fallback(cfg, False, "Could not connect/introspect: " + _safe_failure(
                exc, event="connection_discovery.discover_failed", timeout=timeout, org_id=org_id, source_type=st,
            ))
    logger.info(
        "connection_discovery.discover", org_id=org_id, source_type=st, connected=result.get("connected"),
    )
    return result


# ── Column-level schema introspection for a single entity/table ─────────────
def _map_sql_type(dt: str) -> str:
    dt = (dt or "").lower()
    if any(k in dt for k in ("serial", "int")):
        return "integer"
    if any(k in dt for k in ("numeric", "decimal", "real", "double", "float", "money", "number")):
        return "number"
    if "bool" in dt or dt == "bit":
        return "boolean"
    if any(k in dt for k in ("timestamp", "date", "time")):
        return "timestamp"
    return "string"


def _split_entity(entity: str, default_schema: str | None = None):
    if entity and "." in entity:
        s, t = entity.split(".", 1)
        return s, t
    return default_schema, entity


async def _pg_cols(cfg, entity, timeout):
    import asyncpg

    schema, table = _split_entity(entity, "public")
    conn = await asyncio.wait_for(
        asyncpg.connect(host=_host(cfg), port=int(cfg.get("port") or 5432), user=cfg.get("username"),
                        password=cfg.get("password"), database=cfg.get("database"), timeout=timeout),
        timeout=timeout + 1,
    )
    try:
        rows = await conn.fetch(
            """
            SELECT c.column_name, c.data_type, c.is_nullable, COALESCE(pk.is_pk, false) AS is_pk
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT kcu.column_name, true AS is_pk
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = $1 AND tc.table_name = $2
            ) pk ON pk.column_name = c.column_name
            WHERE c.table_schema = $1 AND c.table_name = $2
            ORDER BY c.ordinal_position
            """,
            schema, table,
        )
    finally:
        await conn.close()
    fields = [{"name": r["column_name"], "type": _map_sql_type(r["data_type"]),
               "nullable": r["is_nullable"] == "YES", "primary_key": bool(r["is_pk"])} for r in rows]
    return {"connected": True, "message": f"{len(fields)} columns from {schema}.{table}", "fields": fields}


async def _mysql_cols(cfg, entity, timeout):
    import aiomysql

    schema, table = _split_entity(entity, cfg.get("database"))
    conn = await asyncio.wait_for(
        aiomysql.connect(host=_host(cfg), port=int(cfg.get("port") or 3306), user=cfg.get("username"),
                         password=cfg.get("password") or "", db=cfg.get("database"), connect_timeout=timeout),
        timeout=timeout + 1,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT column_name, data_type, is_nullable, column_key FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema, table),
            )
            rows = await cur.fetchall()
    finally:
        conn.close()
    fields = [{"name": r[0], "type": _map_sql_type(r[1]), "nullable": r[2] == "YES", "primary_key": r[3] == "PRI"} for r in rows]
    return {"connected": True, "message": f"{len(fields)} columns from {table}", "fields": fields}


async def _sqlserver_cols(cfg, entity, timeout):
    import pytds

    schema, table = _split_entity(entity, "dbo")

    def _run():
        c = pytds.connect(server=_host(cfg), port=int(cfg.get("port") or 1433), user=cfg.get("username"),
                          password=cfg.get("password"), database=cfg.get("database"), login_timeout=timeout, timeout=timeout)
        try:
            cur = c.cursor()
            cur.execute(
                "SELECT c.column_name, c.data_type, c.is_nullable, "
                "CASE WHEN k.column_name IS NULL THEN 0 ELSE 1 END AS is_pk "
                "FROM information_schema.columns c "
                "LEFT JOIN (SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name "
                "WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_name=%s) k ON k.column_name=c.column_name "
                "WHERE c.table_schema=%s AND c.table_name=%s ORDER BY c.ordinal_position",
                (table, schema, table),
            )
            return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
        finally:
            c.close()

    rows = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 3)
    fields = [{"name": r[0], "type": _map_sql_type(r[1]), "nullable": str(r[2]).upper() == "YES", "primary_key": bool(r[3])} for r in rows]
    return {"connected": True, "message": f"{len(fields)} columns from {table}", "fields": fields}


async def _oracle_cols(cfg, entity, timeout):
    import oracledb

    _, table = _split_entity(entity)
    table = (table or entity).upper()

    def _run():
        port = int(cfg.get("port") or 1521)
        dsn = oracledb.makedsn(_host(cfg), port, service_name=cfg.get("database")) if cfg.get("database") else f"{_host(cfg)}:{port}"
        c = oracledb.connect(user=cfg.get("username"), password=cfg.get("password"), dsn=dsn)
        try:
            cur = c.cursor()
            cur.execute(
                "SELECT tc.column_name, tc.data_type, tc.nullable, "
                "CASE WHEN pk.column_name IS NULL THEN 0 ELSE 1 END "
                "FROM all_tab_columns tc "
                "LEFT JOIN (SELECT acc.column_name FROM all_constraints ac "
                "JOIN all_cons_columns acc ON ac.constraint_name=acc.constraint_name "
                "WHERE ac.constraint_type='P' AND ac.table_name=:t) pk ON pk.column_name=tc.column_name "
                "WHERE tc.table_name=:t ORDER BY tc.column_id",
                t=table,
            )
            return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
        finally:
            c.close()

    rows = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 3)
    fields = [{"name": r[0], "type": _map_sql_type(r[1]), "nullable": r[2] == "Y", "primary_key": bool(r[3])} for r in rows]
    return {"connected": True, "message": f"{len(fields)} columns from {table}", "fields": fields}


async def _mongo_cols(cfg, entity, timeout):
    import pymongo

    from app.services.schema_inference import infer_schema

    db_name, coll = _split_entity(entity, cfg.get("database"))

    def _run():
        c = pymongo.MongoClient(host=_host(cfg), port=int(cfg.get("port") or 27017),
                                username=cfg.get("username") or None, password=cfg.get("password") or None,
                                authSource=cfg.get("auth_source") or "admin", serverSelectionTimeoutMS=int(timeout * 1000))
        try:
            return list(c[db_name or "test"][coll].find({}, limit=25))
        finally:
            c.close()

    docs = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 2)
    for d in docs:
        d.pop("_id", None)
    inferred = infer_schema([dict(d) for d in docs])
    return {"connected": True, "message": f"Inferred from {len(docs)} sample documents", "fields": inferred["fields"]}


_COL = {"postgres": _pg_cols, "postgresql": _pg_cols, "mysql": _mysql_cols, "sqlserver": _sqlserver_cols, "oracle": _oracle_cols, "mongodb": _mongo_cols}


async def introspect_columns(
    connection: dict, entity: str, timeout: float = 8.0, *, org_id: str | None = None
) -> dict:
    """Connect to the source and return the column schema of one entity/table."""
    st = (connection.get("source_type") or "").lower()
    cfg = connection.get("connection_config", {}) or {}
    org_id = org_id or connection.get("org_id")
    if st not in _COL:
        return {"connected": False, "message": f"Schema introspection not supported for source '{st}'", "fields": []}
    try:
        async with outbound_guard.guard(org_id):
            await _guard_destination(cfg, org_id, "introspect_columns")
            result = await _COL[st](cfg, entity, timeout)
    except RateLimitExceededError:
        raise
    except Exception as exc:
        if _is_policy_block(exc):
            # Policy, not a fault: the platform chose not to connect. A "skip", never a "failure".
            result = {"connected": False, "message": redact_secrets(str(exc)), "fields": []}
        else:
            # BUG-10: an actual attempt that went wrong (timeout, refused/dropped connection, bad credentials, missing
            # driver, an unresolvable host, an unexpected driver error). Flagged `failed` so the caller records FAILED
            # with a reason rather than treating it like an expected skip. BUG-11: the reason is the SafeError's, never
            # the driver's own text.
            result = {
                "connected": False, "failed": True, "fields": [],
                "message": "Could not introspect schema: " + _safe_failure(
                    exc, event="connection_discovery.introspect_columns_failed", timeout=timeout,
                    org_id=org_id, source_type=st, entity=entity,
                ),
            }
    logger.info(
        "connection_discovery.introspect_columns", org_id=org_id, source_type=st,
        entity=entity, connected=result.get("connected"),
    )
    return result


# ── Row sampling (for auto-running quality checks on real data) ─────────────
def _jsonify(records: list[dict]) -> list[dict]:
    """Coerce driver rows (Decimal/date/UUID…) into JSON-safe dicts."""
    import json

    return json.loads(json.dumps(records, default=str))


async def _pg_rows(cfg, entity, limit, timeout):
    import asyncpg

    schema, table = _split_entity(entity, "public")
    conn = await asyncio.wait_for(
        asyncpg.connect(host=_host(cfg), port=int(cfg.get("port") or 5432), user=cfg.get("username"),
                        password=cfg.get("password"), database=cfg.get("database"), timeout=timeout),
        timeout=timeout + 1,
    )
    try:
        rows = await conn.fetch(f'SELECT * FROM "{schema}"."{table}" LIMIT {int(limit)}')
    finally:
        await conn.close()
    return [dict(r) for r in rows]


async def _mysql_rows(cfg, entity, limit, timeout):
    import aiomysql

    schema, table = _split_entity(entity, cfg.get("database"))
    conn = await asyncio.wait_for(
        aiomysql.connect(host=_host(cfg), port=int(cfg.get("port") or 3306), user=cfg.get("username"),
                         password=cfg.get("password") or "", db=cfg.get("database"), connect_timeout=timeout),
        timeout=timeout + 1,
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT * FROM `{schema}`.`{table}` LIMIT {int(limit)}")
            return list(await cur.fetchall())
    finally:
        conn.close()


async def _mongo_rows(cfg, entity, limit, timeout):
    import pymongo

    db_name, coll = _split_entity(entity, cfg.get("database"))

    def _run():
        c = pymongo.MongoClient(host=_host(cfg), port=int(cfg.get("port") or 27017),
                                username=cfg.get("username") or None, password=cfg.get("password") or None,
                                authSource=cfg.get("auth_source") or "admin", serverSelectionTimeoutMS=int(timeout * 1000))
        try:
            docs = list(c[db_name or "test"][coll].find({}, limit=int(limit)))
            for d in docs:
                d.pop("_id", None)
            return docs
        finally:
            c.close()

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout + 2)


_ROWS = {"postgres": _pg_rows, "postgresql": _pg_rows, "mysql": _mysql_rows, "mongodb": _mongo_rows}


async def sample_rows(
    connection: dict, entity: str, limit: int = 50, timeout: float = 8.0, *, org_id: str | None = None
) -> dict:
    """Pull up to `limit` real rows from the source entity, for quality evaluation."""
    st = (connection.get("source_type") or "").lower()
    cfg = connection.get("connection_config", {}) or {}
    org_id = org_id or connection.get("org_id")
    # Never trust a caller-supplied limit past this deployment's ceiling —
    # an unbounded sample size is itself a resource-exhaustion / data-
    # exfiltration-volume risk against a source DataConnect doesn't own.
    limit = min(limit, settings.max_sample_rows)
    if st not in _ROWS:
        return {"connected": False, "message": f"Row sampling not supported for source '{st}'", "records": []}
    try:
        async with outbound_guard.guard(org_id):
            await _guard_destination(cfg, org_id, "sample_rows")
            records = _jsonify(await _ROWS[st](cfg, entity, limit, timeout))
        result = {"connected": True, "message": f"Sampled {len(records)} rows from {entity}", "records": records}
    except RateLimitExceededError:
        raise
    except Exception as exc:
        if _is_policy_block(exc):
            result = {"connected": False, "message": redact_secrets(str(exc)), "records": []}
        else:
            result = {"connected": False, "records": [], "message": "Could not sample rows: " + _safe_failure(
                exc, event="connection_discovery.sample_rows_failed", timeout=timeout, org_id=org_id,
                source_type=st, entity=entity,
            )}
    logger.info(
        "connection_discovery.sample_rows", org_id=org_id, source_type=st,
        entity=entity, limit=limit, connected=result.get("connected"),
    )
    return result
