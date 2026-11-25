"""FastAPI entry point for the Workspace service (data plane).

Wires every router, the WebSocket gateway, CORS, exception handlers, and
the startup/shutdown lifespan that verifies dependencies (Postgres, Redis,
blob store) are reachable.

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

In production, the Helm chart deploys this behind a per-org subdomain
(orgid.app.workspace.inaiera.com) with TLS terminated at the data plane
ingress.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from fastapi.exceptions import RequestValidationError
from gateway_auth import protect_app
from sqlalchemy import text

from app.api import auth as auth_api
from app.api import catalog as catalog_api
from app.api import codegen as codegen_api
from app.api import dbt_proxy as dbt_proxy_api
from app.api import dq_proxy as dq_proxy_api
from app.api import executions as executions_api
from app.api import files as files_api
from app.api import flink_proxy as flink_proxy_api
from app.api import git as git_api
from app.api import alert_actions as alert_actions_api
from app.api import alerts as alerts_api
from app.api import internal_jobs as internal_jobs_api
from app.api import settings as settings_api
from app.api import workspaces as workspaces_api
from app.core.errors import conflict_for_unique_violation
from app.core.config import get_settings
from app.core.logging_setup import configure_logging
from app.core.observability import RequestContextMiddleware, bind_path_context
from app.git import SUPPORTED_PROVIDERS
from app.db.session import AsyncSessionLocal, engine
from app.executors.local_kernel_executor import assert_local_kernel_safe_for_environment
from app.services.execution_router import get_execution_router
from app.services.ucs_client import assert_governance_for_environment
from app.services.alerts.sweeper import get_alert_sweeper
from app.services.lifecycle_service import get_lifecycle_reconciler
from app.storage import get_blob_store
from app.storage.secrets import (
    assert_secrets_backend_safe_for_environment, migrate_legacy_plaintext_credentials,
)
from app.ws.gateway import router as ws_router

log = structlog.get_logger()


def _configure_logging(level: str) -> None:
    # WS-016: one redacting pipeline for structlog AND stdlib loggers,
    # shared with the execution worker process (app.worker).
    configure_logging(level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)
    log.info(
        "workspace.starting",
        env=settings.environment,
        org_id=settings.org_id,
        storage=settings.storage_backend,
    )

    # Refuse to start with plaintext-on-disk credential storage outside dev.
    assert_secrets_backend_safe_for_environment()

    # Refuse to start with the unisolated in-process notebook kernel fallback
    # enabled outside dev (WS-RUNTIME-012).
    assert_local_kernel_safe_for_environment()

    # Refuse to run partially governed: the production profile requires UCS (W-010).
    assert_governance_for_environment()

    # One-time migration of any credentials left over from before this guard
    # existed (pre-fix plaintext files on the pod's local disk).
    try:
        await migrate_legacy_plaintext_credentials()
    except Exception as e:
        log.error("workspace.secrets_migration_failed", error=str(e))

    # Verify DB
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        log.info("workspace.db_ok")
    except Exception as e:
        log.error("workspace.db_unreachable", error=str(e))

    # Verify blob store
    try:
        store = get_blob_store()
        # Cheap probe: list with a non-existent prefix
        await store.list_prefix(f"{settings.org_id}/.healthcheck/")
        log.info("workspace.storage_ok", backend=settings.storage_backend)
    except Exception as e:
        log.warning("workspace.storage_probe_failed", error=str(e))

    # WS-EXEC-005: the execution worker (and the lifecycle reconciler that
    # goes with it) runs here only when this process is configured as a
    # worker too — dev's single process. In the Helm deployment API pods set
    # EXECUTION_WORKER_ENABLED=false and a separate `python -m app.worker`
    # Deployment runs them. A failure to start is fatal, not logged-and-
    # ignored: an API that accepts 202s with no worker would strand them.
    execution_router = get_execution_router()
    lifecycle_reconciler = get_lifecycle_reconciler()
    alert_sweeper = get_alert_sweeper()
    if settings.execution_worker_enabled:
        await execution_router.start()
        log.info("workspace.execution_worker_started")
        await lifecycle_reconciler.start()
        log.info("workspace.lifecycle_reconciler_started")
        # Alerts: the failure sweep (a failure to start it never blocks the API; an admin can still "Run rules now").
        try:
            await alert_sweeper.start()
            log.info("workspace.alert_sweeper_started")
        except Exception as e:  # noqa: BLE001
            log.error("workspace.alert_sweeper_failed_to_start", error=type(e).__name__)
    else:
        log.info("workspace.execution_worker_external")

    yield

    log.info("workspace.shutting_down")
    if settings.execution_worker_enabled:
        await alert_sweeper.stop()
        await lifecycle_reconciler.stop()
        await execution_router.stop()
    await engine.dispose()


app = FastAPI(
    title="InAiEra Workspace",
    description=(
        "Developer authoring surface for SQL and notebooks against the customer "
        "Data Lake House. Deployed inside the data plane on the customer's cloud. "
        "Identity and permissions come from Envoy (gateway-auth); every route "
        "checks a workspace:* permission. Files live in cloud object storage; metadata in the "
        "data plane Postgres."
    ),
    version="0.1.0",
    lifespan=lifespan,
    # WS-016: every route binds its path resource ids as log/audit context.
    dependencies=[Depends(bind_path_context)],
    # Hide schema docs in prod environments
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)


# --- Deny by default (contract 01-CONTRACT.md section 5) ---
# Every route needs a gateway-verified principal (TRUST_ENVOY_HEADERS=true + Envoy's x-inaiera-gateway-secret), or it is a 401,
# except this explicit, short list. Each entry is public for a stated reason; nothing here returns data about anyone.
PUBLIC_PATHS = (
    "/healthz", "/readyz",                     # liveness / readiness probes
    "/config.json",                            # the SPA's secret-free runtime config, read while logged out (W-006)
    "/api/v1/auth/logout",                     # clears the session cookie even when the credential is gone (W-004)
    "/api/v1/git/webhooks/*",                  # inbound Git webhooks: each verifies its provider signature
    # OAuth redirect back from the Git provider: verified by its single-use, user-bound `state`, not by the gateway.
    *(f"/api/v1/git/{p}/callback" for p in SUPPORTED_PROVIDERS),
    "/api/v1/internal/jobs/*",                 # called by the dbt runner, not through Envoy: verifies its own signed run token
    # The Acknowledge / Resolve buttons in a Slack or Teams message open these: the person has no Workspace session, and the
    # signed, expiring token in the link is the credential (HMAC, ALERTS_ACTION_SECRET; refused when no secret is set). A GET
    # only shows a confirm page, a POST acts. The page shows the alert's job name and message, nothing about any person.
    "/api/v1/alerts/actions/*",
)
# Added before CORS so CORS stays the outer layer (a browser preflight is answered before the gateway check).
protect_app(app, public_paths=PUBLIC_PATHS)

# --- CORS ---
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "If-Match"],
)

# --- Request/trace correlation (WS-AUDIT-016) ---
# Added last so it becomes the outermost layer (Starlette runs middleware in
# reverse-registration order) — request_id/trace_id should wrap everything
# else, including CORS handling, so no request is ever logged without them.
app.add_middleware(RequestContextMiddleware)


# --- Exception handlers ---
@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's own .errors() embeds the raw exception instance under
    # ctx["error"] for a `field_validator` that raises ValueError (e.g.
    # SqlExecutionRequest.warehouse, WS-DATA-007) — json.dumps can't
    # serialize an exception object, which turned every such validation
    # failure into an unhandled 500 instead of the clean 422 this handler
    # exists to return. Stringify it before it reaches JSONResponse.
    errors = exc.errors()
    for err in errors:
        ctx = err.get("ctx")
        if isinstance(ctx, dict) and isinstance(ctx.get("error"), BaseException):
            err["ctx"] = {**ctx, "error": str(ctx["error"])}
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "details": errors,
            }
        },
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(_: Request, exc: IntegrityError) -> JSONResponse:
    """X-008: a UNIQUE violation that no route translated (a race on a rule its pre-check did not cover) is still the standard 409 with a
    stable code, resource and field — never a raw database error. Anything else (a foreign key, a check) is a real bug: re-raised, a 500."""
    standard = conflict_for_unique_violation(exc)
    if standard is None:
        raise exc
    return JSONResponse(status_code=409, content={"detail": standard.detail})


# --- Routes ---
app.include_router(auth_api.router)
app.include_router(auth_api.me_router)
app.include_router(workspaces_api.router)
app.include_router(catalog_api.router)
app.include_router(dbt_proxy_api.router)
app.include_router(flink_proxy_api.router)
app.include_router(dq_proxy_api.router)
app.include_router(files_api.router)
app.include_router(executions_api.router)
app.include_router(codegen_api.router)
app.include_router(git_api.router)
app.include_router(settings_api.router)
app.include_router(internal_jobs_api.router)
app.include_router(alert_actions_api.router)
app.include_router(alerts_api.router)
app.include_router(ws_router)


# --- Health & meta ---
@app.get("/config.json", tags=["meta"], include_in_schema=False)
async def runtime_config() -> JSONResponse:
    """Deployment-specific values the SPA reads at start-up (W-006). Public (the SPA needs it while logged out) and secret-free."""
    return JSONResponse({"controlPlaneLoginUrl": get_settings().resolved_control_plane_login_url},
                        headers={"Cache-Control": "no-store"})


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok", "service": "workspace"}


@app.get("/readyz", tags=["meta"])
async def readyz():
    """Readiness for k8s: every dependency this pod can't serve without —
    the metadata DB, Redis (WebSocket fan-out, tickets, execution wake-ups),
    the blob store, and the in-process execution worker when this pod runs
    one. Liveness (/healthz) deliberately checks none of these, so a
    dependency outage takes the pod out of rotation instead of restart-
    looping it."""
    from app.services.execution_router import get_redis

    checks: dict[str, str] = {}

    async def check(name: str, probe) -> None:
        try:
            await asyncio.wait_for(probe(), timeout=3)
            checks[name] = "ok"
        except Exception as e:  # noqa: BLE001
            checks[name] = f"error: {str(e)[:200]}"

    async def db_probe():
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))

    async def redis_probe():
        await get_redis().ping()

    async def storage_probe():
        await get_blob_store().list_prefix(f"{_settings.org_id}/.healthcheck/")

    await check("db", db_probe)
    await check("redis", redis_probe)
    await check("storage", storage_probe)
    if _settings.execution_worker_enabled:
        checks["execution_worker"] = "ok" if get_execution_router().healthy() else "error: not running"

    ready = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if ready else "not_ready", **checks}
    return body if ready else JSONResponse(status_code=503, content=body)


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "service": "workspace",
        "version": "0.1.0",
        "org_id": _settings.org_id,
        "docs": "/docs",
    }
