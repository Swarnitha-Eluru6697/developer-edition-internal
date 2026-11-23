"""X-007: a service behind Workspace being down or frozen is a fast, clear 502 / 504 — not a minute-long hang and a bare 500.

Found by the live outage scenario (scripts/e2e): pausing the Flink runner made `GET /flink/jobs` hang for the proxies' 60 s
timeout and then fail with an unhandled httpx error. Now connecting is bounded separately and short, reading is bounded, and the
transport failures become the standard envelope: 502 UPSTREAM_UNAVAILABLE (cannot connect) or 504 UPSTREAM_TIMEOUT (no answer).
A service's own answer, whatever its status, passes through untouched.
"""
from __future__ import annotations

import asyncio
import types

import httpx
import pytest
from conftest import _headers

from app.api import flink_proxy
from app.core.config import get_settings
from app.core.errors import WorkspaceError
from app.core.upstream import upstream_guard, upstream_timeout

ROLES = ["workspace_admin"]
ROUTES = [
    ("dbt runner", "GET", "/api/v1/dbt/projects"),
    ("Flink runner", "GET", "/api/v1/flink/jobs"),
    ("Flink runner", "POST", "/api/v1/flink/jobs"),
    ("Data Quality", "GET", "/api/v1/dq/rules"),
    ("Data Quality", "POST", "/api/v1/dq/rules"),
]


@pytest.fixture
def services(monkeypatch):
    s = get_settings()
    for attr, value in {"dbtrunner_delegation_jwt_secret": "d" * 40, "flinksql_delegation_jwt_secret": "f" * 40,
                        "dq_delegation_jwt_secret": "q" * 40, "dbtrunner_url": "http://dbt.test", "flinksql_runner_url": "http://flink.test",
                        "dq_service_url": "http://dq.test", "data_policy_mode": "off"}.items():
        monkeypatch.setattr(s, attr, value)
    state = {"behaviour": None}

    def handler(req: httpx.Request) -> httpx.Response:
        b = state["behaviour"]
        if isinstance(b, Exception):
            raise b
        return b or httpx.Response(200, json=[])

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return state


def _go(client, method, path):
    return client.request(method, path, headers=_headers(roles=ROLES), json={} if method == "POST" else None)


@pytest.mark.parametrize("service,method,path", ROUTES)
@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow connect"), httpx.RemoteProtocolError("reset"),
                                      httpx.NetworkError("down")])
def test_an_unreachable_service_is_a_502_with_the_standard_envelope(client, services, service, method, path, failure):
    services["behaviour"] = failure
    r = _go(client, method, path)
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "UPSTREAM_UNAVAILABLE" and service.split()[0].lower() in detail["message"].lower()


@pytest.mark.parametrize("service,method,path", ROUTES)
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("no answer"), httpx.WriteTimeout("stalled"), httpx.PoolTimeout("busy")])
def test_a_service_that_does_not_answer_is_a_504(client, services, service, method, path, failure):
    services["behaviour"] = failure
    r = _go(client, method, path)
    assert r.status_code == 504 and r.json()["detail"]["code"] == "UPSTREAM_TIMEOUT", r.text


@pytest.mark.parametrize("service,method,path", ROUTES)
@pytest.mark.parametrize("status", [400, 404, 409, 422, 500, 503])
def test_a_service_that_answers_is_passed_through_untouched(client, services, service, method, path, status):
    services["behaviour"] = httpx.Response(status, json={"detail": {"code": "THEIRS"}})
    r = _go(client, method, path)
    assert r.status_code == status and r.json() == {"detail": {"code": "THEIRS"}}


def test_the_runner_call_helper_maps_failures_too():
    user = types.SimpleNamespace(user_id="u", org_id="o", roles=ROLES, email="e")
    s = get_settings()

    async def go():
        real = httpx.AsyncClient
        try:
            httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))), **kw)
            s.flinksql_delegation_jwt_secret, s.flinksql_runner_url = "f" * 40, "http://flink.test"
            await flink_proxy._runner_call(user, "flink.jobs.read", "GET", "jobs")
        finally:
            httpx.AsyncClient = real

    with pytest.raises(WorkspaceError) as exc:
        asyncio.run(go())
    assert exc.value.status_code == 502


def test_connecting_is_bounded_separately_and_shorter_than_reading():
    t = upstream_timeout()
    s = get_settings()
    assert t.connect == s.upstream_connect_timeout_seconds and t.read == s.upstream_timeout_seconds
    assert t.connect < t.read <= 60
    assert upstream_timeout(slow=True).read == 2 * s.upstream_timeout_seconds


def test_the_guard_leaves_other_exceptions_alone():
    with pytest.raises(ValueError):
        with upstream_guard("x"):
            raise ValueError("a bug is not an outage")


def test_a_frozen_service_is_noticed_within_the_read_bound_not_a_minute(client, services, monkeypatch):
    """The documented defaults: a service that accepted the connection and then froze costs at most upstream_timeout_seconds."""
    s = get_settings()
    assert s.upstream_connect_timeout_seconds <= 10 and s.upstream_timeout_seconds <= 30
