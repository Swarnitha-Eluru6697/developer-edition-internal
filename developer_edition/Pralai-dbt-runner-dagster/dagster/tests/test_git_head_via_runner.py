"""DG-009: Dagster never talks to Git. The git sensor asks the dbt Runner for each project's current commit.

The chart advertised `codeServer.gitCredentials` but mounted it nowhere, and the sensor ran `git ls-remote` itself, so a private repository
worked in a developer's environment (their own credentials) and failed in production. The Runner already owns the repository credentials
(`credential_ref`) and the Git egress policy, so it does the resolving: `GET /internal/projects/{id}/head`.
"""
from __future__ import annotations

import subprocess

import httpx
import pytest
from pralai_dbt_runner_dags.location_defs import build_location_defs
from pralai_dbt_runner_dags.naming import location_name
from pralai_dbt_runner_dags.resources import dbt_runner_client as drc
from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient
from pralai_dbt_runner_dags.sensors import git_sensor

from dagster import build_sensor_context

REAL_CLIENT = httpx.Client
SHA = "a" * 40


def _project(pid, name="demo", path=""):
    return {"id": pid, "name": name, "git_url": "https://github.com/acme/private.git", "git_ref": "main", "project_path": path,
            "git_trigger_enabled": True, "updated_at": "2026-01-01T00:00:00+00:00", "last_synced_sha": None}


class _Runner:
    def __init__(self, projects, heads):
        self._projects, self._heads, self.asked = projects, heads, []

    def list_git_triggered_projects(self):
        return self._projects

    def get_project_head(self, project_id):
        self.asked.append(project_id)
        head = self._heads[project_id]
        if isinstance(head, Exception):
            raise head
        return head


def _tick(runner):
    defs = build_location_defs(location_name(runner._projects[0]), runner._projects)
    sensor = next(s for s in defs.sensors if s.name == "git_commit_sensor")
    return sensor(build_sensor_context(resources={"dbt_runner": runner})).run_requests


def test_the_sensor_runs_no_git_at_all(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError(f"the code server must not execute anything: {a}")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    runner = _Runner([_project("1")], {"1": SHA})
    (req,) = _tick(runner)
    assert req.tags["git_sha"] == SHA and req.run_key == f"1::{SHA}"
    assert runner.asked == ["1"]                                         # the runner was asked for THAT project's commit


def test_the_sensor_module_has_no_git_machinery_left():
    for name in ("subprocess", "_remote_head_sha", "ls_remote", "_redact"):
        assert not hasattr(git_sensor, name), name


def test_a_project_whose_head_is_unavailable_is_skipped_this_tick_and_the_others_still_fire():
    err = httpx.HTTPStatusError("502", request=httpx.Request("GET", "http://r"), response=httpx.Response(502))
    runner = _Runner([_project("1", "a"), _project("2", "b", "b")], {"1": err, "2": SHA})
    reqs = _tick(runner)
    assert [r.tags["project_id"] for r in reqs] == ["2"]
    assert runner.asked == ["1", "2"]


def test_a_head_equal_to_the_last_synced_sha_fires_nothing():
    p = _project("1")
    p["last_synced_sha"] = SHA
    assert _tick(_Runner([p], {"1": SHA})) == []


# ── the client ────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/missing/head"):
            return httpx.Response(502, json={"detail": {"code": "GIT_UNREACHABLE", "message": "cannot read"}})
        return httpx.Response(200, json={"project_id": "p1", "git_ref": "main", "sha": SHA})

    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return DbtRunnerClient(base_url="http://runner.test", org_id="acme", scheduler_jwt_secret="s" * 32), seen


def test_the_client_asks_the_runner_for_the_head_with_its_scoped_token(client):
    client, seen = client
    assert client.get_project_head("p1") == SHA
    (req,) = seen
    assert req.method == "GET" and req.url.path == "/api/v1/internal/projects/p1/head"
    assert req.headers["X-Internal-Service-Token"]                       # Dagster's own scoped identity, not a Git credential


def test_the_client_raises_when_the_runner_cannot_resolve(client):
    client, _ = client
    with pytest.raises(httpx.HTTPStatusError):
        client.get_project_head("missing")
