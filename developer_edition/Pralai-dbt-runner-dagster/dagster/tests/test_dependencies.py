"""Job dependencies in Dagster: a job runs only when every job directly above it has a recent success.

Real Dagster (an in-memory instance), the real jobs, op, monitor and run status sensors; only the dbt Runner's HTTP side is faked.

  * the rule: the upstream's latest result must be a success no older than its schedule interval + 5 minutes (at least 10); a failure
    anywhere above blocks; an upstream that never ran blocks; several upstreams are each judged by their own schedule
  * the start check in `submit_run_op`: a refused run never reaches the Runner, fails with the reason, and records the project as failed
  * the run status sensors: they record each project's real result (the `dbt_run_result` run), never a cancelled run
  * the whole flow: a finished dbt run -> monitor -> result run -> status sensor -> the next job's start check
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from dagster import (
    DagsterEventType,
    DagsterInstance,
    DagsterRunStatus,
    build_run_status_sensor_context,
    build_sensor_context,
)

from pralai_dbt_runner_dags import dependencies as dep
from pralai_dbt_runner_dags import monitoring
from pralai_dbt_runner_dags.dependencies import FAILED, SUCCEEDED, Status, evaluate, reason_text, valid_for
from pralai_dbt_runner_dags.jobs.dbt_run_job import dbt_run_result_job, make_project_job
from pralai_dbt_runner_dags.sensors.dependency_sensor import make_dependency_status_sensors
from pralai_dbt_runner_dags.sensors.run_monitor import make_run_monitor_sensor

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)          # a Friday
MIN = timedelta(minutes=1)
LOCATION = "acme-repo"


def _p(pid, ups=(), cron=None, enabled=True):
    return {"id": pid, "name": pid, "upstream_project_ids": list(ups), "schedule_cron": cron, "schedule_enabled": enabled and cron is not None}


def _ok(minutes_ago):
    return Status(SUCCEEDED, NOW - minutes_ago * MIN)


def _bad(minutes_ago=0):
    return Status(FAILED, NOW - minutes_ago * MIN)


def _why(project_id, projects, statuses):
    return [(b.project_id, b.reason) for b in evaluate(project_id, projects, statuses, NOW)]


# ── how long a success stays valid ───────────────────────────────────────────────────────────────────────────────────────────
def test_the_window_is_the_schedule_interval_plus_five_minutes_and_never_under_ten():
    assert valid_for("*/5 * * * *", True, NOW) == 10 * MIN
    assert valid_for("*/10 * * * *", True, NOW) == 15 * MIN
    assert valid_for("*/30 * * * *", True, NOW) == 35 * MIN
    assert valid_for("0 * * * *", True, NOW) == 65 * MIN
    assert valid_for("0 1 * * *", True, NOW) == 24 * 60 * MIN + 5 * MIN
    assert valid_for("* * * * *", True, NOW) == 10 * MIN
    assert valid_for("* * * * * */30", True, NOW) == 10 * MIN                    # a seconds field (Dagster's form)


def test_without_a_usable_schedule_the_window_is_ten_minutes():
    assert valid_for(None, False, NOW) == 10 * MIN
    assert valid_for("0 * * * *", False, NOW) == 10 * MIN                        # a schedule that is switched off
    assert valid_for("not a cron", True, NOW) == 10 * MIN


def test_an_uneven_schedule_uses_its_longest_gap():
    assert valid_for("0 9 * * 1-5", True, NOW) == 72 * 60 * MIN + 5 * MIN        # Friday to Monday: a weekend is not "stale"
    monthly = valid_for("0 0 1 * *", True, NOW)
    assert timedelta(days=28) <= monthly <= timedelta(days=31, minutes=5)


# ── the rule ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_upstream_success_inside_the_window_lets_the_job_run():
    projects = [_p("a", cron="*/5 * * * *"), _p("b", ["a"])]
    assert _why("b", projects, {"a": _ok(3)}) == []
    assert _why("b", projects, {"a": _ok(9)}) == []


def test_upstream_success_older_than_the_window_is_stale():
    projects = [_p("a", cron="*/5 * * * *"), _p("b", ["a"])]
    [blocker] = evaluate("b", projects, {"a": _ok(14)}, NOW)
    assert (blocker.reason, blocker.age_minutes, blocker.valid_minutes) == ("stale", 14, 10)
    assert _why("b", projects, {"a": _ok(11)}) == [("a", "stale")]


def test_upstream_failure_blocks_however_recent_an_older_success_was():
    projects = [_p("a", cron="*/5 * * * *"), _p("b", ["a"])]
    assert _why("b", projects, {"a": _bad(1)}) == [("a", "failed")]


def test_a_missing_success_state_blocks():
    projects = [_p("a", cron="*/5 * * * *"), _p("b", ["a"])]
    assert _why("b", projects, {}) == [("a", "never_ran")]


def test_slower_schedules_get_a_longer_window():
    for cron, fresh, stale in (("0 * * * *", 50, 70), ("0 1 * * *", 20 * 60, 25 * 60), (None, 9, 11)):
        projects = [_p("a", cron=cron), _p("b", ["a"])]
        assert _why("b", projects, {"a": _ok(fresh)}) == [], (cron, fresh)
        assert _why("b", projects, {"a": _ok(stale)}) == [("a", "stale")], (cron, stale)


def test_each_upstream_is_judged_by_its_own_schedule_and_all_must_pass():
    projects = [_p("a", cron="*/5 * * * *"), _p("b", cron="*/10 * * * *"), _p("c", ["a", "b"])]
    assert _why("c", projects, {"a": _ok(3), "b": _ok(12)}) == []                       # 12 minutes is fine for b (15), not for a (10)
    assert _why("c", projects, {"a": _ok(12), "b": _ok(12)}) == [("a", "stale")]
    assert _why("c", projects, {"a": _ok(3), "b": _ok(20)}) == [("b", "stale")]
    assert _why("c", projects, {"a": _bad(), "b": _ok(20)}) == [("a", "failed"), ("b", "stale")]
    assert _why("c", projects, {"a": _ok(3)}) == [("b", "never_ran")]


def test_only_the_direct_upstream_must_be_recent_but_a_failure_anywhere_above_blocks():
    projects = [_p("a"), _p("b", ["a"]), _p("c", ["b"])]
    assert _why("c", projects, {"a": _ok(120), "b": _ok(3)}) == []                      # a is old, but c is not directly below a
    assert _why("b", projects, {"a": _ok(120)}) == [("a", "stale")]
    assert _why("c", projects, {"a": _bad(), "b": _ok(3)}) == [("a", "failed")]         # a failed: c is blocked even before b runs again
    assert _why("c", projects, {"a": _bad(), "b": _bad()}) == [("b", "failed"), ("a", "failed")]    # nearest first


def test_the_reason_says_what_is_in_the_way():
    projects = [_p("a", cron="*/5 * * * *"), _p("b", cron="*/10 * * * *"), _p("c", ["a", "b"]), _p("d")]
    text = reason_text(evaluate("c", projects, {"a": _ok(30), "b": _bad()}, NOW))
    assert text == ("Not started: upstream project 'b' failed; upstream project 'a' last succeeded 30 minutes ago, more than the "
                    "10 minutes allowed. It can run again once every upstream project has a recent successful run.") or \
        text == ("Not started: upstream project 'a' last succeeded 30 minutes ago, more than the 10 minutes allowed; upstream project "
                 "'b' failed. It can run again once every upstream project has a recent successful run.")
    assert "has not run yet" in reason_text(evaluate("c", projects, {}, NOW))
    assert dep._span(1) == "1 minute" and dep._span(119) == "119 minutes" and dep._span(1445) == "24 hours" and dep._span(4320) == "3 days"


def test_a_project_with_nothing_above_it_is_never_blocked():
    assert _why("a", [_p("a"), _p("b", ["a"])], {}) == []
    assert _why("unknown", [_p("a")], {}) == []


# ── the recorded status ────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def instance():
    return DagsterInstance.ephemeral()


def test_a_status_is_stored_and_read_back_per_project(instance):
    assert dep.record_status(instance, "a", SUCCEEDED, NOW, run_id="r1")
    assert dep.record_status(instance, "b", FAILED, NOW, detail="why")
    got = dep.read_statuses(instance, ["a", "b", "c"])
    assert got["a"] == Status(SUCCEEDED, NOW, "r1") and got["b"] == Status(FAILED, NOW, "", "why") and "c" not in got


def test_an_older_result_never_replaces_a_newer_one(instance):
    dep.record_status(instance, "a", FAILED, NOW)
    assert not dep.record_status(instance, "a", SUCCEEDED, NOW - 5 * MIN)
    assert dep.read_statuses(instance, ["a"])["a"].status == FAILED
    assert dep.record_status(instance, "a", SUCCEEDED, NOW + MIN)
    assert dep.read_statuses(instance, ["a"])["a"].status == SUCCEEDED


def test_an_unreadable_status_counts_as_no_result(instance):
    instance.run_storage.set_cursor_values({dep.STATE_PREFIX + "a": "not json"})
    assert dep.read_statuses(instance, ["a"]) == {}


# ── a fake Runner that knows the links ─────────────────────────────────────────────────────────────────────────────────────
class FakeRunner:
    base_url = "http://runner.test"

    def __init__(self, projects):
        self.projects = {p["id"]: p for p in projects}
        self.runs: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._n = 0

    def get_project(self, project_id):
        self.calls.append(("get_project", project_id))
        return dict(self.projects[project_id])

    def list_projects(self):
        self.calls.append(("list_projects",))
        return [dict(p) for p in self.projects.values()]

    def trigger_run(self, project_id, **kw):
        self._n += 1
        rid = f"run-{self._n}"
        self.runs[rid] = {"id": rid, "project": project_id, "status": "running"}
        self.calls.append(("trigger", project_id, kw))
        return {"id": rid, "status": "queued", "git_sha": kw.get("git_sha")}

    def get_run(self, run_id):
        return dict(self.runs[run_id])

    def cancel_run(self, run_id, **_kw):
        self.calls.append(("cancel", run_id))
        return self.runs.get(run_id)

    def cancel_run_by_key(self, *_a, **_k):
        return None

    def finish(self, run_id, status="succeeded"):
        self.runs[run_id].update(status=status, duration_ms=1000, models_total=1, models_succeeded=1)

    def triggers(self, project_id=None):
        return [c for c in self.calls if c[0] == "trigger" and (project_id is None or c[1] == project_id)]


def _projects():
    return [_p("a", cron="*/5 * * * *"), _p("b", ["a"], cron="*/12 * * * *"), _p("c", ["b"])]


@pytest.fixture
def runner():
    return FakeRunner(_projects())


def _submit(instance, runner, pid, tags=None):
    job = make_project_job(pid, pid, pid, LOCATION)
    return job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=False, tags=tags)


def _failure_text(result) -> str:
    return " ".join(str(e.event_specific_data.error.message) for e in result.all_events if e.event_type == DagsterEventType.STEP_FAILURE)


# ── the start check, inside the real job ───────────────────────────────────────────────────────────────────────────────────
def test_a_job_with_a_recent_upstream_success_runs(instance, runner):
    dep.record_status(instance, "a", SUCCEEDED, datetime.now(timezone.utc) - 3 * MIN)
    result = _submit(instance, runner, "b")
    assert result.success and len(runner.triggers("b")) == 1


def test_a_stale_upstream_success_fails_the_run_without_reaching_the_runner(instance, runner):
    dep.record_status(instance, "a", SUCCEEDED, datetime.now(timezone.utc) - 14 * MIN)
    result = _submit(instance, runner, "b")
    assert not result.success and runner.triggers() == []
    assert "upstream project 'a' last succeeded 14 minutes ago, more than the 10 minutes allowed" in _failure_text(result)
    assert instance.get_run_by_id(result.run_id).status == DagsterRunStatus.FAILURE


def test_a_failed_upstream_fails_the_run(instance, runner):
    dep.record_status(instance, "a", FAILED, datetime.now(timezone.utc))
    result = _submit(instance, runner, "b")
    assert not result.success and runner.triggers() == []
    assert "upstream project 'a' failed" in _failure_text(result)


def test_an_upstream_with_no_recorded_success_fails_the_run(instance, runner):
    result = _submit(instance, runner, "b")
    assert not result.success and runner.triggers() == []
    assert "upstream project 'a' has not run yet" in _failure_text(result)


def test_a_manual_launch_is_checked_like_every_other_run(instance, runner):
    """The Dagster UI, the Workspace's Run now and the cron sensor all start the same job: there is no way around the check."""
    dep.record_status(instance, "a", SUCCEEDED, datetime.now(timezone.utc) - 14 * MIN)
    manual = _submit(instance, runner, "b", tags={monitoring.TAG_TRIGGERED_BY: "someone"})
    assert not manual.success and runner.triggers() == []


def test_a_refused_job_is_recorded_as_failed_so_the_job_below_it_is_refused_too(instance, runner):
    dep.record_status(instance, "a", FAILED, datetime.now(timezone.utc))
    _submit(instance, runner, "b")                                                # b is refused ...
    assert dep.read_statuses(instance, ["b"])["b"].status == FAILED
    result = _submit(instance, runner, "c")                                       # ... so c is refused, with b named
    assert not result.success and runner.triggers() == []
    assert "upstream project 'b' failed" in _failure_text(result)


def test_the_job_runs_again_once_the_upstream_succeeds_again(instance, runner):
    dep.record_status(instance, "a", FAILED, datetime.now(timezone.utc) - 2 * MIN)
    assert not _submit(instance, runner, "b").success
    dep.record_status(instance, "a", SUCCEEDED, datetime.now(timezone.utc))
    assert _submit(instance, runner, "b").success and len(runner.triggers("b")) == 1


def test_a_job_with_nothing_above_it_asks_the_runner_for_nothing_extra(instance, runner):
    result = _submit(instance, runner, "a")
    assert result.success and ("list_projects",) not in runner.calls


def _submit_with(instance, runner, pid, config, tags=None):
    job = make_project_job(pid, pid, pid, LOCATION)
    run_config = {"ops": {"submit_run_op": {"config": {"project_id": pid, **config}}}}
    return job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=False, tags=tags, run_config=run_config)


def test_run_now_is_a_manual_run_and_names_who_clicked(instance, runner):
    """The Trigger column says manual when you click Run now (never api), and who did."""
    _submit(instance, runner, "a", tags={monitoring.TAG_TRIGGERED_BY: "someone"})
    assert runner.triggers("a")[0][2]["trigger"] == "manual" and runner.triggers("a")[0][2]["triggered_by"] == "someone"


def test_a_launch_in_the_dagster_ui_is_manual_too_without_a_name(instance, runner):
    _submit(instance, runner, "a")
    assert runner.triggers("a")[0][2]["trigger"] == "manual" and "triggered_by" not in runner.triggers("a")[0][2]


def test_a_cron_fire_is_left_as_scheduled(instance, runner):
    _submit_with(instance, runner, "a", {"scheduled_fire_time": "2026-10-23T12:00:00+00:00"})
    assert "trigger" not in runner.triggers("a")[0][2] and "triggered_by" not in runner.triggers("a")[0][2]


def test_a_new_commit_is_left_as_scheduled(instance, runner):
    _submit_with(instance, runner, "a", {"git_sha": "a" * 40})
    assert "trigger" not in runner.triggers("a")[0][2]


def test_a_cron_fire_is_scheduled_even_if_someone_tagged_the_run(instance, runner):
    _submit_with(instance, runner, "a", {"scheduled_fire_time": "2026-10-23T12:00:00+00:00"}, tags={monitoring.TAG_TRIGGERED_BY: "someone"})
    assert "trigger" not in runner.triggers("a")[0][2]


def test_the_client_sends_the_trigger_and_the_name_to_the_runner(monkeypatch):
    import httpx

    from pralai_dbt_runner_dags.resources import dbt_runner_client as drc

    seen = []
    real = httpx.Client
    monkeypatch.setattr(drc.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(
        lambda req: (seen.append(dict(req.url.params)), httpx.Response(202, json={"id": "r1"}))[1]), **kw))
    client = drc.DbtRunnerClient(base_url="http://runner.test", scheduler_jwt_secret="s" * 32, org_id="acme")
    client.trigger_run("p1", trigger="manual", triggered_by="someone")
    client.trigger_run("p1", trigger="manual")
    client.trigger_run("p1")
    assert seen == [{"trigger": "manual", "triggered_by": "someone"}, {"trigger": "manual"}, {}]


def test_the_submitting_run_carries_the_project_id_for_the_result_run(instance, runner):
    run = instance.get_run_by_id(_submit(instance, runner, "a").run_id)
    assert run.tags["project_id"] == "a"


def test_a_run_of_the_generic_job_is_tagged_with_its_project_too(instance, runner):
    """The generic job takes the project in its launch config, so its runs carry no project tag until the op adds it: without it the
    result run, and so the status sensors, could not tell which project the outcome belongs to."""
    from pralai_dbt_runner_dags.jobs.dbt_run_job import dbt_scheduled_run_job
    result = dbt_scheduled_run_job.execute_in_process(
        instance=instance, resources={"dbt_runner": runner},
        run_config={"ops": {"submit_run_op": {"config": {"project_id": "a"}}}})
    assert instance.get_run_by_id(result.run_id).tags["project_id"] == "a"


def test_a_refusal_starts_no_other_run(instance, runner):
    dep.record_status(instance, "a", FAILED, datetime.now(timezone.utc))
    _submit(instance, runner, "b")
    assert len(instance.get_runs()) == 1                                           # only the refused run itself: nothing duplicated


# ── the run status sensors ─────────────────────────────────────────────────────────────────────────────────────────────────
def _sensors():
    return {s.name: s for s in make_dependency_status_sensors(LOCATION, dbt_run_result_job)}


def _result_run(instance, runner, pid, outcome, submitted_by="", tags=None):
    """A `dbt_run_result` run for `pid`, as the monitor launches it, ending with the Runner's `outcome`."""
    rid = f"run-x{len(runner.runs) + 1}"
    runner.runs[rid] = {"id": rid, "project": pid, "status": outcome, "duration_ms": 1, "models_total": 1, "models_succeeded": 1}
    result = dbt_run_result_job.execute_in_process(
        instance=instance, resources={"dbt_runner": runner}, raise_on_error=False,
        run_config={"ops": {"report_run_result_op": {"config": {"run_id": rid, "submitted_by": submitted_by}}}},
        tags={"project_id": pid, monitoring.TAG_RUNNER_RUN: rid, monitoring.TAG_SUBMITTED_BY: submitted_by, **(tags or {})})
    return result.run_id


def _fire(instance, run_id):
    run = instance.get_run_by_id(run_id)
    kind = DagsterEventType.RUN_SUCCESS if run.status == DagsterRunStatus.SUCCESS else DagsterEventType.RUN_FAILURE
    event = instance.get_records_for_run(run_id, of_type=kind).records[0].event_log_entry.dagster_event
    sensor = _sensors()["dependency_success_sensor" if kind == DagsterEventType.RUN_SUCCESS else "dependency_failure_sensor"]
    sensor(build_run_status_sensor_context(sensor_name=sensor.name, dagster_event=event, dagster_instance=instance, dagster_run=run))


def test_a_successful_result_records_the_project_as_succeeded(instance, runner):
    _fire(instance, _result_run(instance, runner, "a", "succeeded"))
    status = dep.read_statuses(instance, ["a"])["a"]
    assert status.status == SUCCEEDED and abs((datetime.now(timezone.utc) - status.finished_at).total_seconds()) < 60


def test_a_failed_result_records_the_project_as_failed(instance, runner):
    _fire(instance, _result_run(instance, runner, "a", "failed"))
    assert dep.read_statuses(instance, ["a"])["a"].status == FAILED


def test_a_cancelled_run_is_not_a_result(instance, runner):
    sub = _submit(instance, runner, "a").run_id
    instance.add_run_tags(sub, {monitoring.TAG_STATUS: "cancelled"})               # what report_run_result_op does before it fails
    _fire(instance, _result_run(instance, runner, "a", "cancelled", submitted_by=sub))
    assert dep.read_statuses(instance, ["a"]) == {}


def test_a_result_with_no_project_is_ignored(instance, runner):
    rid = _result_run(instance, runner, "", "succeeded")
    _fire(instance, rid)
    assert dep.read_statuses(instance, [""]) == {}


def test_a_late_event_does_not_overwrite_a_newer_status(instance, runner):
    dep.record_status(instance, "a", FAILED, datetime.now(timezone.utc) + 5 * MIN)
    _fire(instance, _result_run(instance, runner, "a", "succeeded"))
    assert dep.read_statuses(instance, ["a"])["a"].status == FAILED


def test_the_sensors_watch_the_result_job_and_are_on_by_default():
    for name, sensor in _sensors().items():
        assert sensor.default_status.value == "RUNNING", name
        assert sensor.minimum_interval_seconds == 15
    assert set(_sensors()) == {"dependency_success_sensor", "dependency_failure_sensor"}


# ── the whole flow, with the user's example ──────────────────────────────────────────────────────────────────────────────
def _monitor_tick(instance, runner):
    sensor = make_run_monitor_sensor(LOCATION, dbt_run_result_job)
    result = sensor(build_sensor_context(instance=instance, resources={"dbt_runner": runner}))
    return list(getattr(result, "run_requests", []) or [])


def _report(instance, runner):
    """The monitor reports every finished dbt run; the status sensors record each outcome."""
    for req in _monitor_tick(instance, runner):
        res = dbt_run_result_job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, run_config=req.run_config,
                                                    tags=req.tags, raise_on_error=False)
        _fire(instance, res.run_id)


def _run_to_the_end(instance, runner, pid, outcome):
    result = _submit(instance, runner, pid)
    assert result.success
    [rid] = [c for c in runner.runs if runner.runs[c]["project"] == pid and runner.runs[c]["status"] == "running"][-1:]
    runner.finish(rid, outcome)
    _report(instance, runner)


def test_a_fails_so_b_fails_so_c_fails_and_all_recover_when_a_succeeds(instance, runner):
    _run_to_the_end(instance, runner, "a", "succeeded")
    _run_to_the_end(instance, runner, "b", "succeeded")                            # b ran after a succeeded
    assert _submit(instance, runner, "c").success                                  # and c after b

    _run_to_the_end(instance, runner, "a", "failed")                               # a fails
    before = len(runner.triggers())
    result_b = _submit(instance, runner, "b")
    assert not result_b.success and "upstream project 'a' failed" in _failure_text(result_b)
    result_c = _submit(instance, runner, "c")
    assert not result_c.success and "upstream project 'b' failed" in _failure_text(result_c)
    assert len(runner.triggers()) == before                                        # neither b nor c processed anything

    _run_to_the_end(instance, runner, "a", "succeeded")                            # a is fixed
    assert _submit(instance, runner, "b").success
    _run_to_the_end(instance, runner, "b", "succeeded")
    assert _submit(instance, runner, "c").success


def test_a_stopped_upstream_goes_stale_and_the_job_below_it_fails(instance, runner):
    """A runs every 5 minutes; it succeeded, then stopped. Once its last success is older than 10 minutes, B fails."""
    _run_to_the_end(instance, runner, "a", "succeeded")
    assert _submit(instance, runner, "b").success
    later = datetime.now(timezone.utc) + 9 * MIN
    dep.enforce(instance, runner, "b", now=later)                                  # 9 minutes on: still inside the 10 minutes
    with pytest.raises(dep.DependencyBlocked) as blocked:
        dep.enforce(instance, runner, "b", now=datetime.now(timezone.utc) + 15 * MIN)    # 15 minutes on, a has not run again
    assert "last succeeded 15 minutes ago, more than the 10 minutes allowed" in blocked.value.reason
