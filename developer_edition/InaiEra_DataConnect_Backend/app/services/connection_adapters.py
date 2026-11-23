import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import structlog

from app.core.config import settings
from app.core.http_probe import safe_get
from app.core.network_policy import DestinationBlockedError, outbound_guard, validate_destination

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConnectionCheck:
    status: str
    message: str
    details: dict[str, Any]


class ConnectionAdapter(Protocol):
    async def test(self, connection: dict[str, Any]) -> ConnectionCheck:
        ...

    async def discover(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        ...


class DatabaseConnectionAdapter:
    required_fields = {"hostname", "database"}

    async def test(self, connection: dict[str, Any]) -> ConnectionCheck:
        config = connection.get("connection_config", {})
        missing = sorted(self.required_fields - set(config))
        if missing:
            return ConnectionCheck("failed", f"Missing database fields: {', '.join(missing)}", {})
        return ConnectionCheck(
            "ok",
            "Database connection profile is structurally valid",
            {"host": config["hostname"], "database": config["database"]},
        )

    async def discover(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        config = connection.get("connection_config", {})
        tables = config.get("tables", [])
        return [{"name": table, "type": "table"} for table in tables]


class ApiConnectionAdapter:
    async def test(self, connection: dict[str, Any]) -> ConnectionCheck:
        config = connection.get("connection_config", {})
        base_url = config.get("base_url") or config.get("url")
        if not base_url:
            return ConnectionCheck("failed", "Missing API base_url", {})
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            return ConnectionCheck("failed", "API base_url must be http or https", {"base_url": base_url})
        health_path = config.get("health_path")
        if not health_path:
            return ConnectionCheck("ok", "API connection profile is structurally valid", {"base_url": base_url})
        target = base_url.rstrip("/") + "/" + health_path.lstrip("/")
        org_id = connection.get("org_id")
        try:
            async with outbound_guard.guard(org_id):
                response = await safe_get(target, org_id=org_id)
        except DestinationBlockedError as exc:
            return ConnectionCheck("failed", str(exc), {"base_url": base_url})
        return ConnectionCheck(
            "ok" if 200 <= response.status_code < 300 else "failed",
            f"Health endpoint returned HTTP {response.status_code}",
            {"base_url": base_url, "status_code": response.status_code},
        )

    async def discover(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        config = connection.get("connection_config", {})
        endpoints = config.get("endpoints", [])
        return [{"name": endpoint, "type": "endpoint"} for endpoint in endpoints]


class FileConnectionAdapter:
    async def test(self, connection: dict[str, Any]) -> ConnectionCheck:
        config = connection.get("connection_config", {})
        location = config.get("bucket") or config.get("path") or config.get("container")
        if not location:
            return ConnectionCheck("failed", "Missing file source location", {})
        return ConnectionCheck("ok", "File connection profile is structurally valid", {"location": location})

    async def discover(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        config = connection.get("connection_config", {})
        objects = config.get("objects", [])
        return [{"name": item, "type": "file"} for item in objects]


class SchemaConnectionAdapter:
    """The structural check for the catalog's other connection types: the fields the type's schema requires are present."""

    def __init__(self, source_type: str):
        self.source_type = source_type

    async def test(self, connection: dict[str, Any]) -> ConnectionCheck:
        from app.services.connection_config_schema import validate_connection_config_fields

        errors = validate_connection_config_fields(self.source_type, connection.get("connection_config", {}))
        if errors:
            return ConnectionCheck("failed", "; ".join(message for _key, message in errors), {})
        return ConnectionCheck("ok", f"{self.source_type} connection profile is structurally valid", {})

    async def discover(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        return []


def adapter_for_source_type(source_type: str) -> ConnectionAdapter:
    from app.services.connection_config_schema import NEW_SOURCE_TYPES

    normalized = source_type.lower()
    if normalized in NEW_SOURCE_TYPES:
        return SchemaConnectionAdapter(normalized)
    if normalized in {"postgres", "postgresql", "mysql", "sqlserver", "oracle", "jdbc", "database"}:
        return DatabaseConnectionAdapter()
    if normalized in {"api", "rest", "webhook"}:
        return ApiConnectionAdapter()
    if normalized in {"s3", "gcs", "azure_blob", "sftp", "file"}:
        return FileConnectionAdapter()
    return DatabaseConnectionAdapter()


# ── Live connectivity probe ────────────────────────────────────────────────
# Unlike the *structural* adapter.test() above, this actually reaches the
# target: a TCP handshake for databases / FTP / SFTP, a real HTTP GET for APIs,
# endpoint reachability for object stores, and a filesystem check for local files.

DEFAULT_PORTS: dict[str, int] = {
    "postgres": 5432, "postgresql": 5432, "mysql": 3306, "sqlserver": 1433,
    "oracle": 1521, "jdbc": 5432, "mongodb": 27017, "elasticsearch": 9200,
    "ftp": 21, "sftp": 22,
    # host/port connection types of the template catalog (connection_config_schema.NEW_SOURCE_TYPES)
    "activemq": 61616, "cassandra": 9042, "scylladb": 9042, "hbase": 2181, "redis": 6379, "aerospike": 3000,
    "clickhouse": 8123, "singlestore": 3306, "vertica": 5433, "heavyai": 6274, "yugabytedb": 5433, "questdb": 9009,
    "couchbase": 8091, "littlehorse": 2023, "gridgain": 10800, "gemfire": 10334, "diffusion": 8080, "lightstreamer": 6661,
    "data_diode": 3456, "rabbitmq": 5672, "teradata": 1025, "ibm_mq": 1414, "as400": 8470, "netezza": 5480, "mapr_db": 7222,
    "fix": 9878,
}

# Types configured by a URL instead of host/port: the probe reaches the URL's host and port.
URL_KEYS: tuple[str, ...] = ("url", "endpoint_url", "ingestion_url", "base_url")
# Cloud services reached at a fixed public endpoint (no host in the connection): the probe reaches that endpoint on 443.
FIXED_HOSTS: dict[str, str] = {
    "ably": "rest.ably.io", "bigquery": "bigquery.googleapis.com", "gcp_functions": "cloudfunctions.googleapis.com",
    "pagerduty": "events.pagerduty.com", "dataproc": "dataproc.googleapis.com", "aws_eventbridge": "events.us-east-1.amazonaws.com",
    "firebolt": "id.app.firebolt.io",
}
# A connection with nothing to reach (Datagen generates its data, Syslog / SNMP listen on the worker): no probe.
NO_REMOTE_TYPES = frozenset({"none"})


async def _tcp_probe(host: str, port: int, timeout: float = 5.0) -> tuple[bool, float, str | None]:
    """Open a TCP connection and immediately close it. Returns (ok, latency_ms, error)."""
    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, round((time.perf_counter() - start) * 1000, 1), None
    except Exception as exc:  # DNS failure, refused, timeout, etc.
        return False, round((time.perf_counter() - start) * 1000, 1), str(exc) or exc.__class__.__name__


async def _validated(host: str, org_id: str | None) -> ConnectionCheck | None:
    """None if `host` is permitted; a "failed" ConnectionCheck (never raises)
    if network policy blocks it — callers return this straight through."""
    try:
        await validate_destination(host, org_id=org_id, purpose="connection_test")
    except DestinationBlockedError as exc:
        return ConnectionCheck("failed", str(exc), {"host": host})
    return None


async def live_probe(connection: dict[str, Any], timeout: float = 6.0) -> ConnectionCheck:
    source_type = (connection.get("source_type") or "").lower()
    config = connection.get("connection_config", {}) or {}
    org_id = connection.get("org_id")
    # A caller-supplied timeout is never trusted past this deployment's ceiling.
    timeout = min(timeout, settings.network_probe_read_timeout_seconds)

    async with outbound_guard.guard(org_id):
        result = await _live_probe_inner(source_type, config, org_id, timeout)

    logger.info(
        "connection.live_probe", org_id=org_id, source_type=source_type,
        status=result.status, message=result.message,
    )
    return result


async def _live_probe_inner(source_type: str, config: dict[str, Any], org_id: str | None, timeout: float) -> ConnectionCheck:
    # ── API family: real HTTP GET ──
    if source_type in {"api", "rest", "webhook"}:
        base = config.get("base_url") or config.get("url")
        if not base:
            return ConnectionCheck("failed", "Missing base_url/url", {})
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"}:
            return ConnectionCheck("failed", "URL must be http or https", {"url": base})
        target = base.rstrip("/")
        health = config.get("health_path")
        if health:
            target = base.rstrip("/") + "/" + str(health).lstrip("/")
        start = time.perf_counter()
        try:
            resp = await safe_get(target, org_id=org_id)
            ms = round((time.perf_counter() - start) * 1000, 1)
            reachable = resp.status_code < 500
            return ConnectionCheck(
                "ok" if reachable else "failed",
                f"HTTP {resp.status_code} from {target}",
                {"target": target, "status_code": resp.status_code, "latency_ms": ms},
            )
        except DestinationBlockedError as exc:
            return ConnectionCheck("failed", str(exc), {"target": target})
        except Exception as exc:
            return ConnectionCheck("failed", f"Request failed: {exc}", {"target": target})

    # ── Object stores: reachability of the storage endpoint on :443 ──
    if source_type in {"s3", "gcs", "azure_blob"}:
        host = None
        s3_port = 443
        if source_type == "s3":
            bucket = config.get("bucket")
            region = config.get("region", "us-east-1")
            host = f"{bucket}.s3.{region}.amazonaws.com" if bucket else None
            # An S3-compatible store (MinIO, Ceph, ...) is reached through its own endpoint, not AWS.
            if config.get("endpoint"):
                u = urlparse(config["endpoint"] if "//" in config["endpoint"] else f"//{config['endpoint']}")
                host = u.hostname
                s3_port = u.port or (443 if u.scheme == "https" else 80)
        elif source_type == "gcs":
            host = "storage.googleapis.com"
        elif source_type == "azure_blob":
            account = config.get("account")
            host = f"{account}.blob.core.windows.net" if account else "blob.core.windows.net"
        if not host:
            return ConnectionCheck("failed", "Missing bucket/account for reachability probe", {})
        if blocked := await _validated(host, org_id):
            return blocked
        probe_port = s3_port
        ok_, ms, e = await _tcp_probe(host, probe_port, timeout)
        return ConnectionCheck(
            "ok" if ok_ else "failed",
            f"Reachable {host}:{probe_port}" if ok_ else f"Cannot reach {host}:{probe_port} — {e}",
            {"host": host, "port": probe_port, "latency_ms": ms},
        )

    # ── Local file: does the path exist on the server ──
    if source_type == "file":
        path = config.get("path")
        if not path:
            return ConnectionCheck("failed", "Missing path", {})
        exists = os.path.exists(path)
        return ConnectionCheck(
            "ok" if exists else "failed",
            f"Path exists: {path}" if exists else f"Path not found on server: {path}",
            {"path": path, "exists": exists},
        )

    # ── PostgreSQL: real auth check (login + SELECT version) when creds are given ──
    if source_type in {"postgres", "postgresql"} and config.get("username"):
        host = config.get("hostname") or config.get("host")
        if not host:
            return ConnectionCheck("failed", "Missing hostname", {})
        if blocked := await _validated(host, org_id):
            return blocked
        port = int(config.get("port") or 5432)
        db = config.get("database")
        start = time.perf_counter()
        try:
            import asyncpg

            conn = await asyncio.wait_for(
                asyncpg.connect(
                    host=host, port=port, user=config.get("username"),
                    password=config.get("password"), database=db, timeout=timeout,
                ),
                timeout=timeout + 1,
            )
            try:
                ver = await conn.fetchval("SELECT version()")
            finally:
                await conn.close()
            ms = round((time.perf_counter() - start) * 1000, 1)
            return ConnectionCheck(
                "ok",
                f"Authenticated to PostgreSQL {host}:{port}/{db}",
                {"host": host, "port": port, "database": db, "auth": True,
                 "server_version": (str(ver) or "")[:80], "latency_ms": ms},
            )
        except ModuleNotFoundError:
            pass  # asyncpg missing → fall through to TCP reachability below
        except Exception as exc:
            ms = round((time.perf_counter() - start) * 1000, 1)
            return ConnectionCheck(
                "failed",
                f"PostgreSQL auth/connect failed: {exc}",
                {"host": host, "port": port, "database": db, "auth": False, "latency_ms": ms},
            )

    # ── MySQL: real auth check (login + SELECT VERSION()) when creds are given. Before 2026-11-02 only the port was probed, so a wrong
    # password reported "Reachable mysql:3306" and the pipeline failed only at deploy. ──
    if source_type == "mysql" and config.get("username"):
        host = config.get("hostname") or config.get("host")
        if not host:
            return ConnectionCheck("failed", "Missing hostname", {})
        if blocked := await _validated(host, org_id):
            return blocked
        port = int(config.get("port") or 3306)
        db = config.get("database")
        start = time.perf_counter()
        try:
            import aiomysql

            conn = await asyncio.wait_for(
                aiomysql.connect(
                    host=host, port=port, user=config.get("username"), password=config.get("password") or "",
                    db=db, connect_timeout=timeout,
                ),
                timeout=timeout + 1,
            )
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT VERSION()")
                    ver = (await cur.fetchone() or [""])[0]
            finally:
                conn.close()
            ms = round((time.perf_counter() - start) * 1000, 1)
            return ConnectionCheck(
                "ok",
                f"Authenticated to MySQL {host}:{port}/{db}",
                {"host": host, "port": port, "database": db, "auth": True,
                 "server_version": (str(ver) or "")[:80], "latency_ms": ms},
            )
        except ModuleNotFoundError:
            pass  # aiomysql missing → fall through to TCP reachability below
        except Exception as exc:
            ms = round((time.perf_counter() - start) * 1000, 1)
            return ConnectionCheck(
                "failed",
                f"MySQL auth/connect failed: {exc}",
                {"host": host, "port": port, "database": db, "auth": False, "latency_ms": ms},
            )

    if source_type in NO_REMOTE_TYPES:
        return ConnectionCheck("ok", "No external system to reach for this connection type", {})

    # ── URL-configured types (HDFS, Neo4j, Splunk, Qdrant, ...): reachability of the URL's host:port ──
    if not (config.get("hostname") or config.get("host")):
        url = next((config[k] for k in URL_KEYS if config.get(k)), None)
        if url:
            u = urlparse(url if "//" in url else f"//{url}")
            if not u.hostname:
                return ConnectionCheck("failed", f"Cannot read a host from {url}", {"url": url})
            default_port = {"https": 443, "http": 80, "hdfs": 8020, "bolt": 7687, "neo4j": 7687, "ws": 80, "wss": 443,
                            "tcp": 61616, "t3": 7001, "jdbc": 443}.get(u.scheme, 443)
            try:
                url_port = u.port or default_port
            except ValueError:
                return ConnectionCheck("failed", f"Invalid port in {url}", {"url": url})
            if blocked := await _validated(u.hostname, org_id):
                return blocked
            ok_, ms, e = await _tcp_probe(u.hostname, url_port, timeout)
            return ConnectionCheck(
                "ok" if ok_ else "failed",
                f"Reachable {u.hostname}:{url_port}" if ok_ else f"Cannot reach {u.hostname}:{url_port} — {e}",
                {"host": u.hostname, "port": url_port, "latency_ms": ms},
            )
        if source_type in FIXED_HOSTS or source_type in {"datadog", "newrelic"}:
            fixed = FIXED_HOSTS.get(source_type)
            if source_type == "aws_eventbridge":
                fixed = f"events.{config.get('region') or 'us-east-1'}.amazonaws.com"
            elif source_type == "datadog":
                fixed = f"http-intake.logs.{config.get('site') or 'datadoghq.com'}"
            elif source_type == "newrelic":
                fixed = "insights-collector.eu01.nr-data.net" if str(config.get("region")).upper() == "EU" else "insights-collector.newrelic.com"
            if blocked := await _validated(fixed, org_id):
                return blocked
            ok_, ms, e = await _tcp_probe(fixed, 443, timeout)
            return ConnectionCheck(
                "ok" if ok_ else "failed",
                f"Reachable {fixed}:443" if ok_ else f"Cannot reach {fixed}:443 — {e}",
                {"host": fixed, "port": 443, "latency_ms": ms},
            )

    # ── Databases + FTP/SFTP: TCP reachability of host:port ──
    host = config.get("hostname") or config.get("host")
    if not host:
        return ConnectionCheck("failed", "Missing hostname", {})
    port = config.get("port") or DEFAULT_PORTS.get(source_type)
    if not port:
        return ConnectionCheck("failed", f"Missing port (no default for '{source_type}')", {})
    try:
        port = int(port)
    except (TypeError, ValueError):
        return ConnectionCheck("failed", f"Invalid port: {port}", {})
    if blocked := await _validated(host, org_id):
        return blocked
    ok_, ms, e = await _tcp_probe(host, port, timeout)
    return ConnectionCheck(
        "ok" if ok_ else "failed",
        f"Reachable {host}:{port}" if ok_ else f"Cannot reach {host}:{port} — {e}",
        {"host": host, "port": port, "latency_ms": ms},
    )

