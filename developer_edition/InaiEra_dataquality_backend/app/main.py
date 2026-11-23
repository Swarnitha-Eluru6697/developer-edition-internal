"""FastAPI entry point for inaiera-data-quality.

Registers, manages, and renders cross-stack data quality rules.
Decoupled from UCS (which scores and alerts) and from the job runners
(which execute the rendered artifacts).

Run locally:
    uvicorn app.main:app --host 0.0.0.0 --port 8086 --reload
"""
from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text

from app.api import rules as rules_api
from app.api import rulesets as rulesets_api
from app.api import bindings as bindings_api
from app.api import render as render_api
from app.api import sync as sync_api
from app.api import capabilities as capabilities_api
from app.core.auth import jwks_cache
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal, engine

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)
    log.info("dataquality.starting", env=settings.environment, org_id=settings.org_id)

    # DQ-CFG-001: an enabled-but-misconfigured integration fails STARTUP
    # (the pod crash-loops with a clear reason) instead of serving traffic.
    problems = settings.integration_problems()
    if problems:
        for p in problems:
            log.error("dataquality.misconfigured", problem=p)
        raise RuntimeError("DQ service misconfigured: " + "; ".join(problems))

    # Prime JWKS cache (fail-soft — tokens validated on first request if missing)
    try:
        await jwks_cache.refresh()
        log.info("dataquality.jwks_primed")
    except Exception as e:
        log.warning("dataquality.jwks_prime_failed", error=str(e))

    # Check DB
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        log.info("dataquality.db_ok")
    except Exception as e:
        log.error("dataquality.db_unreachable", error=str(e))

    # Seed system rules for this org
    if settings.seed_system_rules:
        try:
            from app.services.rule_service import RuleService
            from app.db.session import AsyncSessionLocal as Sess
            async with Sess() as db:
                svc = RuleService(db)
                created = await svc.seed_system_rules(settings.org_id)
                await db.commit()
            if created:
                log.info("dataquality.system_rules_seeded", count=created)
        except Exception as e:
            log.warning("dataquality.seed_failed", error=str(e))

    worker = None
    if settings.outbox_worker_enabled:
        from app.services.outbox import outbox_worker
        worker = asyncio.create_task(outbox_worker())

    yield

    log.info("dataquality.shutting_down")
    if worker:
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await engine.dispose()


app = FastAPI(
    title="InAiEra Data Quality Service",
    description=(
        "Manages cross-stack data quality rule definitions, rulesets, and job bindings. "
        "Renders rules into native dbt test blocks, FlinkSQL quality jobs, and Python GX/Soda "
        "artifacts. Results flow through UCS (POST /quality/results) for scoring and alerting. "
        "Decoupled from UCS and the job runners — this service only owns rule definitions "
        "and rendering."
    ),
    version="0.2.0",
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
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR",
                            "message": "Request validation failed",
                            "details": exc.errors()}},
    )


@app.exception_handler(Exception)
async def global_handler(_: Request, exc: Exception) -> JSONResponse:
    from fastapi import HTTPException
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    log.exception("unhandled_error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": str(exc)[:200]}},
    )


app.include_router(rules_api.router)
app.include_router(rulesets_api.router)
app.include_router(bindings_api.router)
app.include_router(render_api.router)
app.include_router(sync_api.router)
app.include_router(capabilities_api.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok", "service": "data-quality"}


@app.get("/readyz", tags=["meta"])
async def readyz():
    """503 while any enabled integration is misconfigured (DQ-CFG-001), the
    DB is unreachable, or the inbound JWKS has not been loaded."""
    settings = get_settings()
    problems = settings.integration_problems()
    if problems:
        return JSONResponse(status_code=503, content={"status": "not_ready",
                                                      "error": "misconfigured",
                                                      "problems": problems})
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": "unreachable",
                                                      "error": str(e)[:200]})
    if not jwks_cache.is_primed():
        try:
            await jwks_cache.refresh()
        except Exception:  # noqa: BLE001
            pass
    if not jwks_cache.is_primed():
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": "ok",
                                                      "jwks": False})
    return {"status": "ok", "db": "ok", "jwks": True,
            "integrations": {"ucs": settings.ucs_enabled, "dbtrunner": settings.dbtrunner_enabled,
                             "flinksql": settings.flinksql_enabled}}


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {"service": "data-quality", "version": "0.2.0",
            "org_id": _settings.org_id, "docs": "/docs"}
