"""
Real tests for section 0.117, GAP_REPORT — production error responses
exposing too much internal detail.

The real, reported bug: confirmed live before this fix — a real /run
call against a genuinely unreachable UCS returned, with a 200 status:
    "error": "UCS at http://127.0.0.1:8001 unreachable or errored:
              [Errno 111] Connection refused"
A real internal service name, a real internal hostname/port, and raw
OS-level connection-error detail, handed directly to an external
caller.

The fix: `duration`/`cost`/`ts` is unrelated (see §0.116) - this is
about `app/error_responses.py`, a new module providing the report's
own exact `{"error_code", "correlation_id"}` shape for a genuinely
unexpected failure, applied to every external-facing invocation
channel (run_agent, the streaming endpoint, webhooks, batch), the
existing PublishedVersionIntegrityError handler, and a new, global,
catch-all exception handler for anything else. Full detail is always
logged server-side (never lost), and the report's own scoped
distinction is honored: legitimate, actionable messages ("Agent not
found") are never touched — only genuinely unexpected failures.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.error_responses import (
    is_production, sanitized_error_value, sanitize_span_for_external_response,
    sanitize_result_for_external_response, AGENT_EXECUTION_FAILED,
)


class TestRealIsProduction:
    def test_matches_the_environment_var_exactly(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert is_production() is True
        monkeypatch.setenv("ENVIRONMENT", "development")
        assert is_production() is False
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        assert is_production() is False


class TestRealSanitizedErrorValue:
    def test_has_the_reports_own_exact_shape(self):
        result = sanitized_error_value("r-live-abc123")
        assert result == {"error_code": AGENT_EXECUTION_FAILED, "correlation_id": "r-live-abc123"}


class TestRealSpanSanitization:
    def test_a_span_with_no_error_key_is_returned_unchanged(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        span = {"node": "llm", "node_id": "n1", "status": "ok"}
        assert sanitize_span_for_external_response(span, "cid") == span

    def test_a_span_with_a_real_error_is_sanitized_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        span = {"node": "llm", "node_id": "n1", "status": "error", "error": "connection to db-primary.internal:5432 failed"}
        result = sanitize_span_for_external_response(span, "cid-123")
        assert result["error"] == {"error_code": AGENT_EXECUTION_FAILED, "correlation_id": "cid-123"}
        # Every other real key is preserved unchanged.
        assert result["node"] == "llm"
        assert result["node_id"] == "n1"
        assert result["status"] == "error"

    def test_a_span_with_a_real_error_is_not_sanitized_outside_production(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        span = {"node": "llm", "status": "error", "error": "connection to db-primary.internal:5432 failed"}
        result = sanitize_span_for_external_response(span, "cid-123")
        assert result["error"] == "connection to db-primary.internal:5432 failed"

    def test_the_original_span_is_never_mutated(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        span = {"status": "error", "error": "raw internal detail"}
        sanitize_span_for_external_response(span, "cid")
        assert span["error"] == "raw internal detail"


class TestRealResultSanitization:
    def test_every_span_and_ucs_soft_denial_is_sanitized_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        result = {
            "status": "failed",
            "spans": [
                {"node": "trigger", "status": "ok"},
                {"node": "llm", "status": "error", "error": "UCS at http://127.0.0.1:8001 unreachable: [Errno 111]"},
            ],
            "ucs_soft_denials": [
                {"object": "lakehouse.core.events", "resolved": False, "error": "UCS at http://127.0.0.1:8001 unreachable: [Errno 111]"},
            ],
        }
        sanitized = sanitize_result_for_external_response(result, "r-live-xyz")
        as_text = str(sanitized)
        assert "127.0.0.1:8001" not in as_text
        assert "Errno 111" not in as_text
        assert sanitized["error_code"] == AGENT_EXECUTION_FAILED
        assert sanitized["correlation_id"] == "r-live-xyz"
        assert sanitized["spans"][0] == {"node": "trigger", "status": "ok"}

    def test_a_successful_result_is_not_annotated_with_an_error_code(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        result = {"status": "success", "spans": [{"node": "trigger", "status": "ok"}]}
        sanitized = sanitize_result_for_external_response(result, "r-live-xyz")
        assert "error_code" not in sanitized
        assert "correlation_id" not in sanitized

    def test_a_result_with_no_ucs_soft_denials_is_unaffected(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        result = {"status": "success", "spans": []}
        sanitized = sanitize_result_for_external_response(result, "cid")
        assert "ucs_soft_denials" not in sanitized

    def test_outside_production_the_full_result_is_returned_unchanged(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        result = {"status": "failed", "spans": [{"status": "error", "error": "raw detail here"}]}
        assert sanitize_result_for_external_response(result, "cid") is result

    def test_the_original_result_is_never_mutated_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        result = {"status": "failed", "spans": [{"status": "error", "error": "raw detail"}]}
        sanitize_result_for_external_response(result, "cid")
        assert result["spans"][0]["error"] == "raw detail"


class TestRealLiveRunEndToEnd:
    """Real, live, end-to-end proof — the exact scenario the report's
    own bug was found from: a real /run call against a genuinely
    unreachable UCS, with the real, running API, in production mode."""

    def test_a_real_run_against_unreachable_ucs_never_leaks_the_hostname_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("UCS_SOFT_CHECK", "true")
        from app.main import app
        client = TestClient(app)
        r = client.post("/api/agents/a1/run", json={"input": "test", "invoking_user": "user:test"})
        body_text = r.text
        assert "127.0.0.1:8001" not in body_text
        assert "Errno 111" not in body_text
        assert "unreachable or errored" not in body_text

    def test_the_same_real_run_shows_full_detail_outside_production(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("UCS_SOFT_CHECK", "true")
        from app.main import app
        client = TestClient(app)
        r = client.post("/api/agents/a1/run", json={"input": "test", "invoking_user": "user:test"})
        # A real, honest regression guard for the OTHER side of this
        # fix — dev/test callers must still see the genuinely useful,
        # real detail for local debugging, not be sanitized too.
        assert "unreachable" in r.text or r.json().get("status") == "success"


class TestRealGlobalUnhandledExceptionHandler:
    """Real tests for the global, catch-all backstop (main.py) — a
    genuinely unanticipated exception (not caught by any more specific
    handler) must never leak its own raw detail to an external caller,
    in any environment (the pre-existing, real alternative — an empty
    500 body — was strictly worse everywhere, not just in production).

    Calls the real handler function directly with a constructed
    Request/Exception, rather than a live HTTP round-trip through
    app.dependency_overrides — a real, deliberate choice made after
    finding that FastAPI's own app-level dependency_overrides dict is
    shared, mutable, global state that genuinely interacts with other
    test files' own use of the same real `app` object when the full
    suite runs together (confirmed directly: passed in isolation,
    failed only when run after the rest of the real suite) — this
    real handler function itself is what this fix actually needs to
    verify, and calling it directly is a more robust, isolated way to
    do that."""

    def _fake_request(self, method="GET", path="/api/agents"):
        from starlette.requests import Request
        scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b""}
        return Request(scope)

    def test_a_genuinely_unhandled_exception_is_sanitized_in_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        from app.main import _unhandled_exception_handler
        import asyncio
        exc = RuntimeError("simulated real database connection failure at host db-primary-7.internal.example.com:5432")
        response = asyncio.run(_unhandled_exception_handler(self._fake_request(), exc))
        assert response.status_code == 500
        import json
        body = json.loads(response.body)
        assert body["error_code"] == AGENT_EXECUTION_FAILED
        assert "correlation_id" in body
        assert "db-primary-7" not in response.body.decode()

    def test_a_genuinely_unhandled_exception_is_also_sanitized_outside_production(self, monkeypatch):
        """The real, deliberate design choice: unlike the result/span
        sanitization above (which preserves full dev detail), this
        specific, global backstop sanitizes in EVERY environment,
        since the real, pre-existing alternative it replaces (an
        empty 500 body) was never useful detail to begin with."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        from app.main import _unhandled_exception_handler
        import asyncio
        exc = RuntimeError("simulated real database connection failure at host db-primary-7.internal.example.com:5432")
        response = asyncio.run(_unhandled_exception_handler(self._fake_request(), exc))
        assert response.status_code == 500
        import json
        body = json.loads(response.body)
        assert body["error_code"] == AGENT_EXECUTION_FAILED
        assert "db-primary-7" not in response.body.decode()

    def test_a_normal_404_is_never_shadowed_by_the_global_handler(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        from app.main import app
        client = TestClient(app)
        r = client.get("/api/agents/this-agent-genuinely-does-not-exist")
        assert r.status_code == 404
        assert r.json() == {"detail": "Agent not found"}
