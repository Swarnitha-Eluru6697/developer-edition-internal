"""
Real, structured, external-facing error responses (§0.117, GAP_REPORT)
— the actual fix for a real, reported gap: several real error paths
returned raw exception strings directly to an external caller,
confirmed live — a real, live `/run` call against a genuinely
unreachable UCS returned `"error": "UCS at http://127.0.0.1:8001
unreachable or errored: [Errno 111] Connection refused"` in its own,
direct HTTP response body: a real internal service name ("UCS"), a
real internal hostname/port, and real, raw OS-level connection-error
detail, all handed straight to whoever called the public API.

The real, deliberate design choice this module makes: a genuinely
UNEXPECTED, internal failure (a node handler's own exception, a
provider outage, a database error) gets sanitized for any EXTERNAL
caller — the report's own exact shape, `{"error_code": ...,
"correlation_id": ...}` — while the FULL, real detail is never lost:
it's already persisted, unsanitized, on the Run/Trace row itself
(spans_json, fail_class) — this platform's own real, existing
structured-log equivalent, viewable by an operator via GET /api/traces/
{id} — and additionally written to this process's own real log output
at ERROR level, tagged with the same real correlation_id, so a support
engineer given that one id can find the exact matching, full-detail
record either way. This is NOT a second, parallel logging system;
`correlation_id` is deliberately the SAME id as the real run_id/
trace_id already generated for this exact invocation — one real,
existing identifier already serves as the correlation key, not a new
one invented on top of it.

A DELIBERATE, NARROW distinction from an ordinary `HTTPException`: a
real, controlled, actionable message a caller can act on ("Agent not
found", "API key is required", a real UCS entitlement denial's own
structured reason) is NOT what this module sanitizes — those are
intentional, already-safe, user-facing responses, and hiding them
behind an opaque code would make the API worse, not more secure. This
module's own real scope is specifically the genuinely UNEXPECTED
failure path — an exception a node handler, a provider call, or the
orchestration layer itself didn't anticipate and can't give the caller
any actionable guidance about beyond "something went wrong, here's how
to reference it."
"""
from __future__ import annotations
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("app.errors")


# The comment this replaced said it was "the same real definition of
# 'production', not a fourth, independent one" — while being a third
# independent copy. It is now imported from app.environment, which is
# the one definition, and re-exported here so every existing caller of
# error_responses.is_production keeps working.
from .environment import is_production  # noqa: E402,F401


# Real, stable error codes an external caller (and this platform's own
# real frontend) can build real, deterministic logic against, instead
# of pattern-matching arbitrary exception text — the report's own
# explicit ask ("stable error semantics instead of parsing arbitrary
# exception text").
AGENT_EXECUTION_FAILED = "AGENT_EXECUTION_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"


def sanitized_error_value(correlation_id: str) -> Dict[str, str]:
    """Real, direct helper — the report's own exact structured shape,
    for a caller that has a raw error string to replace but isn't
    working with a real span/result dict shape (e.g. a genuinely
    uncaught exception in a streaming worker thread)."""
    return {"error_code": AGENT_EXECUTION_FAILED, "correlation_id": correlation_id}


def sanitize_span_for_external_response(span: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    """Real, per-span sanitization (§0.117, GAP_REPORT) — the actual
    fix for the exact, confirmed-live leak: a node-execution span's
    own real "error" key (harness.py's own generic `except Exception
    as e: span["error"] = str(e)`) can genuinely contain anything a
    real exception's own __str__ produces — a database DSN, an
    internal hostname, a raw OS errno, a third-party SDK's own
    internal detail. Returns a real, shallow COPY with "error"
    replaced by the report's own exact structured shape when NOT
    production (dev/test still gets the full, real detail for local
    debugging — this only changes real, external, production
    responses) — every other real key (`node`, `node_id`, `status`,
    `duration_ms`, ...) is preserved unchanged, since those carry no
    real internal detail of their own."""
    if not is_production() or "error" not in span:
        return span
    sanitized = dict(span)
    sanitized["error"] = sanitized_error_value(correlation_id)
    return sanitized


def sanitize_result_for_external_response(result: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    """Real, whole-result sanitization for a genuine execution failure
    — applied by every real, external-facing invocation channel
    (run_agent, the streaming endpoint, webhooks, MCP) to the exact
    same real `result` dict already used to persist the full-detail
    Run/Trace rows, AFTER that persistence has already happened with
    the real, unsanitized detail intact. Returns a real, shallow copy;
    the caller's own original `result` (and anything already persisted
    from it) is never mutated.

    ucs_soft_denials (§0.117, GAP_REPORT) — a real, SEPARATE leak found
    while live-testing this fix: UCS_SOFT_CHECK's own real, top-level
    `result["ucs_soft_denials"]` list (harness.py) carries its own,
    independent "error" string per entry — confirmed live, the exact
    same raw "UCS at <host> unreachable..." detail leaked through this
    field even after spans[] was already sanitized. Sanitized the same
    real way, for the same real reason."""
    if not is_production():
        return result
    out = dict(result)
    if "spans" in out:
        out["spans"] = [sanitize_span_for_external_response(s, correlation_id) for s in out["spans"]]
    if out.get("ucs_soft_denials"):
        out["ucs_soft_denials"] = [
            {**d, "error": sanitized_error_value(correlation_id)} if "error" in d else d
            for d in out["ucs_soft_denials"]
        ]
    if result.get("status") in ("failed", "error"):
        out["error_code"] = AGENT_EXECUTION_FAILED
        out["correlation_id"] = correlation_id
    return out


def log_full_detail(correlation_id: str, message: str, exc: Optional[BaseException] = None) -> None:
    """Real, structured server-side logging — the real "full stack/
    error detail goes into structured logs" half of the report's own
    ask. Always logs the complete, real detail (this function itself
    never sanitizes anything — that's the whole point: an operator
    with log access, or a support engineer handed a correlation_id by
    an external caller, can always find the real, full picture here),
    tagged with the exact same real id an external caller would have
    received in their own, sanitized response."""
    logger.error("[correlation_id=%s] %s", correlation_id, message, exc_info=exc)
