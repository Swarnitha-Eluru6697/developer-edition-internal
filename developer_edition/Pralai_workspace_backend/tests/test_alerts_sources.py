"""Where alerts learn what happened: run / Flink / Dagster readings, and that a source being down never breaks the sweep."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.core.config import get_settings
from app.services.alerts import sources

P1 = "11111111-1111-1111-1111-111111111111"
P2 = "22222222-2222-2222-2222-222222222222"
RUN_OK, RUN_BAD, RUN_NEW = "aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002", "aaaaaaaa-0000-0000-0000-000000000003"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dbtrunner_delegation_jwt_secret", "x" * 32)
    monkeypatch.setattr(s, "flinksql_delegation_jwt_secret", "y" * 32)
    monkeypatch.setattr(s, "dbtrunner_url", "http://dbt.test")
    monkeypatch.setattr(s, "flinksql_runner_url", "http://flink.test")
    monkeypatch.setattr(s, "dagster_graphql_url", "http://dagster.test/graphql")


def run_json(rid, status, completed="2026-10-29T09:00:00Z", error=None, **kw):
    return {"id": rid, "project_id": P1, "status": status, "completed_at": completed, "started_at": "2026-10-29T08:59:00Z",
            "created_at": "2026-10-29T08:58:00Z", "error_message": error, **kw}


PROJECT = {"id": P1, "name": "customers", "kind": "dbt"}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def go(coro):
    return asyncio.run(coro)


# ── single readings ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_finished_run_becomes_an_observation_with_its_reason():
    o = sources.run_observation(PROJECT, run_json(RUN_BAD, "failed", error="Database Error: boom"))
    assert (o.job_key, o.job_name, o.job_kind, o.category, o.outcome, o.ref_id) == (P1, "customers", "dbt", "run", "failed", RUN_BAD)
    assert o.detail == "Database Error: boom" and o.failure_class is None
    assert o.occurred_at == datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "", None])
def test_a_run_that_has_not_finished_is_not_an_observation_yet(status):
    assert sources.run_observation(PROJECT, run_json(RUN_OK, status)) is None


def test_a_cancelled_run_is_remembered_but_carries_no_reason():
    o = sources.run_observation(PROJECT, run_json(RUN_OK, "cancelled", error="user cancelled"))
    assert o.outcome == "cancelled" and o.detail is None


def test_a_failed_data_test_is_a_quality_failure():
    o = sources.run_observation(PROJECT, run_json(RUN_BAD, "failed", error="Got 5 results, configured to fail if != 0"))
    assert o.failure_class == "quality"
    assert sources.run_observation(PROJECT, run_json(RUN_BAD, "failed", error="Compilation Error")).failure_class is None


def test_a_run_without_any_timestamp_is_skipped_and_the_kind_defaults_to_dbt():
    assert sources.run_observation(PROJECT, {"id": RUN_OK, "status": "failed"}) is None
    o = sources.run_observation({"id": P1, "name": "x"}, run_json(RUN_OK, "succeeded"))
    assert o.job_kind == "dbt"


def test_a_flink_state_is_one_observation_per_status_change():
    job = {"id": "f1", "name": "orders", "status": "failed", "updated_at": "2026-10-29T10:00:00+00:00", "validation_errors": ["bad sink"]}
    o = sources.flink_observation(job)
    assert (o.job_kind, o.category, o.outcome, o.detail) == ("flink", "flink_state", "failed", "bad sink")
    assert o.ref_id == "f1:failed:2026-10-29T10:00:00+00:00"
    assert sources.flink_observation({**job, "status": "running"}).detail is None              # reasons only for a failed job
    assert sources.flink_observation({"id": "f1", "name": "x"}) is None


# ── the dbt Runner ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def runner_handler(projects, runs_by_project, calls):
    def handler(req):
        calls.append((req.url.path, dict(req.url.params), req.headers.get("authorization", "")[:7]))
        if req.url.path.endswith("/projects"):
            return httpx.Response(200, json=projects)
        pid = req.url.path.split("/")[-2]
        return httpx.Response(200, json=runs_by_project.get(pid, []))
    return handler


def test_runs_are_read_per_project_and_a_project_whose_latest_run_is_known_is_skipped():
    calls = []
    projects = [{**PROJECT, "last_run": {"id": RUN_NEW}}, {"id": P2, "name": "scorecard", "kind": "sql", "last_run": {"id": RUN_OK}},
                {"id": "33333333-3333-3333-3333-333333333333", "name": "never-ran", "kind": "python", "last_run": None}]
    runs = {P1: [run_json(RUN_NEW, "failed", error="boom"), run_json(RUN_OK, "succeeded")]}
    res, names = go(sources.collect_runner({RUN_OK}, client=client(runner_handler(projects, runs, calls))))
    assert res.ok and sorted(o.ref_id for o in res.observations) == sorted([RUN_NEW, RUN_OK])
    assert names[P1] == ("customers", "dbt") and names[P2] == ("scorecard", "sql")
    run_calls = [c for c in calls if c[0].endswith("/runs")]
    assert [c[0] for c in run_calls] == [f"/api/v1/projects/{P1}/runs"]            # P2's latest run is known; the third never ran
    assert all(c[2] == "Bearer " for c in calls)                                        # a delegated token, not the user's own
    assert run_calls[0][1] == {"limit": "50"}


def test_when_the_runner_is_down_the_source_says_so_and_returns_nothing():
    def down(req):
        raise httpx.ConnectError("refused", request=req)
    res, names = go(sources.collect_runner(set(), client=client(down)))
    assert not res.ok and res.observations == [] and "could not be reached" in res.detail and names == {}


def test_a_refusing_runner_is_reported_with_its_status():
    res, _ = go(sources.collect_runner(set(), client=client(lambda r: httpx.Response(403, json={"code": "FORBIDDEN"}))))
    assert not res.ok and "403" in res.detail


def test_one_project_failing_to_list_its_runs_does_not_lose_the_others():
    projects = [{**PROJECT, "last_run": {"id": RUN_NEW}}, {"id": P2, "name": "scorecard", "kind": "sql", "last_run": {"id": RUN_BAD}}]

    def handler(req):
        if req.url.path.endswith("/projects"):
            return httpx.Response(200, json=projects)
        return httpx.Response(500) if P2 in req.url.path else httpx.Response(200, json=[run_json(RUN_NEW, "failed", error="x")])
    res, _ = go(sources.collect_runner(set(), client=client(handler)))
    assert [o.ref_id for o in res.observations] == [RUN_NEW] and not res.ok


def test_no_delegation_secret_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(get_settings(), "dbtrunner_delegation_jwt_secret", None)
    res, _ = go(sources.collect_runner(set(), client=client(lambda r: httpx.Response(200, json=[]))))
    assert not res.ok and "SERVICE_AUTH_NOT_CONFIGURED" in res.detail


# ── Flink ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_flink_jobs_become_state_observations_and_a_down_runner_is_reported():
    jobs = [{"id": "f1", "name": "orders", "status": "failed", "updated_at": "2026-10-29T10:00:00Z"},
            {"id": "f2", "name": "stream", "status": "running", "updated_at": "2026-10-29T10:00:00Z"}]
    res = go(sources.collect_flink(client=client(lambda r: httpx.Response(200, json=jobs))))
    assert res.ok and [(o.job_name, o.outcome) for o in res.observations] == [("orders", "failed"), ("stream", "running")]

    def down(req):
        raise httpx.ConnectTimeout("t", request=req)
    bad = go(sources.collect_flink(client=client(down)))
    assert not bad.ok and bad.observations == []


# ── Dagster ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def dagster_handler(failed_runs, logs_by_run):
    def handler(req):
        body = json.loads(req.content)
        if "runsOrError" in body["query"]:
            return httpx.Response(200, json={"data": {"runsOrError": {"__typename": "Runs", "results": failed_runs}}})
        events = logs_by_run.get(body["variables"]["id"], [])
        return httpx.Response(200, json={"data": {"logsForRun": {"__typename": "EventConnection", "events": events}}})
    return handler


def failed_run(rid, project=P1, end=1788000000.0):
    return {"runId": rid, "jobName": "j", "endTime": end, "tags": [{"key": "project_id", "value": project}]}


def step_failure(message):
    return [{"__typename": "ExecutionStepFailureEvent", "error": {"message": message}}]


def test_a_long_dagster_reason_is_kept_whole():
    reason = "Not started: " + "upstream project 'a' failed; " * 30
    logs = {"d1": step_failure("dagster.Failure: " + reason)}
    res = go(sources.collect_dagster(set(), {P1: ("revenue", "dbt")}, client=client(dagster_handler([failed_run("d1")], logs))))
    assert res.observations[0].detail == " ".join(reason.split()) and len(res.observations[0].detail) > 500


def test_a_run_dagster_refused_is_a_blocked_observation_and_other_failures_are_only_remembered():
    runs = [failed_run("d1"), failed_run("d2"), failed_run("d3", project="99999999-9999-9999-9999-999999999999")]
    logs = {"d1": step_failure("dagster.Failure: Not started: upstream project 'customers' failed. It can run again once ..."),
            "d2": step_failure("RuntimeError: runner unreachable")}
    res = go(sources.collect_dagster(set(), {P1: ("revenue", "dbt")}, client=client(dagster_handler(runs, logs))))
    by_ref = {o.ref_id: o for o in res.observations}
    assert by_ref["d1"].category == "blocked" and by_ref["d1"].outcome == "blocked" and by_ref["d1"].job_name == "revenue"
    assert by_ref["d1"].detail.startswith("Not started: upstream project 'customers' failed.")
    assert by_ref["d2"].category == "dagster" and by_ref["d2"].outcome == "failed"
    assert "d3" not in by_ref                                                  # a project the runner does not know


def test_a_dagster_run_already_seen_is_not_read_again():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["query"][:20])
        return dagster_handler([failed_run("d1")], {})(req)
    res = go(sources.collect_dagster({"d1"}, {P1: ("revenue", "dbt")}, client=client(handler)))
    assert res.observations == [] and len(calls) == 1                           # the list only: no log call for a known run


def test_dagster_not_configured_is_quietly_skipped_and_a_down_dagster_is_reported(monkeypatch):
    def down(req):
        raise httpx.ConnectError("refused", request=req)
    bad = go(sources.collect_dagster(set(), {}, client=client(down)))
    assert not bad.ok and "could not be reached" in bad.detail
    monkeypatch.setattr(get_settings(), "dagster_graphql_url", "")
    skipped = go(sources.collect_dagster(set(), {}))
    assert skipped.ok and skipped.detail == "not configured" and skipped.observations == []


def test_the_sweep_identity_holds_only_read_permissions_for_the_runners():
    from app.core.service_tokens import FLINK_SCOPE_JOBS_READ, DBT_SCOPE_RUNS_READ, DBT_SCOPE_PROJECTS_READ, delegated_permissions
    from app.services.alerts.sources import system_principal

    p = system_principal()
    assert p.roles == []                                   # no role names anywhere (permission contract)
    assert not {x for x in p.permissions if x.endswith(":deploy") or x.endswith(":manage")}
    # what a token for each read scope would carry: something at the dbt runner and at the FlinkSQL runner, never nothing
    for scope in (DBT_SCOPE_PROJECTS_READ, DBT_SCOPE_RUNS_READ, FLINK_SCOPE_JOBS_READ):
        assert delegated_permissions(p, scope), scope
