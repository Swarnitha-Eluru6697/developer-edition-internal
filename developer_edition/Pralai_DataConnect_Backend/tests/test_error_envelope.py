"""
The standard error envelope (app/schemas/errors.py) and the exception
hierarchy/handlers that produce it (app/core/errors.py, app/main.py).

Two layers:
  1. Every DataConnectError subclass, exercised end-to-end through a real
     HTTP request — including the ones no domain service raises yet
     (SourceConnectivityError, SchemaConflictError, BackendUnavailableError,
     RequestTimeoutError) — proven via throwaway routes added to a *second*
     app instance from create_app() (identical exception-handler wiring to
     the real app, but never touches the shared `app` singleton every other
     test file's `TestClient(app)` reuses, so nothing here can leak into
     them). ConnectorConfigError, KafkaConnectError and QualityRuleError
     already have real call sites — those are covered by their own
     domain test files (test_connection_service_validation.py,
     test_runtime_orchestrator.py/test_kafka_connect_retry.py,
     test_quality_execution_modes.py) instead of repeated here.
  2. The generic fallback paths every route already goes through regardless
     of a DataConnectError ever being raised: RequestValidationError (a bad
     request body), a plain HTTPException, and an unhandled Exception.
"""

from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

from app.core.errors import (
    BackendUnavailableError,
    RequestTimeoutError,
    SchemaConflictError,
    SourceConnectivityError,
    ValidationError,
)
from app.main import app, create_app
from app.schemas.errors import FieldError

# ── A throwaway probe app, sharing app/main.py's exception-handler wiring ──
_probe_router = APIRouter()


@_probe_router.get("/_test/raise/{code}")
async def _raise_by_code(code: str) -> None:
    if code == "source_connectivity_error":
        raise SourceConnectivityError("The source database refused the connection.")
    if code == "schema_conflict":
        raise SchemaConflictError(
            "New schema version is not BACKWARD compatible.",
            field_errors=[FieldError(field="schema_definition.fields[2].type", message="type changed from string to integer")],
            details={"compatibility": "BACKWARD"},
        )
    if code == "backend_unavailable":
        raise BackendUnavailableError("Postgres is unreachable.")
    if code == "request_timeout":
        raise RequestTimeoutError("The source probe exceeded its deadline.")
    if code == "validation_error":
        raise ValidationError("Zone must be one of raw/curated/serving.")
    raise ValueError(f"unknown probe code: {code}")


_probe_app = create_app()
_probe_app.include_router(_probe_router, prefix="/api/v1/dataconnect")
# raise_server_exceptions=False: TestClient's default re-raises any exception
# that reaches a 5xx response for debugging convenience, regardless of
# whether a registered handler already converted it to a real response —
# which is exactly what these tests need to inspect, so the default would
# make every 5xx-shaped test below fail even though the handler worked fine
# (verified by hand against the real app: the response body is correctly
# enveloped either way, only TestClient's default re-raising gets in the way).
# Every route of an app built by create_app() — this probe router included — is
# behind the gateway (app/core/gateway.py), so the probe client sends gateway headers too.
probe_client = TestClient(_probe_app, raise_server_exceptions=False, headers=DEFAULT_GATEWAY_HEADERS)

client = TestClient(app, raise_server_exceptions=False, headers=DEFAULT_GATEWAY_HEADERS)  # the real app, for the generic (non-probe) paths


def _assert_envelope_shape(body: dict, *, code: str, retryable: bool) -> None:
    assert body["code"] == code
    assert isinstance(body["message"], str)
    assert body["message"]
    assert body["retryable"] is retryable
    assert "field_errors" in body
    assert "details" in body
    assert "request_id" in body


class TestDataConnectErrorSubclasses:
    def test_source_connectivity_error(self):
        resp = probe_client.get("/api/v1/dataconnect/_test/raise/source_connectivity_error")
        assert resp.status_code == 502
        _assert_envelope_shape(resp.json(), code="SOURCE_CONNECTION_FAILED", retryable=True)

    def test_schema_conflict_carries_field_errors_and_details(self):
        resp = probe_client.get("/api/v1/dataconnect/_test/raise/schema_conflict")
        assert resp.status_code == 409
        body = resp.json()
        _assert_envelope_shape(body, code="SCHEMA_INCOMPATIBLE", retryable=False)
        assert body["field_errors"] == [
            {"field": "schema_definition.fields[2].type", "message": "type changed from string to integer"}
        ]
        assert body["details"] == {"compatibility": "BACKWARD"}

    def test_backend_unavailable_error(self):
        resp = probe_client.get("/api/v1/dataconnect/_test/raise/backend_unavailable")
        assert resp.status_code == 503
        _assert_envelope_shape(resp.json(), code="DATABASE_UNAVAILABLE", retryable=True)

    def test_request_timeout_error(self):
        resp = probe_client.get("/api/v1/dataconnect/_test/raise/request_timeout")
        assert resp.status_code == 504
        _assert_envelope_shape(resp.json(), code="OPERATION_TIMEOUT", retryable=True)

    def test_validation_error_subclass(self):
        resp = probe_client.get("/api/v1/dataconnect/_test/raise/validation_error")
        assert resp.status_code == 422
        _assert_envelope_shape(resp.json(), code="validation_error", retryable=False)

    def test_request_id_echoes_the_caller_supplied_header(self):
        resp = probe_client.get(
            "/api/v1/dataconnect/_test/raise/backend_unavailable",
            headers={"X-Request-Id": "req-abc-123"},
        )
        assert resp.json()["request_id"] == "req-abc-123"
        assert resp.headers["X-Request-Id"] == "req-abc-123"


class TestGenericFallbackPaths:
    def test_request_validation_error_reports_field_errors(self):
        # source_type/connection_config are required on ConnectionProfileCreate.
        resp = client.post(
            "/api/v1/dataconnect/connections", headers={"X-Tenant-Id": "org-1"}, json={},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["retryable"] is False
        assert len(body["field_errors"]) >= 1
        assert all({"field", "message"} <= fe.keys() for fe in body["field_errors"])

    def test_plain_http_exception_maps_status_to_a_stable_code(self, monkeypatch):
        # Monkeypatched rather than hitting a real "missing row" 404 through
        # Postgres — consistent with test_api_integration.py's own pattern —
        # so this only exercises the HTTPException -> envelope mapping, not
        # a live database round trip.
        async def _not_found(*args, **kwargs):
            raise HTTPException(status_code=404, detail="Connection 'does-not-exist' not found.")

        monkeypatch.setattr("app.api.routes.connections.connection_service.get_or_404", _not_found)

        resp = client.get(
            "/api/v1/dataconnect/connections/does-not-exist", headers={"X-Tenant-Id": "org-1"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["retryable"] is False
        assert "does-not-exist" in body["message"]

    # A generic ValueError -> 422 "validation_error" is already exercised
    # end-to-end by tests/test_quality_execution_modes.py and
    # tests/test_schema_service.py's oversized-payload tests — not repeated
    # here to avoid a second, riskier mock of the same route.

    def test_unhandled_exception_never_leaks_internals(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("super secret stack trace detail")

        monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", _boom)

        resp = client.get("/api/v1/dataconnect/connector-templates")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "internal_error"
        assert body["retryable"] is True
        assert "secret" not in body["message"]
        assert body["message"] == "Internal server error"
