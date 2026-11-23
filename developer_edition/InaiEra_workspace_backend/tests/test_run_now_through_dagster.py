"""Run now through Dagster: the dependency rule lives in Dagster, so a run started by hand must go through the project's Dagster job.

With `DAGSTER_GRAPHQL_URL` set, "Run now" launches the project's Dagster job, waits for its submit step and answers with the runner's run;
when the dependency rule refuses the run, the reason comes back as a 409 and nothing reaches the runner. Without it, the direct call is
unchanged. Fake Dagster (GraphQL) and fake runner on one mock transport; the real route, token delegation and error envelope.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.core.config import get_settings

DBT_SECRET = "workspace-to-dbtrunner-delegation-secret"
PID = str(uuid.uuid4())
RUNNER_RUN = str(uuid.uuid4())
ROLES = ["data_engineer"]
DAGSTER = "http://dagster.test/graphql"


class Fake:
    """Dagster's GraphQL API and the dbt runner behind one transport; records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.jobs = [{"name": "customers", "tags": [{"key": "project_id", "value": PID}, {"key": "project_kind", "value": "dbt"}]},
                     {"name": "other", "tags": [{"key": "project_id", "value": str(uuid.uuid4())}]}]
        self.entries = None
        self.run_status = "SUCCESS"
        self.run_tags = [{"key": "inaiera/dbt_run_id", "value": RUNNER_RUN}]
        self.failure = {"message": "", "className": ""}
        self.launch_result = None
        self.statuses = []                       # statuses answered in turn before the final one
        self.dagster_down = False
        self.project_status = 200
        self.launched: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.host == "dagster.test":
            if self.dagster_down:
                raise httpx.ConnectError("down")
            return self._graphql(json.loads(req.content))
        path = req.url.path
        if path == f"/api/v1/projects/{PID}":
            return httpx.Response(self.project_status, json={"id": PID, "name": "customers"} if self.project_status == 200 else {"detail": "no"})
        if path == f"/api/v1/projects/{PID}/runs/{RUNNER_RUN}":
            return httpx.Response(200, json={"id": RUNNER_RUN, "status": "queued", "trigger": "api"})
        if path == f"/api/v1/projects/{PID}/run":
            return httpx.Response(202, json={"id": RUNNER_RUN, "status": "queued", "trigger": "manual"})
        return httpx.Response(404)

    def _graphql(self, body: dict) -> httpx.Response:
        q = body["query"]
        if "workspaceOrError" in q:
            entries = self.entries if self.entries is not None else [{"name": "repo-loc", "loadStatus": "LOADED", "locationOrLoadError": {
                "__typename": "RepositoryLocation", "name": "repo-loc", "repositories": [{"name": "__repository__", "pipelines": self.jobs}]}}]
            return httpx.Response(200, json={"data": {"workspaceOrError": {"__typename": "Workspace", "locationEntries": entries}}})
        if "launchRun" in q:
            self.launched.append(body["variables"]["params"])
            return httpx.Response(200, json={"data": {"launchRun": self.launch_result or {"__typename": "LaunchRunSuccess", "run": {"runId": "drun-1"}}}})
        if "runOrError" in q:
            status = self.statuses.pop(0) if self.statuses else self.run_status
            return httpx.Response(200, json={"data": {"runOrError": {"__typename": "Run", "runId": "drun-1", "status": status, "tags": self.run_tags}}})
        if "logsForRun" in q:
            events = [{"__typename": "ExecutionStepFailureEvent", "error": self.failure}] if self.failure["message"] else []
            return httpx.Response(200, json={"data": {"logsForRun": {"__typename": "EventConnection", "events": events}}})
        return httpx.Response(400)

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


@pytest.fixture
def fake(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "dbtrunner_delegation_jwt_secret", DBT_SECRET)
    monkeypatch.setattr(s, "dbtrunner_url", "http://dbt.test")
    monkeypatch.setattr(s, "dagster_graphql_url", DAGSTER)
    monkeypatch.setattr(s, "dagster_run_wait_seconds", 2.0)
    import app.services.dagster_runs as dr
    monkeypatch.setattr(dr, "POLL_SECONDS", 0.01)
    f = Fake()
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(f.handler), **kw))
    return f


def _run_now(client, gateway_headers, pid=PID):
    return client.post(f"/api/v1/dbt/projects/{pid}/run", headers=gateway_headers(user_name="alice", roles=ROLES), json={})


def test_run_now_launches_the_projects_dagster_job_and_returns_the_runners_run(client, gateway_headers, fake):
    r = _run_now(client, gateway_headers)
    assert r.status_code == 202, r.text
    assert r.json()["id"] == RUNNER_RUN and r.json()["trigger"] == "api"
    [params] = fake.launched
    assert params["selector"] == {"repositoryLocationName": "repo-loc", "repositoryName": "__repository__", "jobName": "customers"}
    assert params["runConfigData"] == {}
    [tag] = params["executionMetadata"]["tags"]
    assert tag["key"] == "inaiera/triggered_by" and str(uuid.UUID(tag["value"]))          # who clicked: the user's stable id
    assert not [r for r in fake.to("dbt.test") if r.url.path.endswith("/run")]      # the runner was NOT called directly


def test_the_job_is_found_by_the_projects_tag_not_by_name_or_position(client, gateway_headers, fake):
    fake.jobs = [{"name": "z_first", "tags": []}, {"name": "a_mine", "tags": [{"key": "project_id", "value": PID}]}]
    assert _run_now(client, gateway_headers).status_code == 202
    assert fake.launched[0]["selector"]["jobName"] == "a_mine"


def test_a_refused_run_comes_back_as_a_409_with_the_reason_and_nothing_reaches_the_runner(client, gateway_headers, fake):
    reason = "Not started: upstream project 'a' last succeeded 14 minutes ago, more than the 10 minutes allowed. It can run again once every upstream project has a recent successful run."
    fake.run_status = "FAILURE"
    fake.failure = {"message": f"dagster._core.definitions.events.Failure: {reason}\n", "className": "Failure"}
    r = _run_now(client, gateway_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "UPSTREAM_NOT_READY" and r.json()["detail"]["message"] == reason
    assert r.json()["detail"]["details"] == {"dagster_run_id": "drun-1"}
    assert not [x for x in fake.to("dbt.test") if x.url.path.endswith(("/run", f"/runs/{RUNNER_RUN}"))]


def test_a_run_that_failed_for_another_reason_is_a_502_not_a_refusal(client, gateway_headers, fake):
    fake.run_status = "FAILURE"
    fake.failure = {"message": "httpx.ConnectError: the runner is down\n", "className": "ConnectError"}
    r = _run_now(client, gateway_headers)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "DAGSTER_RUN_FAILED"
    assert "the runner is down" in r.json()["detail"]["message"]


def test_it_waits_for_the_submit_step(client, gateway_headers, fake):
    fake.statuses = ["QUEUED", "STARTING", "STARTED"]
    r = _run_now(client, gateway_headers)
    assert r.status_code == 202 and r.json()["id"] == RUNNER_RUN


def test_a_run_dagster_never_hands_over_is_a_504(client, gateway_headers, fake):
    fake.run_status = "STARTED"
    r = _run_now(client, gateway_headers)
    assert r.status_code == 504 and r.json()["detail"]["code"] == "DAGSTER_RUN_TIMEOUT"


def test_dagster_being_down_is_a_503_and_never_a_silent_bypass(client, gateway_headers, fake):
    fake.dagster_down = True
    r = _run_now(client, gateway_headers)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "DAGSTER_UNAVAILABLE"
    assert not [x for x in fake.to("dbt.test") if x.url.path.endswith("/run")]


def test_a_project_without_a_job_yet_says_so(client, gateway_headers, fake):
    fake.jobs = [{"name": "other", "tags": []}]
    r = _run_now(client, gateway_headers)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "DAGSTER_JOB_NOT_FOUND" and "within about a minute" in r.json()["detail"]["message"]


def test_a_repository_still_loading_is_mentioned(client, gateway_headers, fake):
    fake.entries = [{"name": "repo-loc", "loadStatus": "LOADING", "locationOrLoadError": {"__typename": "PythonError"}}]
    r = _run_now(client, gateway_headers)
    assert r.status_code == 503 and "still loading" in r.json()["detail"]["message"]


def test_the_generic_job_takes_the_project_in_its_launch_config(client, gateway_headers, fake):
    fake.jobs = [{"name": "dbt_scheduled_run", "tags": []}]
    assert _run_now(client, gateway_headers).status_code == 202
    assert fake.launched[0]["selector"]["jobName"] == "dbt_scheduled_run"
    assert fake.launched[0]["runConfigData"] == {"ops": {"submit_run_op": {"config": {"project_id": PID}}}}


def test_a_launch_dagster_refuses_is_a_502(client, gateway_headers, fake):
    fake.launch_result = {"__typename": "RunConfigValidationInvalid", "message": "bad config"}
    r = _run_now(client, gateway_headers)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "DAGSTER_LAUNCH_FAILED" and "bad config" in r.json()["detail"]["message"]


def test_the_caller_must_be_able_to_see_the_project_first(client, gateway_headers, fake):
    fake.project_status = 404
    r = _run_now(client, gateway_headers)
    assert r.status_code == 404 and fake.launched == [] and fake.to("dagster.test") == []


def test_the_visibility_check_and_the_run_lookup_use_delegated_tokens_with_the_right_scopes(client, gateway_headers, fake):
    from jose import jwt
    _run_now(client, gateway_headers)
    scopes = []
    for req in fake.to("dbt.test"):
        token = req.headers["Authorization"].removeprefix("Bearer ")
        scopes.append(jwt.decode(token, DBT_SECRET, algorithms=["HS256"], audience="dbtrunner")["scope"])
    assert scopes == ["dbt.projects.read", "dbt.runs.read"]


def test_without_dagster_configured_run_now_calls_the_runner_directly_as_before(client, gateway_headers, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "dagster_graphql_url", "")
    r = _run_now(client, gateway_headers)
    assert r.status_code == 202 and r.json()["trigger"] == "manual"
    assert fake.to("dagster.test") == []
    assert [x.method for x in fake.to("dbt.test")] == ["POST"]
