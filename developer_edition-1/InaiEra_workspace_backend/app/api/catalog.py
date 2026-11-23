"""Catalog API — database connections and Object Explorer.

Two surfaces in one router:
  1. /api/v1/connections — CRUD for named database connections (the
     "Add new connection" flow the user sees in the Object Explorer panel).
     These are org-scoped, not workspace-scoped, so a connection added once
     is available in all workspaces. Credentials go to the secrets vault;
     only the vault path is stored in the DB row.

  2. /api/v1/connections/{id}/catalog/* — lightweight catalog browse
     endpoints that proxy to the target engine via the connection's
     credentials. These power the Object Explorer tree (databases →
     schemas → tables → columns) without the user needing to run SQL.
"""
from __future__ import annotations

import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.deps import CurrentUser, DB
from app.core.egress_policy import EgressPolicyError, assert_host_allowed
from app.core.errors import WorkspaceError, conflict, flush_or_conflict, forbidden, not_found, validation_failed
from app.core.rbac import RequireCatalogBrowse, RequireConnectionsManage
from app.models import DatabaseConnection
from app.schemas.catalog import (
    CatalogColumn, CatalogNamespace, CatalogTable,
    ConnectionTestResult, DatabaseConnectionCreate,
    DatabaseConnectionOut, DatabaseConnectionUpdate, UcsCatalogOption,
)
from app.services.audit_service import AuditService
from app.services.catalog_adapter import (
    CatalogUnavailableError, explain_connect_error, get_catalog_adapter, match_ucs_catalog,
)
from app.services.ucs_client import (
    UcsAuthorizationError, UcsUnavailableError, _ucs_configured, authorize_connection_use, get_explorer_tree,
)
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/connections", tags=["catalog"])


# ── CRUD ──────────────────────────────────────────────────────────────────

@router.get("", response_model=list[DatabaseConnectionOut])
async def list_connections(user: CurrentUser, db: DB) -> list[DatabaseConnectionOut]:
    """List all database connections for this org."""
    settings = get_settings()
    result = await db.execute(
        select(DatabaseConnection)
        .where(DatabaseConnection.org_id == settings.org_id)
        .order_by(DatabaseConnection.name)
    )
    return [DatabaseConnectionOut.model_validate(c) for c in result.scalars()]


@router.get("/ucs-catalogs", response_model=list[UcsCatalogOption])
async def list_ucs_catalogs(user: RequireConnectionsManage) -> list[UcsCatalogOption]:
    """Relational and lakehouse catalogs registered in the Unified Catalog,
    with the non-secret target UCS reaches each relational one at — feeds the
    "Unified Catalog source" picker in the connection form. Empty (not an
    error) when UCS is unreachable: the form still works, just unlinked."""
    try:
        tree = await get_explorer_tree(user)
    except UcsUnavailableError:
        return []
    out = []
    for name, node in sorted(tree.items()):
        engine = node.get("engine")
        if engine not in ("relational", "iceberg"):
            continue
        t = node.get("connection") or {}
        out.append(UcsCatalogOption(
            name=name, engine=engine, host=t.get("host"), port=t.get("port"),
            database=t.get("database"), username=t.get("user"),
        ))
    return out


@router.post("", response_model=DatabaseConnectionOut, status_code=status.HTTP_201_CREATED)
async def create_connection(
    payload: DatabaseConnectionCreate, user: RequireConnectionsManage, db: DB, request: Request,
) -> DatabaseConnectionOut:
    """Register a new database connection.

    Credentials (username/password) are written to the secrets vault and
    only the vault path is stored on the row. The API response never
    returns credentials.
    """
    settings = get_settings()
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)

    catalog_name = payload.catalog_name or await _auto_link_ucs_catalog(
        user, payload.connection_type, payload.host, payload.port,
        payload.database_name, payload.username,
    )

    # Name clash first: the secret must never be written for a row that
    # can't be inserted (WS-SEC-002 — keyed by name, it used to overwrite the
    # existing connection's credentials before the unique constraint fired).
    await _assert_name_free(db, settings.org_id, payload.name)
    conn_id = uuid4()
    ucs_resource_fqn = await _resolve_ucs_identity(user, conn_id, catalog_name, verified=not payload.catalog_name)

    vault_path: str | None = None
    if payload.username or payload.password:
        vault_path = await _write_credentials_to_vault(
            org_id=settings.org_id,
            conn_id=conn_id,
            username=payload.username,
            password=payload.password,
        )

    conn = DatabaseConnection(
        id=conn_id,
        org_id=settings.org_id,
        created_by_user_id=user_row.id,
        name=payload.name,
        description=payload.description,
        connection_type=payload.connection_type,
        host=payload.host,
        port=payload.port,
        database_name=payload.database_name,
        catalog_name=catalog_name,
        ucs_resource_fqn=ucs_resource_fqn,
        vault_secret_path=vault_path,
        use_tls=payload.use_tls,
        extra_properties=payload.extra_properties,
    )
    db.add(conn)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a race on the name: don't leave the new secret behind.
        await db.rollback()
        if vault_path:
            await _delete_vault_secret(vault_path)
        raise conflict(f"A connection named '{payload.name}' already exists.", code="CONNECTION_NAME_TAKEN", resource="connection", field="name")

    await audit.record(
        user_id=user_row.id,
        action="connection.create",
        resource_type="database_connection",
        resource_id=conn.id,
        ip_address=request.client.host if request.client else None,
        metadata={"name": conn.name, "type": conn.connection_type, "host": conn.host},
    )
    return DatabaseConnectionOut.model_validate(conn)


@router.get("/{connection_id}", response_model=DatabaseConnectionOut)
async def get_connection(connection_id: UUID, user: CurrentUser, db: DB) -> DatabaseConnectionOut:
    conn = await _get_conn(db, connection_id)
    return DatabaseConnectionOut.model_validate(conn)


@router.patch("/{connection_id}", response_model=DatabaseConnectionOut)
async def update_connection(
    connection_id: UUID, payload: DatabaseConnectionUpdate, user: RequireConnectionsManage, db: DB, request: Request,
) -> DatabaseConnectionOut:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    conn = await _get_conn(db, connection_id)
    settings = get_settings()

    new_name = payload.name if payload.name is not None and payload.name != conn.name else None
    if new_name:
        await _assert_name_free(db, conn.org_id, new_name)
        conn.name = new_name
    if payload.description is not None:
        conn.description = payload.description
    if payload.host is not None:
        conn.host = payload.host
    if payload.port is not None:
        conn.port = payload.port
    if payload.database_name is not None:
        conn.database_name = payload.database_name
    relink = False
    explicit_link = bool(payload.catalog_name)
    if payload.catalog_name is not None:
        # "" clears the link; it's then re-detected below.
        new_link = payload.catalog_name or None
        relink = new_link != conn.catalog_name
        conn.catalog_name = new_link
    if payload.use_tls is not None:
        conn.use_tls = payload.use_tls
    if payload.extra_properties is not None:
        conn.extra_properties = payload.extra_properties
    if payload.username or payload.password:
        username, password = payload.username, payload.password
        # A partial update (only username, or only password) keeps the other
        # stored field rather than blanking it. Read the backend directly: a
        # lookup failure must fail the request, not silently wipe a credential.
        if not (username and password) and conn.vault_secret_path:
            from app.storage import get_secrets_backend
            existing = await get_secrets_backend().get(conn.vault_secret_path) or {}
            username = username or existing.get("username")
            password = password or existing.get("password")
        # Same secret, updated in place — the path is the connection's, so a
        # rename never forks or orphans it.
        conn.vault_secret_path = await _write_credentials_to_vault(
            org_id=settings.org_id,
            conn_id=conn.id,
            username=username,
            password=password,
            path=conn.vault_secret_path,
        )
        conn.is_verified = False  # re-verify after cred change
    else:
        username = None

    if not conn.catalog_name:
        if username is None and conn.vault_secret_path:
            username = (await _load_db_creds(conn.vault_secret_path)).get("username")
        detected = await _auto_link_ucs_catalog(
            user, conn.connection_type, conn.host, conn.port, conn.database_name, username,
        )
        relink = relink or detected != conn.catalog_name
        conn.catalog_name = detected
        explicit_link = False

    # W-011: the governed identity changes only when the UCS link does — never because the connection was renamed.
    if relink or not conn.ucs_resource_fqn:
        conn.ucs_resource_fqn = await _resolve_ucs_identity(user, conn.id, conn.catalog_name, verified=not explicit_link)

    await flush_or_conflict(db, f"A connection named '{new_name or conn.name}' already exists.", constraint="uq_dbconn_org_name")
    # Reload what the database just changed (updated_at is set by the server): serialising an expired attribute in the async context
    # raised MissingGreenlet, so ANY successful update of a connection answered 500 against a real database.
    await db.refresh(conn)
    await audit.record(
        user_id=user_row.id, action="connection.update",
        resource_type="database_connection", resource_id=conn.id,
        ip_address=request.client.host if request.client else None,
        metadata=payload.model_dump(exclude_none=True, exclude={"username", "password"}),
    )
    return DatabaseConnectionOut.model_validate(conn)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: UUID, user: RequireConnectionsManage, db: DB, request: Request,
) -> None:
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    conn = await _get_conn(db, connection_id)
    vault_path = conn.vault_secret_path
    await db.delete(conn)
    await db.flush()
    # Remove its credentials too (they used to be orphaned forever). If the
    # backend refuses, the request fails and the row delete rolls back, so a
    # retry can finish both — never a row-less secret.
    if vault_path:
        await _delete_vault_secret(vault_path, strict=True)
    await audit.record(
        user_id=user_row.id, action="connection.delete",
        resource_type="database_connection", resource_id=connection_id,
        ip_address=request.client.host if request.client else None,
    )


# ── Test / verify ──────────────────────────────────────────────────────────

@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
async def test_connection(
    connection_id: UUID, user: RequireConnectionsManage, db: DB, request: Request,
) -> ConnectionTestResult:
    """Attempt a lightweight connection to the target engine and measure
    latency. Updates is_verified + last_verified_at on success."""
    from datetime import datetime, timezone
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    conn = await _get_conn(db, connection_id)
    # A live probe actually authenticates to the target with the connection's
    # real credentials — that's "using" it, the same act connection.query
    # gates for execution (WS-DATA-007). Existence of the row (any org user
    # can list connections) is not permission to make it dial out.
    try:
        await authorize_connection_use(user, user_row.id, conn)
    except UcsAuthorizationError as e:
        raise forbidden(f"Not authorized to use connection '{conn.name}': {e.reason}")
    result = await _run_connection_test(conn)
    if result.success:
        conn.is_verified = True
        conn.last_verified_at = datetime.now(timezone.utc)
        await db.flush()
    await audit.record(
        user_id=user_row.id, action="connection.test",
        resource_type="database_connection", resource_id=conn.id,
        ip_address=request.client.host if request.client else None,
        metadata={"success": result.success, "latency_ms": result.latency_ms},
    )
    return result


# ── Catalog browse (Object Explorer) ──────────────────────────────────────

@router.get("/{connection_id}/catalog/namespaces", response_model=list[CatalogNamespace])
async def list_namespaces(connection_id: UUID, user: RequireCatalogBrowse, db: DB) -> list[CatalogNamespace]:
    """List schemas/namespaces for this connection.

    Sourced from UCS's canonical catalog discovery (see CatalogAdapter) —
    direct introspection and placeholder data are dev-mode-only fallbacks,
    never used in staging/prod (WS-CAT-008).
    """
    conn = await _get_conn(db, connection_id)
    try:
        return await get_catalog_adapter().list_namespaces(conn, user)
    except CatalogUnavailableError as e:
        raise _catalog_unavailable(e)


@router.get("/{connection_id}/catalog/namespaces/{namespace}/tables", response_model=list[CatalogTable])
async def list_tables(
    connection_id: UUID, namespace: str, user: RequireCatalogBrowse, db: DB,
) -> list[CatalogTable]:
    """List tables in a schema/namespace. See list_namespaces docstring."""
    conn = await _get_conn(db, connection_id)
    try:
        return await get_catalog_adapter().list_tables(conn, namespace, user)
    except CatalogUnavailableError as e:
        raise _catalog_unavailable(e)


@router.get("/{connection_id}/catalog/namespaces/{namespace}/tables/{table}/columns",
            response_model=list[CatalogColumn])
async def list_columns(
    connection_id: UUID, namespace: str, table: str, user: RequireCatalogBrowse, db: DB,
) -> list[CatalogColumn]:
    """Get column definitions for a table (used by codegen context assembly).
    See list_namespaces docstring."""
    conn = await _get_conn(db, connection_id)
    try:
        return await get_catalog_adapter().list_columns(conn, namespace, table, user)
    except CatalogUnavailableError as e:
        raise _catalog_unavailable(e)


# ── Helpers ───────────────────────────────────────────────────────────────

def _catalog_unavailable(e: CatalogUnavailableError) -> WorkspaceError:
    return WorkspaceError(502, "CATALOG_UNAVAILABLE", e.reason)


async def _auto_link_ucs_catalog(
    principal, connection_type: str, host: str | None, port: int | None,
    database: str | None, username: str | None,
) -> str | None:
    """The UCS catalog registered for the same database, if exactly
    identifiable (see match_ucs_catalog). Best effort: None if UCS is down."""
    try:
        tree = await get_explorer_tree(principal)
    except UcsUnavailableError:
        return None
    return match_ucs_catalog(
        tree, connection_type=connection_type, host=host, port=port,
        database=database, username=username,
        dev_mode=get_settings().environment == "dev",
    )


async def _resolve_ucs_identity(principal, conn_id: UUID, catalog_name: str | None, *, verified: bool = False) -> str:
    """The canonical UCS resource for a connection (W-011). An explicit `catalog_name` must be a catalog UCS really has — otherwise a
    connection could be pointed at a catalog its creator merely names (and borrow that catalog's grants); when UCS cannot be asked,
    an explicit link is refused (503) rather than accepted unverified. No link = the connection's own immutable id."""
    if catalog_name and verified:       # it came out of UCS's own tree a moment ago (auto-link)
        return catalog_name
    if catalog_name and not _ucs_configured():
        # No Unified Catalog in this deployment (development only; production requires UCS): there is nothing to link
        # to or borrow grants from, so the connection is governed by its own id. A Trino catalog name still reaches Trino.
        return f"{get_settings().data_policy_catalog}.connection.{conn_id}"
    if catalog_name:
        try:
            tree = await get_explorer_tree(principal)
        except UcsUnavailableError as e:
            raise WorkspaceError(503, "UCS_UNAVAILABLE",
                                 f"Cannot link to the Unified Catalog source '{catalog_name}': UCS could not be reached to verify it") from e
        if catalog_name not in tree:
            raise validation_failed("catalog_name", f"'{catalog_name}' is not a catalog registered in the Unified Catalog")
        return catalog_name
    return f"{get_settings().data_policy_catalog}.connection.{conn_id}"


async def _get_conn(db: DB, connection_id: UUID) -> DatabaseConnection:
    conn = await db.get(DatabaseConnection, connection_id)
    if not conn:
        raise not_found("database_connection", str(connection_id))
    return conn


async def _load_db_creds(vault_path: str | None) -> dict:
    """Fetch stored connection credentials from the secrets backend.

    No plaintext-on-disk fallback: a lookup miss or backend error simply
    yields no credentials, same as before when the file didn't have the key.
    """
    if not vault_path:
        return {}
    from app.storage import get_secrets_backend
    try:
        backend = get_secrets_backend()
        return await backend.get(vault_path) or {}
    except Exception as e:
        import structlog
        structlog.get_logger().error("db_creds_read_failed", vault_path=vault_path, error=str(e))
        return {}


async def _pg_catalog_query(conn, query: str, *args):
    """Connect to the connection's own Postgres and run a catalog query.

    Honors conn.use_tls — previously this always connected in plaintext
    regardless of that flag (WS-CAT-008): a connection an admin explicitly
    marked TLS-required would still have its catalog browsed unencrypted.
    """
    import asyncpg
    creds = await _load_db_creds(conn.vault_secret_path)
    c = await asyncpg.connect(
        host=conn.host or "localhost",
        port=conn.port or 5432,
        user=creds.get("username") or getattr(conn, "username", None) or "postgres",
        password=creds.get("password") or "",
        database=conn.database_name or "postgres",
        ssl="require" if conn.use_tls else None,
        timeout=8,
    )
    try:
        return await c.fetch(query, *args)
    finally:
        await c.close()


async def _assert_name_free(db, org_id: str, name: str) -> None:
    taken = (await db.execute(
        select(DatabaseConnection.id).where(
            DatabaseConnection.org_id == org_id, DatabaseConnection.name == name,
        )
    )).first()
    if taken:
        raise conflict(f"A connection named '{name}' already exists.", code="CONNECTION_NAME_TAKEN", resource="connection", field="name")


async def _delete_vault_secret(path: str, *, strict: bool = False) -> None:
    from app.storage import get_secrets_backend
    try:
        await get_secrets_backend().delete(path)
    except Exception as e:  # noqa: BLE001
        import structlog
        structlog.get_logger().error("connection_secret_delete_failed", error=str(e))
        if strict:
            raise WorkspaceError(502, "SECRETS_BACKEND_ERROR",
                                 "Couldn't remove the connection's credentials; nothing was deleted. Try again.")


async def _write_credentials_to_vault(
    org_id: str, conn_id, username: str | None, password: str | None,
    path: str | None = None,
) -> str:
    """Write credentials to the secrets backend and return the vault path.

    Delegates to the configured secrets backend (AWS Secrets Manager, Azure
    Key Vault, GCP Secret Manager, or — dev only — the local backend). Only
    this path is persisted on the DatabaseConnection row; the credential
    value itself never touches the metadata database or local disk.
    """
    from app.storage import get_secrets_backend
    # Keyed by the connection's id, under the deployment's prefix: unique for
    # the row's whole life (a name is neither unique over time nor stable).
    # Existing rows keep whatever path they already have.
    path = path or f"{get_settings().secrets_path_prefix}/connections/{org_id}/{conn_id}"
    backend = get_secrets_backend()
    await backend.put(path, {"username": username or "", "password": password or ""})
    return path


async def _run_connection_test(conn: DatabaseConnection) -> ConnectionTestResult:
    """Attempt a minimal query against the target engine.

    conn.host/.port are user-supplied — the trino branch below makes a
    server-side HTTP request straight to them, so it's gated on the same
    egress/host policy as dev-mode catalog introspection (WS-CAT-008),
    regardless of environment: this probe is a normal, always-available
    feature, not a dev-only path, so the policy is the only thing standing
    between it and SSRF. Postgres/pg_lake log in for real with the stored
    credentials and TLS flag, so "verified" means browsing and SQL will work.
    """
    import httpx
    start = time.monotonic()
    try:
        if conn.connection_type == "trino":
            try:
                await assert_host_allowed(conn.host or "")
            except EgressPolicyError as e:
                return ConnectionTestResult(
                    success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    error_message=f"Host not permitted: {e.reason}",
                )
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"http{'s' if conn.use_tls else ''}://{conn.host}:{conn.port or 443}/v1/info"
                )
                if resp.status_code == 200:
                    info = resp.json()
                    return ConnectionTestResult(
                        success=True,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        server_version=info.get("nodeVersion", {}).get("version"),
                    )
            return ConnectionTestResult(
                success=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                error_message=f"Trino returned HTTP {resp.status_code} from /v1/info",
            )
        elif conn.connection_type in ("pg_lake", "postgres"):
            # Log in with the stored credentials and TLS setting — the same
            # way browsing and SQL execution will connect.
            try:
                await assert_host_allowed(conn.host or "")
            except EgressPolicyError as e:
                return ConnectionTestResult(
                    success=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    error_message=f"Host not permitted: {e.reason}",
                )
            rows = await _pg_catalog_query(conn, "SELECT current_setting('server_version') AS v")
            return ConnectionTestResult(
                success=True,
                latency_ms=int((time.monotonic() - start) * 1000),
                server_version=f"PostgreSQL {rows[0]['v']}" if rows else None,
            )
        # Generic: just mark success for now
        return ConnectionTestResult(success=True, latency_ms=int((time.monotonic() - start) * 1000))
    except Exception as e:
        return ConnectionTestResult(
            success=False,
            latency_ms=int((time.monotonic() - start) * 1000),
            error_message=explain_connect_error(e),
        )
