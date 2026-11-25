"""CatalogAdapter — the one seam between the Object Explorer API
(app/api/catalog.py) and wherever catalog metadata actually comes from
(WS-CAT-008).

Strict precedence, in this order, for every browse call:

  1. UCS canonical discovery — the governance control plane's live view of
     registered Gravitino catalogs. This is the *only* source used outside
     dev mode. The connection is linked to a UCS catalog either explicitly
     (its `catalog_name` is a UCS catalog) or, failing that, by matching its
     own target against the non-secret JDBC target UCS reports for each
     relational catalog (see match_ucs_catalog). Registering a source with
     UCS is still an admin act in UCS itself; this only recognises a source
     that is already registered.

  2. Direct introspection — workspace opens a socket to the connection's own
     host/port itself. Dev-mode only (`settings.environment == "dev"`), and
     always behind an egress/host policy check, because
     `DatabaseConnection.host`/`.port` are user-supplied and otherwise a
     ready-made SSRF primitive (this was previously ungated in every
     environment).

When neither source has the data, the call raises CatalogUnavailableError
with the real reason (unregistered in UCS, TLS rejected, bad credentials, …).
There is deliberately no placeholder data: a fabricated tree is
indistinguishable from a real one in the UI and hid exactly those failures.
"""
from __future__ import annotations

import structlog

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.egress_policy import EgressPolicyError, assert_host_allowed
from app.models import DatabaseConnection
from app.schemas.catalog import CatalogColumn, CatalogNamespace, CatalogTable
from app.services.ucs_client import UcsUnavailableError, get_explorer_tree, get_object_columns

log = structlog.get_logger()

_PG_TYPES = ("postgres", "pg_lake")
_DEFAULT_PG_PORT = 5432


class CatalogUnavailableError(Exception):
    """No source could serve this browse call; `reason` is user-facing."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def match_ucs_catalog(
    tree: dict, *, connection_type: str, host: str | None, port: int | None,
    database: str | None, username: str | None, dev_mode: bool,
) -> str | None:
    """Name of the UCS relational catalog that points at the same Postgres
    database as this connection, or None.

    Database name and port must match. A catalog whose host also matches wins.
    Only in dev mode, where UCS and Workspace reach the same server under
    different docker hostnames (`postgres` vs `host.docker.internal`), a
    host mismatch is tolerated when the user also matches and every such
    candidate points at one single target — so it can't pick between two
    genuinely different servers.
    """
    if connection_type not in _PG_TYPES or not database:
        return None
    want_port = port or _DEFAULT_PG_PORT
    candidates = []
    for name, node in tree.items():
        target = node.get("connection") if node.get("engine") == "relational" else None
        if not target or target.get("database") != database:
            continue
        if (target.get("port") or _DEFAULT_PG_PORT) != want_port:
            continue
        candidates.append((name, target))
    if not candidates:
        return None

    same_host = sorted(n for n, t in candidates if host and (t.get("host") or "").lower() == host.lower())
    if same_host:
        return same_host[0]
    if not dev_mode or not username:
        return None
    same_user = [(n, t) for n, t in candidates if t.get("user") == username]
    targets = {(t.get("host"), t.get("port"), t.get("database")) for _, t in same_user}
    if len(targets) == 1:
        return sorted(n for n, _ in same_user)[0]
    return None


async def resolve_ucs_catalog(conn: DatabaseConnection, tree: dict) -> str | None:
    """The UCS catalog this connection browses through: its own
    `catalog_name` if UCS has it, else an auto-match on its target."""
    if conn.catalog_name and conn.catalog_name in tree:
        return conn.catalog_name
    username = None
    if conn.connection_type in _PG_TYPES and conn.vault_secret_path:
        from app.api.catalog import _load_db_creds
        username = (await _load_db_creds(conn.vault_secret_path)).get("username")
    return match_ucs_catalog(
        tree, connection_type=conn.connection_type, host=conn.host, port=conn.port,
        database=conn.database_name, username=username,
        dev_mode=get_settings().environment == "dev",
    )


class CatalogAdapter:
    async def list_namespaces(self, conn: DatabaseConnection, principal: Principal) -> list[CatalogNamespace]:
        name, node, ucs_reason = await self._ucs_catalog(conn, principal)
        if node is not None:
            return [CatalogNamespace(name=s, type="schema") for s in node.get("schemas", {})]
        return await self._direct(conn, ucs_reason, self._dev_direct_namespaces(conn))

    async def list_tables(
        self, conn: DatabaseConnection, namespace: str, principal: Principal,
    ) -> list[CatalogTable]:
        name, node, ucs_reason = await self._ucs_catalog(conn, principal)
        if node is not None:
            objs = node.get("schemas", {}).get(namespace, [])
            return [
                CatalogTable(
                    name=o["name"], schema_name=namespace, catalog_name=name,
                    table_type=o.get("type", "table"),
                )
                for o in objs
            ]
        return await self._direct(conn, ucs_reason, self._dev_direct_tables(conn, namespace))

    async def list_columns(
        self, conn: DatabaseConnection, namespace: str, table: str, principal: Principal,
    ) -> list[CatalogColumn]:
        name, node, ucs_reason = await self._ucs_catalog(conn, principal)
        if node is not None:
            try:
                cols = await get_object_columns(principal, f"{name}.{namespace}.{table}")
                return [
                    CatalogColumn(
                        name=c.get("name", ""),
                        data_type=_type_name(c.get("type") or c.get("data_type")),
                        is_nullable=c.get("nullable", True),
                    )
                    for c in cols
                ]
            except UcsUnavailableError as e:
                log.warning("ucs_discovery_unavailable", connection_id=str(conn.id), error=str(e))
                ucs_reason = f"Unified Catalog couldn't return columns: {e}"
        return await self._direct(conn, ucs_reason, self._dev_direct_columns(conn, namespace, table))

    # ── UCS tier ─────────────────────────────────────────────────────────

    async def _ucs_catalog(
        self, conn: DatabaseConnection, principal: Principal,
    ) -> tuple[str | None, dict | None, str]:
        """(catalog name, conn's subtree of UCS's explorer tree, reason if none)."""
        try:
            tree = await get_explorer_tree(principal)
        except UcsUnavailableError as e:
            log.warning("ucs_discovery_unavailable", connection_id=str(conn.id), error=str(e))
            return None, None, f"Unified Catalog is unavailable ({e})"
        name = await resolve_ucs_catalog(conn, tree)
        if name is None:
            return None, None, "this connection isn't linked to a source registered in the Unified Catalog"
        return name, tree[name], ""

    def _dev_mode(self) -> bool:
        return get_settings().environment == "dev"

    async def _direct(self, conn: DatabaseConnection, ucs_reason: str, attempt):
        """Fall back to direct introspection (dev only), or raise with why not."""
        if not self._dev_mode():
            attempt.close()  # never awaited outside dev mode
            raise CatalogUnavailableError(f"Can't browse '{conn.name}': {ucs_reason}.")
        try:
            return await attempt
        except EgressPolicyError as e:
            log.warning("egress_policy_blocked", connection_id=str(conn.id), host=conn.host, reason=e.reason)
            raise CatalogUnavailableError(f"Host not permitted: {e.reason}") from e
        except CatalogUnavailableError:
            raise
        except Exception as e:
            log.warning("catalog_direct_introspection_failed", connection_id=str(conn.id), error=str(e))
            raise CatalogUnavailableError(
                f"Couldn't read the catalog of '{conn.name}': {explain_connect_error(e)}"
            ) from e

    # ── Dev-mode direct introspection tier ──────────────────────────────
    # Deferred imports of app.api.catalog's helpers to avoid a circular
    # import (that module imports CatalogAdapter for the browse endpoints).

    async def _dev_direct_namespaces(self, conn: DatabaseConnection) -> list[CatalogNamespace]:
        if conn.connection_type in _PG_TYPES:
            await assert_host_allowed(conn.host or "")
            from app.api.catalog import _pg_catalog_query
            rows = await _pg_catalog_query(
                conn,
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT LIKE 'pg_%' AND schema_name <> 'information_schema' "
                "ORDER BY schema_name",
            )
            return [CatalogNamespace(name=r["schema_name"], type="schema") for r in rows]
        return await self._dev_lakehouse_get(
            "/v1/catalog/namespaces",
            lambda data: [CatalogNamespace(name=n["name"], type="schema") for n in data.get("namespaces", [])],
        )

    async def _dev_direct_tables(self, conn: DatabaseConnection, namespace: str) -> list[CatalogTable]:
        if conn.connection_type in _PG_TYPES:
            await assert_host_allowed(conn.host or "")
            from app.api.catalog import _pg_catalog_query
            rows = await _pg_catalog_query(
                conn,
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = $1 ORDER BY table_name",
                namespace,
            )
            return [
                CatalogTable(
                    name=r["table_name"], schema_name=namespace, catalog_name=conn.catalog_name,
                    table_type="view" if r["table_type"] == "VIEW" else "table",
                )
                for r in rows
            ]
        return await self._dev_lakehouse_get(
            f"/v1/catalog/namespaces/{namespace}/tables",
            lambda data: [
                CatalogTable(name=t["name"], schema_name=namespace, catalog_name=conn.catalog_name)
                for t in data.get("tables", [])
            ],
        )

    async def _dev_direct_columns(
        self, conn: DatabaseConnection, namespace: str, table: str,
    ) -> list[CatalogColumn]:
        if conn.connection_type in _PG_TYPES:
            await assert_host_allowed(conn.host or "")
            from app.api.catalog import _pg_catalog_query
            rows = await _pg_catalog_query(
                conn,
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
                namespace, table,
            )
            return [
                CatalogColumn(name=r["column_name"], data_type=r["data_type"], is_nullable=(r["is_nullable"] == "YES"))
                for r in rows
            ]
        return await self._dev_lakehouse_get(
            f"/v1/catalog/namespaces/{namespace}/tables/{table}/schema",
            lambda data: [
                CatalogColumn(name=c["name"], data_type=c.get("type", "string"), is_nullable=c.get("nullable", True))
                for c in data.get("columns", [])
            ],
        )

    @staticmethod
    async def _dev_lakehouse_get(path: str, parse):
        """The lakehouse URL is fixed deployment config (settings.lakehouse_query_url),
        never user input — no egress check needed for this branch, unlike the
        postgres/pg_lake one above which targets a user-supplied host."""
        import httpx
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.lakehouse_query_url}{path}")
            if resp.status_code != 200:
                raise CatalogUnavailableError(f"Lakehouse catalog returned HTTP {resp.status_code}")
            return parse(resp.json())


def _type_name(t) -> str:
    """Gravitino reports simple types as strings ("integer") but others as
    objects, e.g. {"type": "external", "catalogString": "bigserial"}."""
    if isinstance(t, dict):
        return str(t.get("catalogString") or t.get("name") or t.get("type") or "unknown")
    return str(t) if t else "unknown"


def explain_connect_error(e: Exception) -> str:
    """The driver's message, plus the likely fix for the common cases."""
    msg = str(e)[:300]
    if "rejected SSL upgrade" in msg:
        return f"{msg}. The server doesn't support TLS — edit the connection and turn off \"Use TLS / SSL\"."
    if "password authentication failed" in msg:
        return f"{msg}. Check the connection's username and password."
    return msg


_adapter: CatalogAdapter | None = None


def get_catalog_adapter() -> CatalogAdapter:
    global _adapter
    if _adapter is None:
        _adapter = CatalogAdapter()
    return _adapter
