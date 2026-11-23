"""Process-wide logging: structlog + stdlib through one redacting pipeline.

Used by both the API (app.main) and the execution worker (app.worker), so
every process logs the same JSON shape with the same correlation fields
(request_id/trace_id/org_id/user_id from the request, execution_id/
workspace_id/... bound by the worker) and the same secret redaction (WS-016).

stdlib loggers (uvicorn, sqlalchemy, httpx, asyncpg, …) are routed through
the same processors via ProcessorFormatter — previously they went straight
to logging.basicConfig and bypassed redaction entirely. format_exc_info
renders tracebacks into the event *before* redaction, so a secret in an
exception message is scrubbed too.
"""
from __future__ import annotations

import logging

import structlog

from app.core.observability import redact_sensitive_fields


def configure_logging(level: str) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        redact_sensitive_fields,
    ]
    structlog.configure(
        processors=[*shared, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.add_logger_name, *shared],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(lvl)
    # uvicorn installs its own handlers; send its records through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
