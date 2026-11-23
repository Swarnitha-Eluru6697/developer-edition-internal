"""Tests for WS-AUDIT-016: standardized audit/trace fields + secret-safe logging.

Three things are verified, each independent of the others:
  1. redact_sensitive_fields — the structlog processor — never lets a
     secret-shaped value or a sensitive-named field through, and never raises
     (a broken pattern must degrade to "log the event anyway", not crash).
  2. RequestContextMiddleware — a real HTTP round-trip via TestClient proves
     request_id/trace_id are generated, echoed back as response headers, and
     that a caller-supplied X-Request-Id is honored rather than overwritten.
  3. AuditService.record — the persisted metadata carries request_id/
     trace_id/org_id automatically, and the two new optional args
     (workspace_id, run_id) work without touching any of the ~30 existing
     call sites that don't pass them.
"""
from __future__ import annotations

import uuid

import structlog

from app.core.observability import _trace_id_from_traceparent, redact_sensitive_fields
from app.models import AuditLog
from app.services.audit_service import AuditService


# --------------------------------------------------------------------------
# 1. Secret-safe logging
# --------------------------------------------------------------------------
def test_sensitive_key_is_fully_redacted_regardless_of_value():
    event = redact_sensitive_fields(None, "info", {
        "event": "db_connected",
        "password": "hunter2",
        "token": "abc.def.ghi",
    })
    assert event["password"] == "[REDACTED]"
    assert event["token"] == "[REDACTED]"
    assert event["event"] == "db_connected"  # untouched, not a sensitive key


def test_code_content_key_is_redacted_even_though_it_looks_harmless():
    # WS-AI-011's concern applies to logs too: source code must never be logged.
    event = redact_sensitive_fields(None, "info", {
        "current_file_content": "SELECT * FROM customers WHERE ssn = '123-45-6789'",
    })
    assert event["current_file_content"] == "[REDACTED]"


def test_secret_shaped_value_is_redacted_even_under_an_innocuous_key():
    event = redact_sensitive_fields(None, "info", {
        "note": "retry with AKIAABCDEFGHIJKLMNOP as the key",
    })
    assert "AKIAABCDEFGHIJKLMNOP" not in event["note"]
    assert "[REDACTED]" in event["note"]


def test_jwt_shaped_value_is_redacted():
    fake_jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dGhpc2lzYXNpZ25hdHVyZQ"
    event = redact_sensitive_fields(None, "info", {"detail": f"failed with token {fake_jwt}"})
    assert fake_jwt not in event["detail"]


def test_credentialed_connection_string_is_redacted():
    event = redact_sensitive_fields(None, "info", {
        "detail": "connect failed: postgresql://admin:s3cr3t@db.internal:5432/prod",
    })
    assert "s3cr3t" not in event["detail"]
    assert "db.internal" in event["detail"]  # only the credential is stripped


def test_ordinary_fields_pass_through_unmodified():
    event = redact_sensitive_fields(None, "info", {
        "event": "execution_completed",
        "execution_id": "11111111-1111-1111-1111-111111111111",
        "row_count": 42,
        "duration_ms": 1500,
    })
    assert event["execution_id"] == "11111111-1111-1111-1111-111111111111"
    assert event["row_count"] == 42
    assert event["duration_ms"] == 1500


def test_redaction_never_raises_even_on_malformed_input():
    # A non-string value under a sensitive key, weird types, nested structures
    # — none of this should ever propagate an exception out of a log call.
    event = redact_sensitive_fields(None, "info", {
        "password": 12345,
        "nested": {"still": "here"},
        "none_value": None,
        "a_list": [1, 2, 3],
    })
    assert event["password"] == "[REDACTED]"  # int under a sensitive key is still redacted
    assert event["nested"] == {"still": "here"}
    assert event["none_value"] is None
    assert event["a_list"] == [1, 2, 3]


def test_traceparent_extraction():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert _trace_id_from_traceparent(tp) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert _trace_id_from_traceparent(None) is None
    assert _trace_id_from_traceparent("garbage") is None


# --------------------------------------------------------------------------
# 2. Request/trace correlation middleware — real HTTP round trip
# --------------------------------------------------------------------------
def test_request_id_and_trace_id_are_generated_and_echoed(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("x-request-id")
    assert resp.headers.get("x-trace-id")
    # Every request gets its own id absent a caller-supplied one.
    resp2 = client.get("/healthz")
    assert resp.headers["x-request-id"] != resp2.headers["x-request-id"]


def test_caller_supplied_request_id_is_honored_not_overwritten(client):
    mine = "my-own-correlation-id-123"
    resp = client.get("/healthz", headers={"X-Request-Id": mine})
    assert resp.headers["x-request-id"] == mine


def test_caller_supplied_trace_id_is_honored(client):
    resp = client.get("/healthz", headers={"X-Trace-Id": "trace-abc"})
    assert resp.headers["x-trace-id"] == "trace-abc"


def test_context_is_cleared_between_requests(client):
    # If context leaked across requests, the second request (no header sent)
    # would echo back the first request's id instead of minting its own.
    first = client.get("/healthz", headers={"X-Request-Id": "should-not-leak"})
    assert first.headers["x-request-id"] == "should-not-leak"
    second = client.get("/healthz")
    assert second.headers["x-request-id"] != "should-not-leak"


# --------------------------------------------------------------------------
# 3. AuditService — standardized fields land in persisted metadata
# --------------------------------------------------------------------------
def test_audit_record_captures_bound_request_and_trace_id():
    captured: dict = {}

    class _FakeDB:
        def add(self, obj):
            captured["obj"] = obj

        async def flush(self):
            captured["flushed"] = True

    import asyncio

    async def _run():
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id="req-123", trace_id="trace-456", org_id="org-789",
        )
        try:
            svc = AuditService(_FakeDB())
            await svc.record(user_id=None, action="test.action")
        finally:
            structlog.contextvars.clear_contextvars()

    asyncio.run(_run())

    row: AuditLog = captured["obj"]
    assert row.metadata_["request_id"] == "req-123"
    assert row.metadata_["trace_id"] == "trace-456"
    assert row.metadata_["org_id"] == "org-789"
    assert captured["flushed"] is True


def test_audit_record_workspace_id_and_run_id_are_optional_and_additive():
    captured: dict = {}

    class _FakeDB:
        def add(self, obj):
            captured["obj"] = obj

        async def flush(self):
            pass

    import asyncio
    ws_id = uuid.uuid4()

    async def _run():
        structlog.contextvars.clear_contextvars()
        svc = AuditService(_FakeDB())
        # Existing call shape (no workspace_id/run_id) — must still work
        # exactly as every one of the ~30 pre-existing call sites expects.
        await svc.record(user_id=None, action="test.no_new_args")
        no_new_args_metadata = captured["obj"].metadata_

        await svc.record(user_id=None, action="test.with_new_args", workspace_id=ws_id, run_id="run-1")
        with_new_args_metadata = captured["obj"].metadata_
        return no_new_args_metadata, with_new_args_metadata

    no_new_args_metadata, with_new_args_metadata = asyncio.run(_run())
    assert "workspace_id" not in no_new_args_metadata
    assert "run_id" not in no_new_args_metadata
    assert with_new_args_metadata["workspace_id"] == str(ws_id)
    assert with_new_args_metadata["run_id"] == "run-1"


def test_caller_metadata_wins_on_key_collision_with_standardized_fields():
    captured: dict = {}

    class _FakeDB:
        def add(self, obj):
            captured["obj"] = obj

        async def flush(self):
            pass

    import asyncio

    async def _run():
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(org_id="context-org")
        try:
            svc = AuditService(_FakeDB())
            await svc.record(
                user_id=None, action="test.override",
                metadata={"org_id": "explicit-org-override"},
            )
        finally:
            structlog.contextvars.clear_contextvars()

    asyncio.run(_run())
    assert captured["obj"].metadata_["org_id"] == "explicit-org-override"
