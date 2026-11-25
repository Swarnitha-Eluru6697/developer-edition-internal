"""DG-014: a code-location reload is only "done" when the webserver says so.

Before: `_graphql` POSTed the mutation and ignored the answer, so a webserver that was down, returned an HTTP error,
a GraphQL error, or answered 200 with `PythonError` / `RepositoryLocationNotFound` in the data (how Dagster reports most
failures) all counted as success. The Jobs page then kept showing the old location, with nothing logged and nothing
owed: the reload was never tried again.

Now every answer is checked (HTTP status, GraphQL `errors`, the union type Dagster returns, and whether the location
actually loaded), a failure is retried with bounded exponential backoff, a reload that still fails is remembered and
retried on the next sync pass, and `/metrics` plus an error log say so. The real Manager against a faked webserver.
"""
from __future__ import annotations

import json
import urllib.request

import httpx
import pytest

from pralai_dbt_runner_dags import location_manager as lm
from pralai_dbt_runner_dags.naming import location_name

REAL_CLIENT = httpx.Client


def _ok_workspace():
    return httpx.Response(200, json={"data": {"reloadWorkspace": {"__typename": "Workspace"}}})


def _ok_location(name="repo-a"):
    return httpx.Response(200, json={"data": {"reloadRepositoryLocation": {
        "__typename": "WorkspaceLocationEntry", "name": name, "loadStatus": "LOADED",
        "locationOrLoadError": {"__typename": "RepositoryLocation"}}}})


def _location_union(typename, **extra):
    return httpx.Response(200, json={"data": {"reloadRepositoryLocation": {"__typename": typename, **extra}}})


class Webserver:
    """A scripted webserver: answers are consumed in order, the last one repeats. Records the mutations it saw."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(json.loads(request.content))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture
def web(monkeypatch):
    def install(*answers):
        w = Webserver(*answers)
        monkeypatch.setattr(lm.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(w), **kw))
        return w
    return install


@pytest.fixture
def sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(lm, "_sleep", slept.append)
    return slept


@pytest.fixture(autouse=True)
def fresh_metrics(monkeypatch):
    monkeypatch.setattr(lm, "METRICS", lm.ReloadMetrics())


# ── what counts as success ───────────────────────────────────────────────
def test_a_healthy_answer_is_success(web, sleeps):
    w = web(_ok_location())
    assert lm.Manager().reload_location("repo-a") is True
    assert len(w.seen) == 1 and sleeps == []


@pytest.mark.parametrize("answer,why", [
    (httpx.Response(400, json={"errors": [{"message": "Cannot query field"}]}), "http 4xx"),
    (httpx.Response(500, text="boom"), "http 5xx"),
    (httpx.Response(200, json={"errors": [{"message": "bad"}], "data": None}), "graphql errors"),
    (httpx.Response(200, json={"errors": [{"message": "partial"}], "data": {
        "reloadRepositoryLocation": {"__typename": "WorkspaceLocationEntry"}}}), "errors next to data"),
    (_location_union("PythonError", message="Traceback"), "PythonError in data"),
    (_location_union("UnauthorizedError", message="no"), "UnauthorizedError in data"),
    (_location_union("RepositoryLocationNotFound", message="Location x does not exist."), "unknown location"),
    (_location_union("ReloadNotSupported", message="no"), "reload not supported"),
    (httpx.Response(200, json={"data": {"reloadRepositoryLocation": {
        "__typename": "WorkspaceLocationEntry", "name": "repo-a", "loadStatus": "LOADED",
        "locationOrLoadError": {"__typename": "PythonError", "message": "cannot reach grpc"}}}}), "location failed to load"),
    (httpx.Response(200, text="<html>not json</html>"), "not json"),
    (httpx.Response(200, json={"data": None}), "no data"),
    (httpx.Response(200, json={}), "empty"),
    (httpx.ConnectError("refused"), "webserver down"),
    (httpx.ReadTimeout("slow"), "timeout"),
])
def test_anything_but_a_loaded_location_is_a_failure(web, sleeps, answer, why):
    web(answer)
    assert lm.Manager().reload_location("repo-a") is False, why


def test_the_workspace_reload_checks_its_answer_too(web, sleeps):
    web(httpx.Response(200, json={"data": {"reloadWorkspace": {"__typename": "PythonError", "message": "x"}}}))
    assert lm.Manager().reload_workspace() is False
    web(_ok_workspace())
    assert lm.Manager().reload_workspace() is True


# ── retry with bounded exponential backoff ───────────────────────────────
def test_a_transient_failure_is_retried_until_it_works(web, sleeps):
    w = web(httpx.Response(503), httpx.ConnectError("refused"), _ok_location())
    assert lm.Manager().reload_location("repo-a") is True
    assert len(w.seen) == 3 and sleeps == [1.0, 2.0]


def test_retries_are_bounded_and_the_backoff_is_capped(web, sleeps, monkeypatch):
    monkeypatch.setattr(lm, "RELOAD_ATTEMPTS", 6)
    w = web(httpx.Response(503))
    assert lm.Manager().reload_location("repo-a") is False
    assert len(w.seen) == 6 and sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]        # doubles, never above RELOAD_BACKOFF_MAX


def test_a_request_that_can_never_succeed_is_not_retried(web, sleeps):
    w = web(httpx.Response(400, json={"errors": [{"message": "Cannot query field"}]}))
    assert lm.Manager().reload_location("repo-a") is False
    assert len(w.seen) == 1 and sleeps == []


def test_retrying_stops_at_the_deadline(web, sleeps, monkeypatch):
    clock = iter([0.0, 0.0, 70.0, 70.0, 140.0, 140.0])
    monkeypatch.setattr(lm.time, "monotonic", lambda: next(clock, 999.0))
    w = web(httpx.Response(503))
    assert lm.Manager().reload_location("repo-a") is False
    assert len(w.seen) < lm.RELOAD_ATTEMPTS


# ── a failed reload is owed, not forgotten ───────────────────────────────
class _AliveProc:
    def poll(self):
        return None


def _manager(monkeypatch, projects_by_pass):
    """A Manager whose process side is faked: only the reload behaviour is under test."""
    m = lm.Manager()
    passes = iter(projects_by_pass)
    monkeypatch.setattr(lm, "fetch_all_projects", lambda attempts=1: next(passes))
    monkeypatch.setattr(m, "start", lambda loc, fp, port=None: (m.servers.__setitem__(
        loc, lm.Server(loc, 4001, _AliveProc(), fp)), True)[1])

    def restart(loc, fp):
        m.servers[loc] = lm.Server(loc, 4001, _AliveProc(), fp)
        return True
    monkeypatch.setattr(m, "restart", restart)
    monkeypatch.setattr(m, "stop", lambda loc: m.servers.pop(loc, None))
    monkeypatch.setattr(m, "write_workspace", lambda: None)
    return m


def _project(name="p", repo="a"):
    return {"id": name, "name": name, "git_url": f"https://github.com/acme/{repo}.git", "git_ref": "main",
            "project_path": "", "schedule_cron": None, "updated_at": "2026-01-01T00:00:00+00:00"}


def test_a_failed_workspace_reload_is_retried_on_the_next_pass(web, sleeps, monkeypatch):
    m = _manager(monkeypatch, [[_project()], [_project()]])
    failing = web(httpx.Response(503))
    m.sync()                                                    # a new location: workspace rewritten, reload fails
    assert m.pending_workspace is True and lm.METRICS.pending("workspace") == 1
    assert len(failing.seen) == lm.RELOAD_ATTEMPTS
    healthy = web(_ok_workspace())
    m.sync()                                                    # membership unchanged: the owed reload runs by itself
    assert m.pending_workspace is False and lm.METRICS.pending("workspace") == 0
    assert [q["query"] for q in healthy.seen] == [lm._WORKSPACE_MUTATION]


def test_a_failed_location_reload_is_retried_on_the_next_pass(web, sleeps, monkeypatch):
    first, changed = [_project()], [_project(), _project(name="p2")]       # same repo: a project was added to it
    m = _manager(monkeypatch, [first, changed, changed])
    web(_ok_workspace())
    m.sync()                                                    # location started, workspace reload ok
    failing = web(httpx.Response(503))
    m.sync()                                                    # the repo's project set changed: restart + reload fails
    loc = location_name(first[0])
    assert m.pending_locations == {loc} and lm.METRICS.pending("location") == 1
    assert len(failing.seen) == lm.RELOAD_ATTEMPTS
    healthy = web(_ok_location(loc))
    m.sync()                                                    # nothing changed: only the owed reload runs
    assert m.pending_locations == set() and lm.METRICS.pending("location") == 0
    assert len(healthy.seen) == 1 and healthy.seen[0]["variables"] == {"n": loc}


def test_a_location_the_webserver_has_not_heard_of_gets_a_workspace_reload_next_pass(web, sleeps, monkeypatch):
    m = _manager(monkeypatch, [[_project()], [_project(), _project(name="p2")], [_project(), _project(name="p2")]])
    web(_ok_workspace())
    m.sync()
    web(_location_union("RepositoryLocationNotFound", message="does not exist"))
    m.sync()
    assert m.pending_workspace is True and m.pending_locations == set()
    healthy = web(_ok_workspace())
    m.sync()
    assert [q["query"] for q in healthy.seen] == [lm._WORKSPACE_MUTATION] and not m.pending_workspace


def test_an_unknown_location_escalates_to_a_workspace_reload(web, sleeps):
    m = lm.Manager()
    w = web(_location_union("RepositoryLocationNotFound", message="does not exist"), _ok_workspace())
    assert m.reload_location("repo-a") is False
    assert m.pending_workspace is True and "repo-a" not in m.pending_locations
    assert len(w.seen) == 1                                     # not retried: it can never work until the workspace reloads


def test_one_exhausted_reload_ends_the_pass_instead_of_stalling_it(web, sleeps):
    m = lm.Manager()
    m.pending_locations = {"a", "b", "c"}
    w = web(httpx.ConnectError("refused"))
    m.flush_pending_reloads()
    assert len(w.seen) == lm.RELOAD_ATTEMPTS                    # one location's attempts, not three times as many
    assert m.pending_locations == {"a", "b", "c"}


def test_a_workspace_reload_makes_every_owed_location_reload_unnecessary(web, sleeps):
    m = lm.Manager()
    m.pending_workspace, m.pending_locations = True, {"a", "b"}
    w = web(_ok_workspace())
    m.flush_pending_reloads()
    assert len(w.seen) == 1 and "reloadWorkspace" in w.seen[0]["query"]
    assert not m.pending_workspace and m.pending_locations == set()


# ── the metric and the log ───────────────────────────────────────────────
def test_failures_and_successes_are_counted(web, sleeps):
    web(httpx.Response(503))
    lm.Manager().reload_location("repo-a")
    web(_ok_location())
    lm.Manager().reload_location("repo-a")
    text = lm.METRICS.render()
    assert 'dagster_location_reload_total{kind="location",result="failure"} 1' in text
    assert 'dagster_location_reload_total{kind="location",result="success"} 1' in text
    assert 'dagster_location_reload_last_success_timestamp_seconds{kind="location"}' in text


def test_the_pending_gauge_reflects_what_is_still_owed(web, sleeps):
    m = lm.Manager()
    web(httpx.Response(503))
    m.reload_location("repo-a")
    assert 'dagster_location_reload_pending{kind="location"} 1' in lm.METRICS.render()
    web(_ok_location())
    m.flush_pending_reloads()
    assert 'dagster_location_reload_pending{kind="location"} 0' in lm.METRICS.render()


def test_a_failed_reload_is_logged_as_an_error(web, sleeps, capsys):
    web(_location_union("PythonError", message="Traceback (most recent call last)"))
    lm.Manager().reload_location("repo-a")
    import re
    out = capsys.readouterr().out
    assert re.search(r"\[error\s*\]\s+location_manager\.reload_failed", out) and "PythonError" in out


def test_the_metrics_endpoint_serves_the_text_format():
    lm.METRICS.record("workspace", False)
    server = lm.start_metrics_server(0)
    try:
        port = server.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
        assert 'dagster_location_reload_total{kind="workspace",result="failure"} 1' in body
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5)
        assert e.value.code == 404
    finally:
        server.shutdown()
