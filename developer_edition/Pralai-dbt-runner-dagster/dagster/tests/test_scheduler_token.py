"""DBT-003: Dagster authenticates to the dbt Runner with its OWN short-lived, scoped JWT, never the static token.

iss/sub "dagster", aud "dbtrunner", ONE scope per call (dbt.projects.read / dbt.runs.read / dbt.runs.trigger),
this data plane's org, a fresh jti every call, signed (HS256) with SCHEDULER_JWT_SECRET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from pralai_dbt_runner_dags import location_defs
from pralai_dbt_runner_dags.resources import dbt_runner_client as drc
from pralai_dbt_runner_dags.resources.dbt_runner_client import (
    DbtRunnerClient,
    mint_scheduler_token,
)

SECRET = "dagster-scheduler-signing-secret"
ORG = "acme"
REAL_CLIENT = httpx.Client      # captured before any test patches it


def decode(token: str, secret: str = SECRET) -> dict:
    """Verify an HS256 JWT with the standard library and return its claims."""
    head, payload, sig = token.split(".")
    good = hmac.new(secret.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    assert hmac.compare_digest(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), good), "bad signature"
    assert json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))["alg"] == "HS256"
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@pytest.fixture
def seen(monkeypatch):
    """Point every httpx.Client at a fake runner and record the requests it gets."""
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.url.path.endswith("/head"):
            return httpx.Response(200, json={"project_id": "p1", "git_ref": "main", "sha": "a" * 40})
        return httpx.Response(200, json=[] if req.method == "GET" and "runs" not in req.url.path else {"id": "r1", "status": "succeeded"})

    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(location_defs.httpx, "get", lambda url, headers=None, timeout=None: _get(handler, url, headers))
    return requests


def _get(handler, url, headers):
    req = httpx.Request("GET", url, headers=headers)
    resp = handler(req)
    resp.request = req
    return resp


@pytest.fixture
def client():
    return DbtRunnerClient(base_url="http://runner.test", scheduler_jwt_secret=SECRET, org_id=ORG)


def claims_of(req: httpx.Request) -> dict:
    assert "authorization" not in {k.lower() for k in req.headers}
    return decode(req.headers["X-Internal-Service-Token"])


# ── the token ────────────────────────────────────────────────────────────
def test_the_token_carries_exactly_what_the_runner_requires():
    c = decode(mint_scheduler_token(SECRET, ORG, "dbt.runs.trigger"))
    assert (c["iss"], c["sub"], c["aud"], c["org_id"]) == ("dagster", "dagster", "dbtrunner", ORG)
    assert c["scope"].split() == ["dbt.runs.trigger"]                   # one operation only
    assert 0 < c["exp"] - c["iat"] <= 300                               # the runner rejects longer lifetimes
    assert c["jti"]


def test_every_token_is_fresh():
    a, b = (decode(mint_scheduler_token(SECRET, ORG, "dbt.projects.read"))["jti"] for _ in range(2))
    assert a != b


def test_no_secret_means_no_token():
    with pytest.raises(ValueError):
        mint_scheduler_token("", ORG, "dbt.projects.read")


# ── every client call uses its own scope, and never the static token ─────
@pytest.mark.parametrize("call,scope", [
    (lambda c: c.list_projects(), "dbt.projects.read"),
    (lambda c: c.list_scheduled_projects(), "dbt.projects.read"),
    (lambda c: c.list_git_triggered_projects(), "dbt.projects.read"),      # DG-008
    (lambda c: c.get_project_head("p1"), "dbt.projects.read"),             # DG-009
    (lambda c: c.get_project("p1"), "dbt.projects.read"),
    (lambda c: c.trigger_run("p1"), "dbt.runs.trigger"),
    (lambda c: c.get_run("r1"), "dbt.runs.read"),
    (lambda c: c.cancel_run("r1"), "dbt.runs.cancel"),
])
def test_each_call_sends_a_scoped_token(client, seen, call, scope):
    call(client)
    (req,) = seen
    assert claims_of(req)["scope"].split() == [scope]
    assert req.url.path.startswith("/api/v1/internal/")


def test_polling_mints_a_new_token_per_request(client, seen, monkeypatch):
    monkeypatch.setattr(drc.time, "sleep", lambda _s: None)
    states = iter(["running", "running", "succeeded"])

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "r1", "status": next(states)})
    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    client.poll_run_until_terminal("r1")
    assert len({claims_of(r)["jti"] for r in seen}) == 3                # no token is ever reused


def test_the_static_internal_token_is_never_sent(client, seen, monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "local-dev-token")
    client.list_projects()
    client.trigger_run("p1")
    assert all("local-dev-token" not in req.headers.get("X-Internal-Service-Token", "") for req in seen)


def test_the_resource_no_longer_has_a_static_token_field():
    assert "internal_service_token" not in DbtRunnerClient.model_fields
    assert {"scheduler_jwt_secret", "org_id"} <= set(DbtRunnerClient.model_fields)


# ── the code-location loader (direct call, no client) ────────────────────
def test_the_location_loader_uses_a_scoped_token_too(seen, monkeypatch):
    monkeypatch.setenv("SCHEDULER_JWT_SECRET", SECRET)
    monkeypatch.setenv("ORG_ID", ORG)
    monkeypatch.setenv("DBT_RUNNER_BASE_URL", "http://runner.test")
    location_defs.fetch_all_projects(attempts=1)
    (req,) = seen
    assert claims_of(req)["scope"].split() == ["dbt.projects.read"]


def test_the_location_loader_retries_with_a_new_token_each_attempt(monkeypatch):
    monkeypatch.setenv("SCHEDULER_JWT_SECRET", SECRET)
    monkeypatch.setenv("ORG_ID", ORG)
    monkeypatch.setattr(location_defs.time, "sleep", lambda _s: None)
    jtis = []

    def fake_get(url, headers=None, timeout=None):
        jtis.append(decode(headers["X-Internal-Service-Token"])["jti"])
        if len(jtis) < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))
    monkeypatch.setattr(location_defs.httpx, "get", fake_get)
    location_defs.fetch_all_projects(attempts=5, delay_seconds=0)
    assert len(jtis) == 3 and len(set(jtis)) == 3


# ── Dagster and the runner agree on the vocabulary ───────────────────────
RUNNER = Path(os.environ.get("DBTRUNNER_REPO", Path(__file__).resolve().parents[3] / "Pralai_workspace_runners" / "pralai-dbt-runner"))


@pytest.mark.skipif(not (RUNNER / "app" / "api" / "projects.py").exists(), reason="runner repo not checked out next to this one")
def test_scopes_and_issuer_match_the_dbt_runner():
    projects = (RUNNER / "app" / "api" / "projects.py").read_text()
    runner_scopes = set(re.findall(r'require_scheduler_scope\("([a-z.]+)"\)', projects))
    used = {s for s in re.findall(r'"(dbt\.[a-z.]+)"', Path(drc.__file__).read_text())}
    assert runner_scopes == {"dbt.projects.read", "dbt.runs.read", "dbt.runs.trigger", "dbt.runs.cancel"}
    assert used == runner_scopes
    assert 'scheduler_jwt_allowed_issuers: list[str] = ["dagster"]' in (RUNNER / "app" / "core" / "config.py").read_text()


def test_the_git_listing_uses_its_own_route(client, seen):
    client.list_git_triggered_projects()
    (req,) = seen
    assert req.method == "GET" and req.url.path == "/api/v1/internal/projects/git-triggered"
