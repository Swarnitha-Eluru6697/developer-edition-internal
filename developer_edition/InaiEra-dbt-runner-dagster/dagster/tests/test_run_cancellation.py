"""DG-017: abandoning a dbt run that Dagster started cancels it.

Before: `submit_run_op` started a Runner run and nothing tied it to Dagster's lifecycle: terminating the Dagster run
left the dbt Job running (and modifying data), as did a commit mismatch that failed the submit step after the Runner
had already queued the run. Now the client can cancel (`cancel_run`, `cancel_run_by_key`; the Runner applies its
DBT-005 cancelling -> cancelled semantics) and `submit_run_op` uses it when Dagster interrupts it or fails it for a
reason that leaves the Runner's run orphaned. (Since DG-018 the Dagster run no longer waits for the dbt run, so the
monitor sensor, not a polling step, cancels one that overruns: tests/test_run_monitor.py.) The real op and client; only
the Runner's HTTP side and the interrupt are faked.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

import httpx
import pytest
from dagster import DagsterInstance, build_op_context
from dagster._core.errors import DagsterExecutionInterruptedError

from inaiera_dbt_runner_dags.ops import trigger_and_poll as ops
from inaiera_dbt_runner_dags.ops.trigger_and_poll import submit_run_op
from inaiera_dbt_runner_dags.resources import dbt_runner_client as drc
from inaiera_dbt_runner_dags.resources.dbt_runner_client import (
    DbtRunnerClient,
    RunPollTimeout,
)

REAL_CLIENT = httpx.Client
SECRET = "s" * 32
SHA = "a" * 40


# ── the client ───────────────────────────────────────────────────────────
@pytest.fixture
def client():
    return DbtRunnerClient(base_url="http://runner.test", scheduler_jwt_secret=SECRET, org_id="acme")


@pytest.fixture
def runner(monkeypatch):
    """A scripted Runner: answers are consumed per (method, path); the last one repeats. Records every request."""
    class Runner:
        def __init__(self):
            self.script: dict[tuple[str, str], list[httpx.Response]] = {}
            self.seen: list[httpx.Request] = []

        def on(self, method, path, *responses):
            self.script[(method, path)] = list(responses)

        def __call__(self, req: httpx.Request) -> httpx.Response:
            self.seen.append(req)
            answers = self.script[(req.method, req.url.path)]
            return answers.pop(0) if len(answers) > 1 else answers[0]

    r = Runner()
    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(r), **kw))
    monkeypatch.setattr(drc.time, "sleep", lambda _s: None)
    return r


def _claims(req: httpx.Request) -> dict:
    """Verify the request's HS256 token with the standard library and return its claims."""
    head, payload, sig = req.headers["X-Internal-Service-Token"].split(".")
    good = hmac.new(SECRET.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    assert hmac.compare_digest(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), good), "bad signature"
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _run(status, rid="r1"):
    return httpx.Response(200, json={"id": rid, "status": status})


def test_cancel_run_posts_to_the_cancel_route_with_its_own_scope(client, runner):
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", _run("cancelled"))
    assert client.cancel_run("r1")["status"] == "cancelled"
    (req,) = runner.seen
    assert _claims(req)["scope"].split() == ["dbt.runs.cancel"]                     # not trigger, not read


def test_cancel_run_waits_for_the_runner_to_confirm_cancelled(client, runner):
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", _run("cancelling"))
    runner.on("GET", "/api/v1/internal/runs/r1", _run("cancelling"), _run("cancelling"), _run("cancelled"))
    run = client.cancel_run("r1", wait_seconds=10.0)
    assert run["status"] == "cancelled"
    assert [r.method for r in runner.seen] == ["POST", "GET", "GET", "GET"]
    assert len({_claims(r)["jti"] for r in runner.seen}) == 4                         # a fresh token every request


def test_cancel_run_stops_waiting_after_its_budget_and_reports_cancelling(client, runner, monkeypatch):
    clock = iter([0.0, 0.0, 4.0, 8.0, 12.0, 16.0])
    monkeypatch.setattr(drc.time, "monotonic", lambda: next(clock, 99.0))
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", _run("cancelling"))
    runner.on("GET", "/api/v1/internal/runs/r1", _run("cancelling"))
    assert client.cancel_run("r1", wait_seconds=10.0)["status"] == "cancelling"      # still the Runner's to finish


def test_cancel_run_without_a_wait_does_not_poll(client, runner):
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", _run("cancelling"))
    assert client.cancel_run("r1")["status"] == "cancelling"
    assert [r.method for r in runner.seen] == ["POST"]


def test_a_run_that_already_finished_is_reported_not_raised(client, runner):
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", httpx.Response(409, json={"detail": "run is already succeeded"}))
    runner.on("GET", "/api/v1/internal/runs/r1", _run("succeeded"))
    assert client.cancel_run("r1")["status"] == "succeeded"


def test_an_unknown_run_is_nothing_to_cancel(client, runner):
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", httpx.Response(404, json={"detail": "not found"}))
    assert client.cancel_run("r1") is None


def test_a_server_error_is_raised_for_the_caller_to_decide(client, runner):
    runner.on("POST", "/api/v1/internal/runs/r1/cancel", httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        client.cancel_run("r1")


def test_cancel_by_key_finds_a_submission_that_was_still_being_processed(client, runner):
    path = "/api/v1/internal/projects/p1/runs/cancel"
    runner.on("POST", path, httpx.Response(404), httpx.Response(404), _run("cancelled"))
    run = client.cancel_run_by_key("p1", "dagster:abc:submit_run_op", attempts=5)
    assert run["status"] == "cancelled" and len(runner.seen) == 3
    assert json.loads(runner.seen[0].content) == {"idempotency_key": "dagster:abc:submit_run_op"}
    assert _claims(runner.seen[0])["scope"].split() == ["dbt.runs.cancel"]


def test_cancel_by_key_gives_up_when_no_run_ever_appears(client, runner):
    runner.on("POST", "/api/v1/internal/projects/p1/runs/cancel", httpx.Response(404))
    assert client.cancel_run_by_key("p1", "k", attempts=4) is None and len(runner.seen) == 4


def test_cancel_by_key_treats_a_finished_run_as_done(client, runner):
    runner.on("POST", "/api/v1/internal/projects/p1/runs/cancel", httpx.Response(409))
    assert client.cancel_run_by_key("p1", "k") is None and len(runner.seen) == 1


def test_a_poll_that_hits_its_ceiling_raises_the_dedicated_timeout(client, runner, monkeypatch):
    clock = iter([0.0, 1.0, 5000.0])
    monkeypatch.setattr(drc.time, "monotonic", lambda: next(clock, 9999.0))
    runner.on("GET", "/api/v1/internal/runs/r1", _run("running"))
    with pytest.raises(RunPollTimeout):
        client.poll_run_until_terminal("r1", max_wait_seconds=60)
    assert issubclass(RunPollTimeout, RuntimeError)                                   # existing callers still catch it


# ── the ops ──────────────────────────────────────────────────────────────
class FakeClient:
    base_url = "http://runner.test"

    def __init__(self, *, poll=None, trigger=None, cancel_raises=None):
        self.poll, self.trigger, self.cancel_raises = poll, trigger, cancel_raises
        self.cancelled: list[tuple] = []

    def get_project(self, project_id):                    # the start check (dependencies.py): no links, so nothing above it
        return {"id": project_id, "upstream_project_ids": []}

    def list_projects(self):
        return []

    def trigger_run(self, project_id, **kw):
        self.triggered = (project_id, kw)
        if isinstance(self.trigger, BaseException):
            raise self.trigger
        return self.trigger or {"id": "run-1", "status": "queued"}

    def poll_run_until_terminal(self, run_id, **_kw):
        if isinstance(self.poll, BaseException):
            raise self.poll
        return self.poll

    def cancel_run(self, run_id, *, wait_seconds=0.0):
        self.cancelled.append(("run", run_id, wait_seconds))
        if self.cancel_raises:
            raise self.cancel_raises
        return {"id": run_id, "status": "cancelling"}

    def cancel_run_by_key(self, project_id, key, **kw):
        self.cancelled.append(("key", project_id, key))
        if self.cancel_raises:
            raise self.cancel_raises


def _ctx(client, **cfg):
    instance = DagsterInstance.ephemeral()
    instance.add_run_tags = lambda *_a, **_k: None          # no run record here: the tag hand-over has its own tests
    return build_op_context(resources={"dbt_runner": client}, instance=instance, op_config={"project_id": "p1", **cfg})


def _submit(client, **cfg):
    return submit_run_op(_ctx(client, **cfg))


def test_a_failing_cancel_never_hides_the_original_interrupt():
    c = FakeClient(trigger=DagsterExecutionInterruptedError(), cancel_raises=httpx.ConnectError("runner down"))
    with pytest.raises(DagsterExecutionInterruptedError):
        _submit(c)
    assert len(c.cancelled) == 1


def test_terminating_during_the_submission_cancels_by_idempotency_key():
    """The request may have been processed although its answer was never seen: no run id, so the key finds it."""
    c = FakeClient(trigger=DagsterExecutionInterruptedError())
    ctx = _ctx(c)
    with pytest.raises(DagsterExecutionInterruptedError):
        submit_run_op(ctx)
    ((kind, project, key),) = c.cancelled
    assert (kind, project) == ("key", "p1") and key == c.triggered[1]["idempotency_key"] == f"dagster:{ctx.run_id}:submit_run_op"


def test_a_failed_submission_request_cancels_nothing():
    c = FakeClient(trigger=httpx.HTTPStatusError("422", request=httpx.Request("POST", "http://x"),
                                                 response=httpx.Response(422)))
    with pytest.raises(httpx.HTTPStatusError):
        _submit(c)
    assert c.cancelled == []


@pytest.mark.parametrize("reported", ["b" * 40, None], ids=["other-commit", "no-commit-reported"])
def test_a_run_frozen_at_the_wrong_commit_is_cancelled_not_left_building(reported):
    """DG-007's check fails the step AFTER the Runner queued the run: that run must not go on to build the wrong commit."""
    c = FakeClient(trigger={"id": "run-1", "status": "queued", "git_sha": reported})
    with pytest.raises(RuntimeError, match="commit"):
        _submit(c, git_sha=SHA)
    assert c.cancelled == [("run", "run-1", ops.CANCEL_CONFIRM_SECONDS)]


def test_a_normal_submission_cancels_nothing():
    c = FakeClient(trigger={"id": "run-1", "status": "queued", "git_sha": SHA})
    assert _submit(c, git_sha=SHA) == "run-1" and c.cancelled == []


# ── Dagster and the runner agree on the route and the scope ──────────────
def test_the_client_calls_the_routes_the_runner_exposes():
    import os
    import pathlib
    runner = pathlib.Path(os.environ.get("DBTRUNNER_REPO", pathlib.Path(__file__).resolve().parents[3]
                                         / "InaiEra_workspace_runners" / "inaiera-dbt-runner")) / "app" / "api" / "projects.py"
    if not runner.exists():
        pytest.skip("the runner repo is not checked out next to this one")
    routes = set(re.findall(r'@router\.post\("(/internal/[^"]*cancel)"', runner.read_text()))
    client_src = pathlib.Path(drc.__file__).read_text()
    assert routes == {"/internal/runs/{run_id}/cancel", "/internal/projects/{project_id}/runs/cancel"}
    assert "/api/v1/internal/runs/{run_id}/cancel" in client_src
    assert "/api/v1/internal/projects/{project_id}/runs/cancel" in client_src
