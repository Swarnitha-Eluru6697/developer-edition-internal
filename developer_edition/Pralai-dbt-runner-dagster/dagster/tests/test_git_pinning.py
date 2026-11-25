"""DG-007: the commit the git sensor sees is the commit the dbt Runner executes.

The sensor resolved commit A and put it in the run key and the Dagster run's tags, but the request it eventually
sent to the dbt Runner carried only project/select. The Runner then resolved the branch for itself, so if the branch
advanced between the sensor tick and the run's submission, a run labelled A built B.

Now the sensor's SHA travels in the run config -> submit_run_op -> the Runner request (`git_sha`), the Runner freezes
exactly that commit, and the op refuses to continue if the Runner reports a different one (which is also what an
older Runner that ignores the field looks like). The sensor's own resolver is exact too: pinning a wrong SHA would
now build the wrong commit, so `main` may no longer match `refs/heads/feature/main`.

Real local Git repositories, the real sensor, op and client; only the Runner's HTTP side is faked.
"""
from __future__ import annotations

import json

import httpx
import pytest
from pralai_dbt_runner_dags.location_defs import build_location_defs
from pralai_dbt_runner_dags.naming import location_name
from pralai_dbt_runner_dags.ops.trigger_and_poll import submit_run_op
from pralai_dbt_runner_dags.resources import dbt_runner_client as drc
from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

from dagster import DagsterInstance, build_op_context, build_sensor_context

REAL_CLIENT = httpx.Client


def _op_ctx(**kw):
    """An op context whose instance accepts the monitor hand-over tag (no run record exists here: DG-018's tagging has
    its own tests in test_run_monitor.py)."""
    instance = DagsterInstance.ephemeral()
    instance.add_run_tags = lambda *_a, **_k: None
    return build_op_context(instance=instance, **kw)


# ---------------------------------------------------------------------
# The sensor hands the SHA it saw to the run
# ---------------------------------------------------------------------
def _project(url, pid="1"):
    return {"id": pid, "name": "demo", "git_url": url, "git_ref": "main", "project_path": "",
            "schedule_cron": "*/2 * * * *", "updated_at": "2026-01-01T00:00:00+00:00", "last_synced_sha": None}


class _Runner:
    def __init__(self, projects, head):
        self._projects = projects
        self._head = head

    def list_git_triggered_projects(self):
        return self._projects

    def get_project_head(self, project_id):               # DG-009: the RUNNER resolves the ref
        return self._head


def _tick(projects, head):
    defs = build_location_defs(location_name(projects[0]), projects)
    sensor = next(s for s in defs.sensors if s.name == "git_commit_sensor")
    return sensor(build_sensor_context(resources={"dbt_runner": _Runner(projects, head)})).run_requests


TIP = "c" * 40


def test_the_run_request_carries_the_commit_the_sensor_saw():
    (req,) = _tick([_project("https://github.com/acme/demo.git")], TIP)
    submit_cfg = req.run_config["ops"]["submit_run_op"]["config"]
    assert submit_cfg["git_sha"] == TIP                              # what the run will ask the Runner to build
    assert req.tags["git_sha"] == TIP                                # what the run is labelled as
    assert req.run_key == f"1::{TIP}"


def test_a_branch_that_advances_after_the_tick_does_not_change_the_request():
    """The request is built from the head the Runner reported at THIS tick; nothing re-reads the branch later (DG-007)."""
    runner = _Runner([_project("https://github.com/acme/demo.git")], TIP)
    defs = build_location_defs(location_name(runner._projects[0]), runner._projects)
    sensor = next(s for s in defs.sensors if s.name == "git_commit_sensor")
    (req,) = sensor(build_sensor_context(resources={"dbt_runner": runner})).run_requests
    runner._head = "d" * 40                                          # the branch moves before the run is submitted
    assert req.run_config["ops"]["submit_run_op"]["config"]["git_sha"] == TIP


def test_the_generic_and_cron_jobs_pin_nothing():
    """Cron and manual runs have no commit of their own: the Runner resolves the ref at submission (DBT-006)."""
    defs = build_location_defs(location_name(_project("https://github.com/acme/demo.git")), [_project("https://github.com/acme/demo.git")])
    job = defs.jobs[0]
    assert job.run_config["ops"]["submit_run_op"]["config"].get("git_sha", "") == ""


# ---------------------------------------------------------------------
# submit_run_op: sends the SHA and refuses a Runner that built something else
# ---------------------------------------------------------------------
SHA_A = "a" * 40
SHA_B = "b" * 40


class _Client:
    base_url = "http://runner.test"

    def __init__(self, reported):
        self.reported = reported
        self.calls = []

    def get_project(self, project_id):                    # the start check (dependencies.py): no links, so nothing above it
        return {"id": project_id, "upstream_project_ids": []}

    def list_projects(self):
        return []

    def trigger_run(self, project_id, *, select=None, git_sha=None, idempotency_key=None, scheduled_fire_time=None, **_by_hand):
        self.calls.append({"project_id": project_id, "select": select, "git_sha": git_sha,
                           "idempotency_key": idempotency_key, "scheduled_fire_time": scheduled_fire_time})
        return {"id": "run-1", "status": "queued", "git_sha": self.reported}


def _submit(client, **cfg):
    ctx = _op_ctx(resources={"dbt_runner": client}, op_config={"project_id": "p1", **cfg})
    return submit_run_op(ctx)


def test_the_op_asks_the_runner_for_exactly_that_commit():
    client = _Client(reported=SHA_A)
    assert _submit(client, git_sha=SHA_A) == "run-1"
    (call,) = client.calls
    assert {k: call[k] for k in ("project_id", "select", "git_sha")} == {"project_id": "p1", "select": None, "git_sha": SHA_A}


def test_the_op_fails_if_the_runner_froze_a_different_commit():
    """Also what an older Runner that ignores `git_sha` (and runs the branch tip) looks like."""
    with pytest.raises(RuntimeError, match="commit"):
        _submit(_Client(reported=SHA_B), git_sha=SHA_A)


def test_the_op_fails_if_an_older_runner_reports_no_commit_at_all():
    with pytest.raises(RuntimeError, match="does not report"):
        _submit(_Client(reported=None), git_sha=SHA_A)


def test_the_comparison_ignores_case():
    assert _submit(_Client(reported=SHA_A.upper()), git_sha=SHA_A) == "run-1"


def test_without_a_commit_nothing_is_pinned_and_nothing_is_checked():
    client = _Client(reported=SHA_B)
    assert _submit(client) == "run-1"
    assert client.calls[0]["git_sha"] is None


# ---------------------------------------------------------------------
# The client sends it in the request body
# ---------------------------------------------------------------------
@pytest.fixture
def sent(monkeypatch):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content or b"{}"))
        return httpx.Response(202, json={"id": "r1", "status": "queued"})

    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return bodies


@pytest.fixture
def runner_client():
    return DbtRunnerClient(base_url="http://runner.test", scheduler_jwt_secret="s" * 32, org_id="acme")


def test_the_client_sends_git_sha_in_the_body(runner_client, sent):
    runner_client.trigger_run("p1", select="tag:x", git_sha=SHA_A)
    assert sent == [{"select": "tag:x", "git_sha": SHA_A}]


def test_the_client_sends_nothing_extra_when_unpinned(runner_client, sent):
    runner_client.trigger_run("p1")
    assert sent == [{}]


# ---------------------------------------------------------------------
# X-004: a retried step must not start a second dbt run
# ---------------------------------------------------------------------
def test_the_op_sends_an_idempotency_key_bound_to_its_dagster_run():
    client = _Client(reported=None)
    ctx = _op_ctx(resources={"dbt_runner": client}, op_config={"project_id": "p1"})
    submit_run_op(ctx)
    submit_run_op(ctx)                                               # the same step retried within the same Dagster run
    k1, k2 = (c["idempotency_key"] for c in client.calls)
    assert k1 == k2 and k1.startswith("dagster:") and ctx.run_id in k1


def test_a_different_dagster_run_has_a_different_key(monkeypatch):
    from unittest.mock import PropertyMock

    keys = []
    for run_id in ("run-a", "run-b"):
        client = _Client(reported=None)
        ctx = _op_ctx(resources={"dbt_runner": client}, op_config={"project_id": "p1"})
        monkeypatch.setattr(type(ctx), "run_id", PropertyMock(return_value=run_id))
        submit_run_op(ctx)
        keys.append(client.calls[0]["idempotency_key"])
    assert keys == ["dagster:run-a:submit_run_op", "dagster:run-b:submit_run_op"]


@pytest.fixture
def headers_sent(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("idempotency-key"))
        return httpx.Response(202, json={"id": "r1", "status": "queued"})

    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_the_client_sends_the_idempotency_key_as_a_header(runner_client, headers_sent):
    runner_client.trigger_run("p1", idempotency_key="dagster:abc:submit_run_op")
    runner_client.trigger_run("p1")
    assert headers_sent == ["dagster:abc:submit_run_op", None]
