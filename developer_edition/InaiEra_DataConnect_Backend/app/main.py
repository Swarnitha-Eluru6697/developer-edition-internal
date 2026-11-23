"""
InaiEra DataConnect — FastAPI application factory.

Improvements over original:
- Structured JSON logging via structlog
- Lifespan context manager (replaces deprecated on_event)
- A /metrics endpoint with cardinality-restricted custom metrics (see
  app/core/metrics.py), plus optional HTTP request metrics via
  prometheus-fastapi-instrumentator when that prod-extra is installed
- Request-ID injection for distributed tracing
- Global exception handlers for consistent error envelopes
"""

import asyncio
import re
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

import structlog
from asyncpg.exceptions import ForeignKeyViolationError, UniqueViolationError
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router, public_router
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import settings
from app.core.errors import DataConnectError
from app.core.gateway import install_gateway
from app.core.logging import configure_logging
from app.core.metrics import render_metrics
from app.db.session import engine
from app.schemas.errors import ErrorEnvelope, FieldError

logger = structlog.get_logger(__name__)

# HTTP status -> stable error `code` for a plain HTTPException, which carries
# no code of its own (just a status and a free-text `detail`) — this is only
# the fallback path; anything raising a DataConnectError subclass instead
# (see app/core/errors.py) already carries its own specific code.
_STATUS_CODE_NAMES: dict[int, str] = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 413: "payload_too_large",
    422: "validation_error", 429: "rate_limited",
    # 503/504 match BackendUnavailableError/RequestTimeoutError's own codes
    # (app/core/errors.py) — the same failure can reach a caller via either
    # a raised DataConnectError subclass or a bare HTTPException with this
    # status, and FE-BUG-04's frontend mapping keys on the code string, so
    # both paths must agree on it.
    502: "bad_gateway", 503: "DATABASE_UNAVAILABLE", 504: "OPERATION_TIMEOUT",
}
_RETRYABLE_STATUSES = {429, 502, 503, 504}

# The only UniqueViolations specific enough to explain in pipeline terms —
# see the UniqueConstraints on dc_pipelines in app/models/pipeline.py. Any
# other constraint (on this table or any other) gets a generic, honest
# message instead of this one misapplied to it.
_PIPELINE_IDENTITY_CONSTRAINTS = {
    "dc_pipelines_org_id_topic_name_key",
    "dc_pipelines_org_id_connector_name_key",
    # Since 2026-11-06 the names carry no organization, so they are also unique across organizations (alembic 0057).
    "uq_dc_pipelines_topic_name",
    "uq_dc_pipelines_connector_name",
}
# NEW-08: two sinks naming one connector dead-letter queue. PipelineService turns this into a validation error itself; this is the backstop for
# any other writer that reaches the database.
_DEAD_LETTER_TOPIC_INDEX = "uq_dc_pipelines_dead_letter_topic"


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    field_errors: list[FieldError] | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    body = ErrorEnvelope(
        code=code, message=message, field_errors=field_errors,
        request_id=_request_id(request), retryable=retryable, details=details,
    )
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body.model_dump()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    configure_logging()

    from app.core.gateway import assert_explicit_environment

    assert_explicit_environment()  # BUG-04: never run the development principal by accident, or outside development

    logger.info(
        "dataconnect.startup",
        version=settings.app_version,
        env=settings.env,
    )

    # Load the connector-template catalog from PostgreSQL (seeds it if empty).
    # There is no in-memory fallback (see app/db/seed_templates.py) — outside
    # development a failure here must fail startup, not just get logged while
    # the API starts anyway looking healthy with no working control database.
    # In development we still let the API start (for a quick local checkout
    # without Postgres running), but the catalog stays empty until it's up.
    try:
        from app.db.seed_templates import init_template_catalog
        await init_template_catalog()
    except Exception:
        logger.exception("template_catalog.init_failed")
        if settings.env != "development":
            raise

    # Operation claim/lease worker (app/services/operation_runner.py): picks
    # up any queued Operation — including one left "running" by a process
    # that has since died, whose lease has lapsed — and runs it. Started
    # unconditionally (not gated by runtime_deploy_enabled the way the
    # reconciler below is): pipeline_service.deploy/pause/resume/restart
    # already no-op cleanly when Kafka Connect isn't configured, and gating
    # the worker itself here would instead leave those Operations queued
    # forever in that mode.
    from app.services import operation_runner

    await operation_runner.start_worker()

    # QualityExecutionJob claim/lease worker (app/services/quality_job_runner.py): runs queued
    # quality jobs and reclaims one left `running` by a process that has since died (its lease
    # lapsed). Replaces the earlier blind "resume everything queued/running at startup", which
    # would have re-run a job another live replica was still executing.
    try:
        from app.services.quality_job_runner import start_cleanup_task
        from app.services.quality_job_runner import start_worker as start_quality_job_worker

        await start_quality_job_worker()
        await start_cleanup_task()
    except Exception:
        logger.exception("quality_execution_jobs.worker_start_failed")

    # GAP-10: prune old discovery runs and their objects.
    from app.services.discovery_service import start_cleanup_task as start_discovery_cleanup
    from app.services.discovery_service import stop_cleanup_task as stop_discovery_cleanup

    await start_discovery_cleanup()

    # Runtime controller: continuously reconciles the durable pipeline spec
    # against Kafka Connect's actual state (see app/services/runtime_reconciler.py).
    reconciler_task = None
    quality_stream_task = None
    dlq_counter_task = None
    if settings.runtime_deploy_enabled:
        from app.services.runtime_reconciler import runtime_reconciler

        reconciler_task = asyncio.create_task(runtime_reconciler.run_forever())

        # Real-time quality enforcement: continuously reconciles which
        # running pipelines have a quality policy attached against a live
        # Kafka consumer per pipeline that filters its data — see
        # app/services/quality_stream_worker.py. Gated the same way as the
        # reconciler above since both need a real Kafka/Connect stack to do
        # anything meaningful.
        from app.services.quality_stream_worker import quality_stream_manager

        quality_stream_task = asyncio.create_task(quality_stream_manager.run_forever())

        # FE-BUG-17: counts every connector-DLQ record by cause (exact, bucketed
        # per minute) for the Quality screen — see services/dlq_failure_counter.py.
        from app.services.dlq_failure_counter import dlq_counter_manager

        dlq_counter_task = asyncio.create_task(dlq_counter_manager.run_forever())

    # Alerts (app/services/alerts/): a sweep every minute evaluates every organization's alert rules over its pipelines and posts new alerts
    # to its Slack / Teams channels. Reads DataConnect's own database only, so it does not need Kafka Connect to be configured; off when
    # settings.alerts_sweep_enabled is (tests).
    from app.services.alerts.sweeper import alert_sweeper

    await alert_sweeper.start()

    yield

    # Graceful shutdown (app/core/shutdown.py): every step runs in this order, each isolated (its failure is logged, the next still runs)
    # and bounded (abandoned after settings.shutdown_step_timeout_seconds). The ORDER is the dependency order: stop the things that USE a
    # client (workers, reconciler, consumers) before closing the client (Kafka producer, HTTP pools), and the database last of all.
    from app.core.shutdown import run_shutdown
    from app.services.quality_job_runner import stop_cleanup_task
    from app.services.quality_job_runner import stop_worker as stop_quality_job_worker

    async def _cancel(task: asyncio.Task | None) -> None:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _stop_dlq_counters() -> None:
        from app.services.dlq_failure_counter import dlq_counter_manager

        await _cancel(dlq_counter_task)
        await dlq_counter_manager.stop_all()

    async def _stop_quality_streams() -> None:
        from app.services.quality_stream_worker import quality_stream_manager

        await _cancel(quality_stream_task)
        await quality_stream_manager.stop_all()

    async def _close_kafka_producer() -> None:
        from app.services.kafka_runtime import close_kafka_producer

        await close_kafka_producer()

    async def _close_http_clients() -> None:
        from app.core.http_clients import close_all as close_http_clients

        await close_http_clients()

    await run_shutdown(
        [
            ("operation_worker", operation_runner.stop_worker),
            ("quality_job_worker", stop_quality_job_worker),
            ("quality_job_cleanup", stop_cleanup_task),
            ("discovery_cleanup", stop_discovery_cleanup),
            ("alerts_sweeper", alert_sweeper.stop),
            ("runtime_reconciler", lambda: _cancel(reconciler_task)),
            ("dlq_counters", _stop_dlq_counters),
            ("quality_streams", _stop_quality_streams),
            ("kafka_producer", _close_kafka_producer),
            ("http_clients", _close_http_clients),
            ("database_engine", engine.dispose),
        ],
        timeout=settings.shutdown_step_timeout_seconds,
    )

    logger.info("dataconnect.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="InaiEra DataConnect API",
        version=settings.app_version,
        description=(
            "Tenant-aware data ingestion and pipeline management platform "
            "backed by Confluent Kafka Connect."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "production" else None,
        redoc_url="/redoc" if settings.env != "production" else None,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    # Gate 1 (app/core/gateway.py): a gateway-verified principal on every route except the health probes (401
    # otherwise); per-route permissions then decide (403). Installed first so it is the INNERMOST middleware:
    # CORS (below) still answers preflights and adds its headers to a 401.
    install_gateway(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # GAP-13: byte limits on bulk-payload routes, enforced before the body is parsed (app/core/body_limit.py).
    app.add_middleware(
        BodySizeLimitMiddleware,
        rules=[
            (re.compile(r"/quality-policies/[^/]+/execute-jobs$"), lambda: settings.quality_job_max_request_bytes),
            (re.compile(r"/quality-policies/[^/]+/preview$"), lambda: settings.quality_preview_max_request_bytes),
        ],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id", str(uuid4()))
        # On request.state (not just bound into structlog's contextvars, the
        # prior state) so exception handlers — which receive `request`, not
        # log context — can echo it back in the error envelope's request_id.
        request.state.request_id = request_id

        with structlog.contextvars.bound_contextvars(
                request_id=request_id
        ):
            response = await call_next(request)

        response.headers["X-Request-Id"] = request_id
        return response

    # ── Global exception handlers ─────────────────────────────────────────────
    # Every one of these renders the same ErrorEnvelope shape (app/schemas/errors.py)
    # regardless of what was actually raised — see app/core/errors.py's module
    # docstring for why a DataConnectError hierarchy coexists with plain
    # HTTPException/ValueError instead of replacing them outright.
    @app.exception_handler(DataConnectError)
    async def data_connect_error_handler(request: Request, exc: DataConnectError):
        logger.warning(
            "dataconnect_error", code=exc.code, path=str(request.url.path), detail=exc.message,
        )
        return _envelope(
            request, status_code=exc.status_code, code=exc.code, message=exc.message,
            field_errors=exc.field_errors, details=exc.details, retryable=exc.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        field_errors = [
            FieldError(
                field=".".join(str(loc) for loc in e["loc"] if loc != "body") or "body",
                message=e["msg"],
            )
            for e in exc.errors()
        ]
        return _envelope(
            request, status_code=422, code="validation_error",
            message="Request validation failed.", field_errors=field_errors,
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        # The existing, pervasive pattern across services for "this input is
        # invalid" (see e.g. app/services/schema_service.py, transform_service.py)
        # — kept as a ValueError -> 422 mapping for backward compatibility
        # rather than requiring every such raise site to switch to
        # app.core.errors.ValidationError; both now render the identical envelope.
        logger.warning("validation_error", detail=str(exc))
        return _envelope(request, status_code=422, code="validation_error", message=str(exc))

    from app.core.network_policy import RateLimitExceededError

    @app.exception_handler(RateLimitExceededError)
    async def rate_limit_handler(request: Request, exc: RateLimitExceededError):
        # Distinct from a generic failure: the caller is being throttled,
        # not told their source is unreachable — see app/core/network_policy.py.
        logger.warning("outbound_probe.rate_limited", path=str(request.url.path), detail=str(exc))
        return _envelope(
            request, status_code=429, code="rate_limited", message=str(exc), retryable=True,
        )

    @app.exception_handler(OperationalError)
    async def database_unavailable_handler(request: Request, exc: OperationalError):
        # OperationalError specifically (connection refused, server closed the
        # connection, ...) — not the broader DBAPIError, which also covers
        # IntegrityError (a real constraint conflict, not an outage) and would
        # misreport a genuine 409-shaped problem as a retryable 503. This is
        # about DataConnect's own control-plane database being unreachable,
        # not a customer source system (SourceConnectivityError) or Kafka
        # Connect (KafkaConnectError).
        logger.exception("database_unavailable", path=str(request.url.path))
        return _envelope(
            request, status_code=503, code="DATABASE_UNAVAILABLE",
            message="The backend database is temporarily unavailable. Please try again shortly.",
            retryable=True,
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        # A real constraint conflict (unique or foreign-key), not a database
        # outage — see the OperationalError handler above for that
        # distinction. Without a handler at all this falls through to the
        # generic Exception handler below, which Starlette wires up as
        # ServerErrorMiddleware's handler rather than a normal
        # ExceptionMiddleware one — that middleware sits *outside*
        # CORSMiddleware, so its response reaches the browser with no
        # Access-Control-Allow-Origin header and gets reported as a network
        # error rather than the actual 409.
        #
        # SQLAlchemy's asyncpg dialect wraps the real driver error as a bare
        # string (see AsyncAdapt_asyncpg_connection._handle_exception) and
        # re-raises it `from` the original — so exc.orig is that string-only
        # wrapper, and exc.orig.__cause__ is the actual asyncpg exception
        # carrying constraint_name/table_name. Every branch below reads from
        # that, never from exc.orig directly.
        cause = getattr(exc.orig, "__cause__", None)
        constraint = getattr(cause, "constraint_name", None)
        table = getattr(cause, "table_name", None)
        logger.warning(
            "integrity_error", path=str(request.url.path), constraint=constraint, table=table,
            detail=str(cause or exc.orig or exc),
        )

        if isinstance(cause, UniqueViolationError) and constraint in _PIPELINE_IDENTITY_CONSTRAINTS:
            # Two pipelines resolving to the same org_id/topic_name or
            # org_id/connector_name — see the UniqueConstraints on
            # dc_pipelines in app/models/pipeline.py. This is the only case
            # specific enough to explain in pipeline terms; every other
            # constraint below gets an honest, generic message instead of
            # this one misapplied to an unrelated table (that used to
            # happen here — e.g. a foreign-key violation on dc_audit_logs
            # from a missing organization row was previously reported as
            # "a pipeline with this identity already exists").
            return _envelope(
                request, status_code=409, code="conflict",
                message=(
                    "A pipeline with this zone/domain/source system/entity/mode/version "
                    "already exists (its topic or connector name would collide). Change "
                    "one of those fields or the version to create a distinct pipeline."
                ),
                retryable=False,
            )
        if isinstance(cause, UniqueViolationError) and constraint == _DEAD_LETTER_TOPIC_INDEX:
            return _envelope(
                request, status_code=409, code="conflict",
                message=(
                    "Another sink pipeline already uses this dead-letter queue (same domain, source system, entity and version). "
                    "Change the entity or version, or choose a policy without a dead-letter queue."
                ),
                field_errors=[FieldError(field="entity", message="Another sink pipeline already uses the dead-letter queue for this name.")],
                retryable=False,
            )
        if isinstance(cause, ForeignKeyViolationError):
            # `table` is the table being written to (the referencing side),
            # not the one the missing row would have been in — naming the
            # constraint instead of asserting a direction keeps this honest
            # without needing a pg_constraint lookup to resolve the other side.
            return _envelope(
                request, status_code=409, code="conflict",
                message=(
                    f"This request references data that doesn't exist (foreign key "
                    f"'{constraint}' on '{table}')." if constraint
                    else "This request references data that doesn't exist."
                ),
                retryable=False,
            )
        return _envelope(
            request, status_code=409, code="conflict",
            message=(
                f"This request violates a database constraint ('{constraint}')."
                if constraint else "This request violates a database constraint."
            ),
            retryable=False,
        )

    @app.exception_handler(TimeoutError)
    @app.exception_handler(SQLAlchemyTimeoutError)
    async def timeout_handler(request: Request, exc: Exception):
        # Python's TimeoutError also covers asyncio.TimeoutError (an alias of
        # it since 3.11) for an outbound call this request depended on (a
        # source probe, Kafka Connect, ...) exceeding its deadline;
        # sqlalchemy.exc.TimeoutError (a connection-pool checkout timing out)
        # is its own unrelated class needing its own registration — it does
        # not inherit from the builtin.
        logger.warning("request_timeout", path=str(request.url.path))
        return _envelope(
            request, status_code=504, code="OPERATION_TIMEOUT",
            message="The request took too long to complete. Please try again.", retryable=True,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # The fallback for the many routes that still `raise HTTPException(...)`
        # directly with a plain string detail — inferring `code` from the
        # status keeps those on the same envelope without every call site
        # needing to switch to a DataConnectError subclass.
        message = exc.detail if isinstance(exc.detail, str) else jsonable_encoder(exc.detail, custom_encoder={Exception: str})
        return _envelope(
            request, status_code=exc.status_code,
            code=_STATUS_CODE_NAMES.get(exc.status_code, "http_error"),
            message=message if isinstance(message, str) else str(message),
            retryable=exc.status_code in _RETRYABLE_STATUSES,
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        logger.exception(
            "unhandled_error",
            path=str(request.url.path),
            method=request.method,
        )
        return _envelope(
            request, status_code=500, code="internal_error",
            message="Internal server error", retryable=True,
        )

    # ── Metrics ───────────────────────────────────────────────────────────────
    # prometheus-fastapi-instrumentator is an optional (prod-extra) dependency
    # that adds HTTP request metrics — method/handler/status only, all
    # already bounded/low-cardinality by default: "handler" is FastAPI's
    # matched ROUTE TEMPLATE (e.g. "/pipelines/{pipeline_id}"), never the raw
    # request path, so a path parameter never becomes a label value. Skipped
    # gracefully wherever it isn't installed (dev/test) — see
    # app/core/metrics.py for the custom, cardinality-restricted metrics that
    # are always available regardless.
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        pass
    else:
        Instrumentator().instrument(app)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        return Response(content=render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")

    # ── Routers ───────────────────────────────────────────────────────────────
    # Every API route sits behind Gate 1 (install_gateway above) and declares its own permission.
    app.include_router(api_router, prefix=settings.api_prefix)
    # Health probes carry no identity (the Docker/orchestrator health check sends no headers): gateway public paths.
    app.include_router(public_router, prefix=settings.api_prefix)

    return app


app = create_app()

