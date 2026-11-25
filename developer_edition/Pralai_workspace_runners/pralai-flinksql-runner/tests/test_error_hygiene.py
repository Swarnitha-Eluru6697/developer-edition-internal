"""FL-010: an unexpected error answers with a stable generic body and a correlation id; the exception text (driver errors carry hostnames,
SQL fragments, internal service names) goes to the server log only."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

LEAK = 'connection to server at "db-primary.prod.internal" (10.0.3.4), port 5432 failed: FATAL: password authentication failed for user "flink_rw"'


@pytest.fixture
def boom():
    async def _boom():
        raise RuntimeError(LEAK)
    app.add_api_route("/__boom", _boom, methods=["GET"])
    try:
        yield
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__boom"]


def test_a_500_is_generic_and_carries_a_request_id(boom):
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/__boom")
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "INTERNAL_ERROR" and err["message"] == "An internal error occurred."
    assert re.fullmatch(r"[0-9a-f]{32}", err["request_id"]) and r.headers["X-Request-ID"] == err["request_id"]
    for fragment in ("db-primary", "10.0.3.4", "flink_rw", "password", "RuntimeError", "5432"):
        assert fragment not in r.text


def test_the_callers_request_id_is_kept_when_it_is_sane(boom):
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/__boom", headers={"X-Request-ID": "gateway-req-12345"})
    assert r.json()["error"]["request_id"] == "gateway-req-12345" and r.headers["X-Request-ID"] == "gateway-req-12345"


@pytest.mark.parametrize("bad", ["short", "has space and \"quotes\"", "x" * 200, "bad/slash/chars<>"])
def test_an_unsafe_request_id_is_replaced(boom, bad):
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/__boom", headers={"X-Request-ID": bad})
    assert re.fullmatch(r"[0-9a-f]{32}", r.json()["error"]["request_id"])


def test_the_detail_is_logged_with_the_same_id(boom, capsys):
    import structlog
    with structlog.testing.capture_logs() as logs, TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/__boom")
    rid = r.json()["error"]["request_id"]
    entry = next(e for e in logs if e["event"] == "unhandled_error")
    assert entry["request_id"] == rid and entry["error_type"] == "RuntimeError"


def test_normal_responses_carry_the_id_too():
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and re.fullmatch(r"[0-9a-f]{32}", r.headers["X-Request-ID"])


def test_http_errors_keep_their_own_body(client):
    r = client.get("/api/v1/jobs")                       # no token: the route's own 401, not the generic handler
    assert r.status_code == 401
