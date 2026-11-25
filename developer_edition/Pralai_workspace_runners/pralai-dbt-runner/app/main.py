"""FastAPI entry point for dbt Runner (data plane).

Registers, validates, schedules, and runs dbt projects against the
customer's lakehouse (Trino+Polaris or pg_lake+Polaris). Intentionally
decoupled from Workspace (TDD 4.3.3) — any caller with a valid platform
JWT can register and run a project.

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 8082 --reload
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api import projects as projects_api
from app.core import log_scrub, metrics
from app.core.auth import jwks_cache
from app.core.config import get_settings
from app.core.errors import conflict_for_unique_violation
from app.db import session as db_session
from app.services.leader import run_as_leader
from app.services.metadata_outbox import outbox_loop
from app.services.run_reconciler import reconciler_loop
from app.services.scheduler import scheduler_loop

log = structlog.get_logger()


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO),
        ),
    )
    log_scrub.install()          # DG-011: no repository credential reaches a log line, whichever call site logged it


_leader_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _leader_task
    settings = get_settings()
    _configure_logging(settings.log_level)
    log.info("dbtrunner.starting", env=settings.environment, org_id=settings.org_id,
             runner_mode=settings.runner_mode)
    if settings.runner_mode == "subprocess" and settings.environment != "dev":
        log.error("dbtrunner.subprocess_mode_outside_dev",
                  note="runs will be refused; set RUNNER_MODE=k8s_job")

    try:
        await jwks_cache.refresh()
        log.info("dbtrunner.jwks_primed")
    except Exception as e:
        log.warning("dbtrunner.jwks_prime_failed", error=str(e))

    try:
        async with db_session.AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        log.info("dbtrunner.db_ok")
    except Exception as e:
        log.error("dbtrunner.db_unreachable", error=str(e))

    # Scheduler, run reconciler (RUN-DBT-003) and metadata outbox
    # (RUN-DBT-008) run on exactly ONE process across all replicas and
    # uvicorn workers — whichever holds the leader advisory lock. The
    # reconciler's first sweep runs immediately on acquiring leadership, so
    # runs stranded by a dead pod are picked up right away.
    loops = [reconciler_loop, outbox_loop]
    if settings.scheduler_enabled:
        loops.insert(0, scheduler_loop)
    else:
        log.info("dbtrunner.scheduler_disabled", reason="SCHEDULER_ENABLED=false (Dagster owns scheduling)")
    _leader_task = asyncio.create_task(run_as_leader(loops))

    yield

    log.info("dbtrunner.shutting_down")
    if _leader_task:
        _leader_task.cancel()
        await asyncio.gather(_leader_task, return_exceptions=True)
    await db_session.engine.dispose()


app = FastAPI(
    title="InAiEra dbt Runner",
    description=(
        "Registers, validates, schedules, and runs dbt projects against the "
        "customer Data Lake House (Trino+Polaris or pg_lake+Polaris). "
        "Deployed inside the data plane. Decoupled from Workspace — any "
        "caller with a valid platform JWT can use this API directly."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Internal-Service-Token"],
)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(_: Request, exc: IntegrityError) -> JSONResponse:
    """X-008: a UNIQUE violation that no route translated is still the standard 409 (stable code, resource, field) — never a raw database
    error. Anything else (a foreign key, a check) is a real bug: re-raised, a 500."""
    standard = conflict_for_unique_violation(exc)
    if standard is None:
        raise exc
    return JSONResponse(status_code=409, content={"detail": standard.detail})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # The submitted value (`input`) is NOT echoed back: a rejected field may be a secret the caller pasted by mistake, such as a
    # git URL with a token in it (DBT-009).
    details = [{k: v for k, v in err.items() if k != "input"}
               for err in jsonable_encoder(exc.errors(), custom_encoder={Exception: str})]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "message": "Request validation failed", "details": details}},
    )


app.include_router(projects_api.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok", "service": "dbtrunner"}


_JWKS_RETRY_SECONDS = 30.0
_K8S_PROBE_TTL_SECONDS = 30.0
_readiness_cache: dict = {"jwks_retry_at": 0.0, "k8s_checked_at": 0.0, "k8s_ok": None}


async def _jwks_ready() -> bool:
    if jwks_cache.is_primed():
        return True
    now = time.monotonic()
    if now >= _readiness_cache["jwks_retry_at"]:
        _readiness_cache["jwks_retry_at"] = now + _JWKS_RETRY_SECONDS
        try:
            await jwks_cache.refresh()
        except Exception:  # noqa: BLE001
            pass
    return jwks_cache.is_primed()


async def _k8s_ready() -> bool:
    from app.executors.isolation import get_runner_isolation

    now = time.monotonic()
    if (_readiness_cache["k8s_ok"] is None
            or now - _readiness_cache["k8s_checked_at"] > _K8S_PROBE_TTL_SECONDS):
        _readiness_cache["k8s_ok"] = await get_runner_isolation().k8s_reachable()
        _readiness_cache["k8s_checked_at"] = now
    return bool(_readiness_cache["k8s_ok"])


@app.get("/readyz", tags=["meta"])
async def readyz():
    """503 unless every dependency this pod needs to serve is usable: the
    metadata DB, the JWKS used to authenticate every user request, and (in
    k8s_job mode) the Kubernetes API runs are submitted through. Outbox
    backlog / dead letters are reported as "degraded" (still 200 — they
    need attention but do not stop this pod from serving)."""
    from app.models import MetadataOutboxEvent, Run

    settings = get_settings()
    checks: dict = {}
    try:
        async with db_session.AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
            checks["db"] = "ok"
            try:
                rows = (await session.execute(
                    select(MetadataOutboxEvent.status, func.count())
                    .where(MetadataOutboxEvent.status.in_(("pending", "dead_letter")))
                    .group_by(MetadataOutboxEvent.status)
                )).all()
                counts = {status: n for status, n in rows}
            except Exception:  # noqa: BLE001 — schema not migrated yet etc.
                counts = None
            cancelling = None
            try:
                stuck_before = func.now() - timedelta(seconds=settings.run_cancel_stuck_seconds)
                n, stuck = (await session.execute(
                    select(func.count(), func.count().filter(Run.cancel_requested_at < stuck_before))
                    .where(Run.status == "cancelling")
                )).one()
                cancelling = {"cancelling": n, "stuck": stuck}
            except Exception:  # schema not migrated yet etc.
                log.warning("readyz_cancellation_count_failed", exc_info=True)
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": "error",
                                                      "error": str(e)})

    ready = True
    checks["jwks"] = "ok" if await _jwks_ready() else "not_primed"
    ready &= checks["jwks"] == "ok"
    if settings.runner_mode == "k8s_job":
        checks["k8s_api"] = "ok" if await _k8s_ready() else "unreachable"
        ready &= checks["k8s_api"] == "ok"

    degraded = []
    if counts is not None:
        checks["outbox"] = {"pending": counts.get("pending", 0),
                            "dead_letter": counts.get("dead_letter", 0)}
        if counts.get("dead_letter", 0) > 0:
            degraded.append("outbox_dead_letter")
        if counts.get("pending", 0) > settings.metadata_outbox_backlog_warn:
            degraded.append("outbox_backlog")
    if cancelling is not None:
        checks["cancellation"] = cancelling
        if cancelling["stuck"] > 0:
            degraded.append("run_cancellation_stuck")      # DBT-005: a Job may still be running
    body = {"status": "not_ready" if not ready else ("degraded" if degraded else "ok"),
            "checks": checks, "degraded": degraded, "metrics": metrics.snapshot()}
    if not ready:
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {"service": "dbtrunner", "version": "0.1.0", "org_id": _settings.org_id, "docs": "/docs"}
