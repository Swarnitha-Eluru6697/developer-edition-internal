"""Standardized audit/trace fields and secret-safe logging (WS-AUDIT-016).

Two independent, purely additive pieces — neither changes any existing
function's behavior, return value, or call sites:

1. `RequestContextMiddleware` binds request_id/trace_id (and, once a route's
   auth dependency resolves the caller, org_id/user_id — see
   app/core/deps.py) into structlog's contextvars for the lifetime of one
   request. `structlog.contextvars.merge_contextvars` is already the first
   processor in app/main.py's `_configure_logging` — it copies whatever is
   bound here into every log event emitted during that request, so every
   existing `log.info(...)`/`log.warning(...)` call in the codebase picks up
   these fields automatically, with zero changes to any of those call sites.

2. `redact_sensitive_fields` is a structlog processor that strips secret-
   shaped values out of the event dict before it's rendered — by field name
   (a denylist of keys that are secrets/credentials/code content regardless
   of shape) and by value pattern (the same defense-in-depth regex family
   used in app/services/codegen_governance.py, kept independent here since
   this module has a different lifecycle: it runs on literally every log
   call in the process, not just codegen's external-context decision).
   Wrapped so it can never raise — a bug in redaction must never be able to
   take down a log call, since log calls happen inside otherwise-unrelated
   business logic.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

log = structlog.get_logger()


# --------------------------------------------------------------------------
# 1. Request/trace correlation
# --------------------------------------------------------------------------
class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds request_id + trace_id for every request's log events.

    org_id/user_id are bound separately, in app/core/deps.py's
    get_current_user, since identity is only known once the JWT is
    validated — a dependency runs after middleware, not before it. Binding
    happens per-request-task and is cleared in `finally` so nothing leaks
    across requests sharing the same worker.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        trace_id = request.headers.get("x-trace-id") or _trace_id_from_traceparent(
            request.headers.get("traceparent")
        ) or request_id

        structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        # Echoed back so a caller (or an upstream gateway) can correlate its
        # own logs with this service's — the actual point of a request_id.
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        return response


def _trace_id_from_traceparent(traceparent: str | None) -> str | None:
    """Extract the trace-id segment from a W3C `traceparent` header
    (`00-<32 hex trace-id>-<16 hex span-id>-<flags>`), if present and
    well-formed. Returns None on anything else so the caller's own fallback
    (request_id) takes over — never raises."""
    if not traceparent:
        return None
    parts = traceparent.split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        return parts[1]
    return None


def bind_identity_contextvars(*, org_id: str | None, user_id: str | None) -> None:
    """Called once per request, right after JWT validation succeeds (see
    app/core/deps.py:get_current_user) — the earliest point identity is
    known. Additive only: never overwrites request_id/trace_id already
    bound by RequestContextMiddleware, and a failure here (e.g. no running
    contextvars context in a non-request caller) is swallowed rather than
    propagated, since identity logging is a courtesy, not a correctness
    dependency for whatever called this."""
    try:
        structlog.contextvars.bind_contextvars(org_id=org_id, user_id=user_id)
    except Exception:  # noqa: BLE001
        pass


_PATH_CONTEXT_KEYS = ("workspace_id", "execution_id", "session_id", "file_id")


async def bind_path_context(request: HTTPConnection) -> None:
    """App-wide dependency: bind the resource ids a route is addressed by
    (its path parameters) as log/audit context, so every log line and audit
    row in the request carries e.g. workspace_id without each call site
    passing it (WS-016). Cleared with the rest of the request's context by
    RequestContextMiddleware."""
    try:
        values = {k: str(request.path_params[k]) for k in _PATH_CONTEXT_KEYS if k in request.path_params}
        if values:
            structlog.contextvars.bind_contextvars(**values)
    except Exception:  # noqa: BLE001 — context is a courtesy, never fatal
        pass


# --------------------------------------------------------------------------
# 2. Secret-safe logging
# --------------------------------------------------------------------------
# Keys that are ALWAYS fully redacted, regardless of what they contain —
# these are secrets, credentials, or raw source/code content by name, and
# nothing about their value shape needs to match a pattern first. Compared
# case-insensitively.
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "id_token", "api_key", "apikey", "authorization", "auth_header",
    "dsn", "connection_string", "conn_str", "private_key", "client_secret",
    "vault_secret_path", "vault_path",
    # Source/code content — the WS-AI-011 "never send code externally by
    # default" concern applies just as much to logs.
    "code_text", "code_content", "current_file_content", "source_code",
    "content", "sql", "file_content",
})

# Defense in depth for a secret-shaped value that ends up under an
# innocuous-looking key. Same pattern family as
# app/services/codegen_governance.py, kept independent (see module
# docstring) — a false negative here is still caught by the key-name
# denylist above whenever the field is actually named like a secret.
_SECRET_VALUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]+?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@'\"]+:[^\s:/@'\"]+@"),  # credentialed URL
    # An alert channel's webhook URL IS the credential (anyone holding it can post into the channel). httpx logs the full URL of
    # every request at INFO, so these are scrubbed wherever they appear.
    re.compile(r"https?://hooks\.slack(?:-gov)?\.com/\S+"),
    re.compile(r"https?://[A-Za-z0-9.-]*(?:webhook\.office\.com|outlook\.office(?:365)?\.com/webhook|logic\.azure\.com|powerplatform\.com)\S*"),
]

_REDACTED = "[REDACTED]"


def redact_sensitive_fields(logger: Any, method_name: str, event_dict: dict) -> dict:
    """Structlog processor: strip secret-shaped values before rendering.

    Never raises — any failure here falls back to returning the event dict
    completely unredacted rather than blocking the log call, and logs a
    one-line warning about the failure so a broken pattern doesn't fail
    silently forever. A log pipeline must never be able to crash the
    business logic that's calling it.
    """
    try:
        for key, value in list(event_dict.items()):
            event_dict[key] = _redact_item(key, value, 0)
        return event_dict
    except Exception:  # noqa: BLE001
        log.warning("log_redaction_failed", exc_info=True)
        return event_dict


_MAX_DEPTH = 6


def _redact_item(key: Any, value: Any, depth: int) -> Any:
    """Redact one field, recursing into dicts/lists/tuples so a secret nested
    in e.g. metadata={"headers": {"authorization": ...}} is caught too."""
    if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
        return _REDACTED
    if isinstance(value, str):
        return _redact_value(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _redact_item(k, v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_item(None, v, depth + 1) for v in value)
    return value


def _redact_value(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            value = pattern.sub(_REDACTED, value)
    return value
