"""FastAPI entry point for the FlinkSQL Runner service."""
from __future__ import annotations
import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api import jobs as jobs_api
from app.api import kafka_catalog as kafka_api
from app.api import validate as validate_api
from app.core.config import get_settings
from app.core.errors import conflict_for_unique_violation
from app.db.session import AsyncSessionLocal

log = structlog.get_logger()


# name -> task, for readiness (RUN-FLINK-010 / Helm readiness hardening).
BACKGROUND_TASKS: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # get_settings() already refused a placeholder ORG_ID outside dev
    # (RUN-FLINK-009) — importing this module fails before we get here.
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    log.info("flinksql_runner.starting", env=settings.environment, org_id=settings.org_id)
    if settings.environment != "dev" and settings.legacy_internal_token_allowed:
        log.warning("flinksql_runner.legacy_internal_token_enabled",
                    note="raw static INTERNAL_SERVICE_TOKEN accepted on /api/v1/validate")
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        log.info("flinksql_runner.db_ok")
    except Exception as e:
        log.error("flinksql_runner.db_unreachable", error=str(e))

    if settings.background_tasks_enabled:
        from app.services.deployment_reconciler import reconciler_loop
        from app.services.metrics_poller import metrics_poll_loop
        from app.services.quality_consumer import quality_consumer_loop
        # Poller and reconciler are leader-elected (one replica does the
        # work); the quality consumer runs on every replica (Kafka group
        # balancing + SKIP LOCKED outbox drain).
        BACKGROUND_TASKS["metrics_poller"] = asyncio.create_task(metrics_poll_loop())
        BACKGROUND_TASKS["quality_consumer"] = asyncio.create_task(quality_consumer_loop())
        BACKGROUND_TASKS["deployment_reconciler"] = asyncio.create_task(reconciler_loop())
        log.info("flinksql_runner.background_tasks_started", tasks=list(BACKGROUND_TASKS))

    yield

    for name, task in list(BACKGROUND_TASKS.items()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        BACKGROUND_TASKS.pop(name, None)
    log.info("flinksql_runner.shutting_down")


app = FastAPI(
    title="InAiEra FlinkSQL Runner",
    description=(
        "Streaming transformation service for the InAiEra ETL layer. "
        "Manages FlinkSQL job definitions, submits them to the Flink SQL Gateway, "
        "monitors running jobs, and provides the Kafka topic catalog for the Workspace UI."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

_s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_s.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Service-Token"],
)


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")


def _request_id_of(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid:
        return rid
    given = request.headers.get("x-request-id", "")
    return given if _REQUEST_ID.match(given) else uuid.uuid4().hex


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Every request has a correlation id: the caller's X-Request-ID when it is a sane token, else a fresh one. It is bound to the
    log context and echoed in the response, so an error a user reports can be found in the logs (FL-010)."""
    rid = _request_id_of(request)
    request.state.request_id = rid
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """X-008: a UNIQUE violation that no route translated is still the standard 409 (stable code, resource, field) — never a raw database
    error. Anything else (a foreign key, a check) is a real bug: the generic 500 below (no internal text, with a request id)."""
    standard = conflict_for_unique_violation(exc)
    if standard is None:
        return await global_handler(request, exc)
    return JSONResponse(status_code=409, content={"detail": standard.detail})


@app.exception_handler(Exception)
async def global_handler(request: Request, exc: Exception) -> JSONResponse:
    from fastapi import HTTPException
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    # FL-010: the exception text (driver errors carry hostnames, SQL fragments, internal service names) goes to the LOG with the
    # request id; the caller gets a stable generic body and that id.
    rid = _request_id_of(request)
    log.exception("unhandled_error", request_id=rid, path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse(status_code=500, headers={"X-Request-ID": rid},
                        content={"error": {"code": "INTERNAL_ERROR", "message": "An internal error occurred.",
                                           "request_id": rid}})


app.include_router(jobs_api.router)
app.include_router(kafka_api.router)
app.include_router(validate_api.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok", "service": "flinksql-runner"}


async def _probe(url: str, timeout: float) -> str | None:
    """None if `url` answers 2xx, else a short, GENERIC reason (the readiness body is unauthenticated: no hostnames or driver text)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.get(url)
        return None if resp.status_code < 300 else f"HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        log.warning("readiness_probe_failed", error=str(e)[:200])
        return "unreachable"


def _task_health(name: str) -> str | None:
    task = BACKGROUND_TASKS.get(name)
    if task is None:
        return "not started"
    if task.done():
        if task.cancelled():
            return "cancelled"
        exc = task.exception()
        if exc:
            log.error("background_task_crashed", task=name, error=repr(exc)[:300])
            return "crashed"
        return "exited"
    return None


@app.get("/readyz", tags=["meta"])
async def readyz() -> dict:
    """CRITICAL readiness vs DEGRADED integrations (FL-009).

    NOT READY (503, removed from service): Postgres, the Flink SQL Gateway / JobManager, the leader-elected background loops
    (deployment reconciler, metrics poller), or a quality consumer that is MISCONFIGURED (a rollout error, not an outage).

    DEGRADED (200, still serving; named in `degraded`): the secondary quality-result integration being unhealthy — Kafka not
    connected, no recent poll, UCS unreachable, its task crashed — and legacy UCS delivery mode (FL-007). An outage of that
    integration must not take job authoring, status and deployment out of service."""
    from app.services.quality_consumer import consumer_health, delivery_mode, legacy_backlog_count

    settings = get_settings()
    checks: dict[str, object] = {}
    ok = True
    degraded: list[str] = []

    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("readiness_db_failed", error=str(e)[:200])
        checks["db"] = "error"
        ok = False

    if settings.readiness_check_flink:
        t = settings.readiness_probe_timeout_seconds
        gw, jm = await asyncio.gather(
            _probe(f"{settings.flink_gateway_url.rstrip('/')}/v1/info", t),
            _probe(f"{settings.flink_rest_url.rstrip('/')}/v1/overview", t),
        )
        checks["flink_gateway"] = gw or "ok"
        checks["flink_jobmanager"] = jm or "ok"
        ok = ok and gw is None and jm is None

    if settings.background_tasks_enabled:
        tasks = {}
        for name in ("metrics_poller", "deployment_reconciler"):
            problem = _task_health(name)
            tasks[name] = problem or "ok"
            ok = ok and problem is None
        checks["background_tasks"] = tasks

    q_ok, q_detail = consumer_health(settings)
    q_detail = {k: v for k, v in q_detail.items() if k != "last_error"}       # raw client text stays in the logs
    misconfigured = q_detail.get("status") == "misconfigured"
    if settings.quality_consumer_enabled and settings.background_tasks_enabled:
        problem = _task_health("quality_consumer")
        if problem:
            q_ok, q_detail = False, {**q_detail, "task": problem}
    if settings.quality_consumer_enabled and settings.ucs_url and settings.ucs_internal_token:
        ucs_problem = await _probe(f"{settings.ucs_url.rstrip('/')}/health",
                                   settings.readiness_probe_timeout_seconds)
        q_detail = {**q_detail, "ucs_health": ucs_problem or "ok"}
        q_ok = q_ok and ucs_problem is None
    checks["quality_consumer"] = q_detail
    if misconfigured:
        ok = False                                   # a configuration error fails the rollout, as before
    elif not q_ok:
        degraded.append("quality_consumer")

    if settings.quality_consumer_enabled:
        mode = delivery_mode(settings)
        if mode["degraded"]:
            mode["undelivered_events"] = await legacy_backlog_count()
            degraded.append("ucs_quality_delivery")
        checks["ucs_quality_delivery"] = mode

    # Backwards-compatible summary field.
    if not settings.quality_consumer_enabled:
        checks["ucs_quality"] = "disabled"
    elif misconfigured:
        checks["ucs_quality"] = ("misconfigured: QUALITY_CONSUMER_ENABLED requires UCS_URL and "
                                 "UCS_INTERNAL_TOKEN")
    else:
        checks["ucs_quality"] = "enabled" if q_ok else "enabled_not_working"

    if not ok:
        return JSONResponse(status_code=503, content={"status": "not_ready", "degraded": degraded, **checks})
    return {"status": "degraded" if degraded else "ok", "degraded": degraded, **checks}


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {"service": "flinksql-runner", "version": "0.1.0",
            "org_id": get_settings().org_id, "docs": "/docs"}
