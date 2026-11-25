"""DG-006: the cron occurrence Dagster fires reaches the dbt Runner, which claims it atomically with the run.

The sensor's comment called the Runner's UNIQUE(project, scheduled_fire_time) a second layer of dedupe, but the
instant stayed in the Dagster run config: `submit_run_op` never passed it on and the client never sent it, so the Runner
never saw an occurrence and Dagster's own run_key was the ONLY guard. Now the instant travels
sensor -> run config -> submit_run_op -> trigger body (`scheduled_fire_time`) and the Runner deduplicates on it, so a
replay that gets past the run_key (history pruned, cursor reset, a re-created location) cannot start a second dbt run.

The real sensor, op and client; only the Runner's HTTP side is faked.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from dagster import DagsterInstance, build_op_context, build_sensor_context

from pralai_dbt_runner_dags.location_defs import build_location_defs
from pralai_dbt_runner_dags.naming import location_name
from pralai_dbt_runner_dags.ops.trigger_and_poll import submit_run_op
from pralai_dbt_runner_dags.resources import dbt_runner_client as drc
from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

REAL_CLIENT = httpx.Client


def _op_ctx(**kw):
    """An op context whose instance accepts the monitor hand-over tag (no run record exists here: DG-018's tagging has
    its own tests in test_run_monitor.py)."""
    instance = DagsterInstance.ephemeral()
    instance.add_run_tags = lambda *_a, **_k: None
    return build_op_context(instance=instance, **kw)
INSTANT = "2026-10-14T06:00:00+00:00"


@pytest.fixture
def sent(monkeypatch):
    """Every run submission the client makes: (path, json body, headers)."""
    seen: list[tuple[str, dict, httpx.Headers]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":                                     # the start check (dependencies.py) asks for the project's links
            return httpx.Response(200, json={"id": "p1", "upstream_project_ids": []})
        seen.append((req.url.path, json.loads(req.content or b"{}"), req.headers))
        return httpx.Response(202, json={"id": "r1", "status": "queued"})

    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    return seen


@pytest.fixture
def client():
    return DbtRunnerClient(base_url="http://runner.test", scheduler_jwt_secret="s" * 32, org_id="acme")


# ── the client ───────────────────────────────────────────────────────────
def test_the_client_sends_the_occurrence_in_the_trigger_body(client, sent):
    client.trigger_run("p1", scheduled_fire_time=INSTANT)
    ((path, body, _),) = sent
    assert path == "/api/v1/internal/projects/p1/run" and body == {"scheduled_fire_time": INSTANT}


def test_the_client_sends_nothing_extra_without_one(client, sent):
    client.trigger_run("p1", select="tag:x")
    assert sent[0][1] == {"select": "tag:x"}


# ── the op ───────────────────────────────────────────────────────────────
class _Client:
    base_url = "http://runner.test"

    def __init__(self):
        self.calls = []

    def get_project(self, project_id):                    # the start check (dependencies.py): no links, so nothing above it
        return {"id": project_id, "upstream_project_ids": []}

    def list_projects(self):
        return []

    def trigger_run(self, project_id, **kw):
        self.calls.append({"project_id": project_id, **kw})
        return {"id": "run-1", "status": "queued"}


def _submit(client, **cfg):
    return submit_run_op(_op_ctx(resources={"dbt_runner": client}, op_config={"project_id": "p1", **cfg}))


def test_the_op_passes_the_occurrence_on():
    client = _Client()
    _submit(client, scheduled_fire_time=INSTANT)
    assert client.calls[0]["scheduled_fire_time"] == INSTANT


@pytest.mark.parametrize("cfg", [{}, {"scheduled_fire_time": ""}, {"scheduled_fire_time": "  "}])
def test_an_unscheduled_run_sends_no_occurrence(cfg):
    client = _Client()
    _submit(client, **cfg)
    assert client.calls[0]["scheduled_fire_time"] is None


# ── the whole path: sensor -> op -> client -> HTTP ───────────────────────
def _project():
    return {"id": "1", "name": "demo", "git_url": "https://github.com/acme/demo.git", "git_ref": "main",
            "project_path": "", "schedule_cron": "*/2 * * * *", "last_synced_sha": None,
            "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}


class _Runner:
    def list_scheduled_projects(self):
        return [_project()]


def _cron_request():
    defs = build_location_defs(location_name(_project()), [_project()])
    sensor = next(s for s in defs.sensors if s.name == "project_schedule_sensor")
    (req,) = sensor(build_sensor_context(resources={"dbt_runner": _Runner()})).run_requests
    return req


def test_the_sensors_instant_is_a_timezone_aware_utc_time_matching_its_run_key():
    req = _cron_request()
    instant = req.run_config["ops"]["submit_run_op"]["config"]["scheduled_fire_time"]
    parsed = datetime.fromisoformat(instant)
    assert parsed.utcoffset() == timedelta(0)                       # the Runner requires a zone; the key is an instant
    assert req.run_key == f"1::{instant}" and req.tags["scheduled_fire_time"] == instant


def test_the_cron_occurrence_reaches_the_runner_and_a_replay_sends_the_same_instant(client, sent, monkeypatch):
    """Two DIFFERENT Dagster runs for one occurrence: different idempotency keys, the SAME occurrence in the body."""
    from unittest.mock import PropertyMock

    cfg = _cron_request().run_config["ops"]["submit_run_op"]["config"]
    for run_id in ("dagster-run-a", "dagster-run-b"):
        ctx = _op_ctx(resources={"dbt_runner": client}, op_config=cfg)
        monkeypatch.setattr(type(ctx), "run_id", PropertyMock(return_value=run_id))
        submit_run_op(ctx)
    bodies = [b for _, b, _ in sent]
    assert bodies[0]["scheduled_fire_time"] == bodies[1]["scheduled_fire_time"] == cfg["scheduled_fire_time"]
    assert sent[0][2]["idempotency-key"] != sent[1][2]["idempotency-key"]
