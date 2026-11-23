"""DG-018: submitting a dbt run no longer holds a Dagster run slot while the dbt run builds.

Before: submit -> poll. The Dagster run (a slot of the QueuedRunCoordinator and a run-worker process) was busy for as long
as the Runner's Kubernetes Job took, up to the 2 h polling ceiling, only to wait. Now the job is `submit_run_op` alone and
ends once the Runner has accepted the run; the outcome is reported by `dbt_run_monitor_sensor`, which launches a
`dbt_run_result` run when the Runner says the dbt run finished (that run records the outcome and fails if the dbt run
did) and cancels one that overruns. The hand-over is a tag on the submitting Dagster run, so the state is Dagster's own
database. Real Dagster (an in-memory instance); only the Runner's HTTP side is faked.
"""
from __future__ import annotations

import httpx
import pytest
from dagster import (
    DagsterInstance,
    DagsterRunStatus,
    RunRequest,
    SkipReason,
    build_sensor_context,
)

from inaiera_dbt_runner_dags import monitoring
from inaiera_dbt_runner_dags.definitions import defs as generic_defs
from inaiera_dbt_runner_dags.jobs.dbt_run_job import (
    dbt_run_result_job,
    make_project_job,
)
from inaiera_dbt_runner_dags.location_defs import build_location_defs
from inaiera_dbt_runner_dags.naming import location_name
from inaiera_dbt_runner_dags.sensors.run_monitor import make_run_monitor_sensor

LOCATION = "acme-repo"
PID = "p1"


class FakeRunner:
    """The Runner as seen by Dagster: run states by id; records every call."""

    base_url = "http://runner.test"

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.unreachable = False
        self._n = 0

    def get_project(self, project_id):                    # the start check (dependencies.py): no links, so nothing above it
        return {"id": project_id, "upstream_project_ids": []}

    def list_projects(self):
        return []

    def trigger_run(self, project_id, **kw):
        self._n += 1
        rid = f"run-{self._n}"
        self.runs[rid] = {"id": rid, "status": "running", "git_sha": kw.get("git_sha")}
        self.calls.append(("trigger", project_id))
        return {"id": rid, "status": "queued", "git_sha": kw.get("git_sha")}

    def get_run(self, run_id):
        self.calls.append(("get", run_id))
        if self.unreachable:
            raise httpx.ConnectError("runner restarting")
        if run_id not in self.runs:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "http://x"), response=httpx.Response(404))
        return dict(self.runs[run_id])

    def cancel_run(self, run_id, **_kw):
        self.calls.append(("cancel", run_id))
        if run_id in self.runs:
            self.runs[run_id]["status"] = "cancelling"
        return self.runs.get(run_id)

    def cancel_run_by_key(self, *_a, **_k):
        return None

    def finish(self, run_id, status="succeeded", **extra):
        self.runs[run_id].update(status=status, duration_ms=4200, models_total=3, models_succeeded=3, **extra)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def instance():
    return DagsterInstance.ephemeral()


def _project_job(location=LOCATION):
    return make_project_job("analytics", PID, "analytics", location)


def _submit(instance, runner, **cfg):
    job = _project_job(cfg.pop("location", LOCATION))
    return job.execute_in_process(instance=instance, resources={"dbt_runner": runner}, raise_on_error=cfg.pop("raise_on_error", True))


def _sensor(location=LOCATION):
    return make_run_monitor_sensor(location, dbt_run_result_job)


def _tick(instance, runner, location=LOCATION):
    return _sensor(location)(build_sensor_context(instance=instance, resources={"dbt_runner": runner}))


def _requests(result) -> list[RunRequest]:
    return list(result.run_requests) if hasattr(result, "run_requests") else []


# ── the job no longer waits ──────────────────────────────────────────────
def test_the_project_job_is_submit_only():
    job = _project_job()
    assert [n.name for n in job.nodes] == ["submit_run_op"]
    assert "poll_run_op" not in str(job.run_config)


def test_the_dagster_run_ends_while_the_dbt_run_is_still_building(instance, runner):
    result = _submit(instance, runner)
    assert result.success
    assert instance.get_run_by_id(result.run_id).status == DagsterRunStatus.SUCCESS
    assert runner.runs["run-1"]["status"] == "running"                              # the Runner's run is still going
    assert [c[0] for c in runner.calls] == ["trigger"]                              # and nothing polled it


def test_the_submitting_run_is_tagged_for_the_monitor(instance, runner):
    run = instance.get_run_by_id(_submit(instance, runner).run_id)
    assert run.tags[monitoring.TAG_MONITOR] == monitoring.PENDING
    assert run.tags[monitoring.TAG_RUNNER_RUN] == "run-1"
    assert run.tags[monitoring.TAG_LOCATION] == LOCATION


def test_a_run_that_cannot_be_registered_for_monitoring_is_cancelled_not_left_building(instance, runner, monkeypatch):
    monkeypatch.setattr(instance, "add_run_tags", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db down")))
    result = _submit(instance, runner, raise_on_error=False)
    assert not result.success
    assert ("cancel", "run-1") in runner.calls


# ── the sensor reports outcomes ──────────────────────────────────────────
def test_nothing_is_reported_while_the_dbt_run_is_building(instance, runner):
    _submit(instance, runner)
    out = _tick(instance, runner)
    assert isinstance(out, SkipReason) and ("get", "run-1") in runner.calls


def test_a_finished_dbt_run_is_reported_once_with_a_stable_run_key(instance, runner):
    sub = _submit(instance, runner).run_id
    runner.finish("run-1")
    (req,) = _requests(_tick(instance, runner))
    assert req.run_key == "dbt-result::run-1"
    assert req.run_config["ops"]["report_run_result_op"]["config"] == {"run_id": "run-1", "submitted_by": sub, "timed_out": False}
    assert req.tags[monitoring.TAG_RUNNER_RUN] == "run-1" and req.tags[monitoring.TAG_SUBMITTED_BY] == sub
    (again,) = _requests(_tick(instance, runner))                                   # until reported, the SAME key: Dagster dedupes
    assert again.run_key == req.run_key


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_every_terminal_status_is_reported(instance, runner, status):
    _submit(instance, runner)
    runner.finish("run-1", status)
    assert len(_requests(_tick(instance, runner))) == 1


def test_only_this_locations_runs_are_watched(instance, runner):
    _submit(instance, runner, location="another-repo")
    runner.finish("run-1")
    assert _requests(_tick(instance, runner)) == []
    assert len(_requests(_tick(instance, runner, location="another-repo"))) == 1


def test_a_runner_that_cannot_be_reached_leaves_everything_pending_and_does_not_crash(instance, runner):
    _submit(instance, runner)
    runner.unreachable = True
    assert _requests(_tick(instance, runner)) == []
    runner.unreachable = False
    runner.finish("run-1")
    assert len(_requests(_tick(instance, runner))) == 1                             # picked up on the next tick


def test_a_run_the_runner_no_longer_knows_is_reported_instead_of_waited_for_ever(instance, runner):
    _submit(instance, runner)
    del runner.runs["run-1"]
    assert len(_requests(_tick(instance, runner))) == 1


def test_several_submissions_are_reported_independently(instance, runner):
    _submit(instance, runner)
    _submit(instance, runner)
    runner.finish("run-2")
    assert [r.run_key for r in _requests(_tick(instance, runner))] == ["dbt-result::run-2"]


# ── a run that overruns is cancelled ─────────────────────────────────────
def test_a_run_past_the_ceiling_is_cancelled_and_reported_as_timed_out(instance, runner, monkeypatch):
    _submit(instance, runner)
    monkeypatch.setattr(monitoring, "max_wait_seconds", lambda: -1.0)               # everything is already overdue
    (req,) = _requests(_tick(instance, runner))
    assert ("cancel", "run-1") in runner.calls
    assert req.run_config["ops"]["report_run_result_op"]["config"]["timed_out"] is True


def test_a_run_inside_the_ceiling_is_left_alone(instance, runner):
    _submit(instance, runner)
    assert _requests(_tick(instance, runner)) == [] and ("cancel", "run-1") not in runner.calls


def test_a_failing_cancel_still_reports_the_overrun(instance, runner, monkeypatch):
    _submit(instance, runner)
    monkeypatch.setattr(monitoring, "max_wait_seconds", lambda: -1.0)
    monkeypatch.setattr(runner, "cancel_run", lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert len(_requests(_tick(instance, runner))) == 1


# ── the result run ───────────────────────────────────────────────────────
def _report(instance, runner, run_id, submitted_by, **extra):
    return dbt_run_result_job.execute_in_process(
        instance=instance, resources={"dbt_runner": runner}, raise_on_error=False,
        run_config={"ops": {"report_run_result_op": {"config": {"run_id": run_id, "submitted_by": submitted_by, **extra}}}})


def test_a_successful_dbt_run_is_a_successful_result_run_with_its_metadata(instance, runner):
    sub = _submit(instance, runner).run_id
    runner.finish("run-1")
    result = _report(instance, runner, "run-1", sub)
    assert result.success
    run = instance.get_run_by_id(sub)
    assert run.tags[monitoring.TAG_MONITOR] == monitoring.REPORTED and run.tags[monitoring.TAG_STATUS] == "succeeded"


@pytest.mark.parametrize("status,words", [("failed", "failed"), ("cancelled", "cancelled")])
def test_a_failed_or_cancelled_dbt_run_is_a_failed_result_run(instance, runner, status, words):
    sub = _submit(instance, runner).run_id
    runner.finish("run-1", status, error_message="boom")
    result = _report(instance, runner, "run-1", sub)
    assert not result.success
    assert instance.get_run_by_id(sub).tags[monitoring.TAG_MONITOR] == monitoring.REPORTED     # reported even though it failed


def test_a_run_that_never_finished_is_a_failed_result_run(instance, runner):
    sub = _submit(instance, runner).run_id
    result = _report(instance, runner, "run-1", sub, timed_out=True)
    assert not result.success


def test_a_reported_run_is_not_watched_again(instance, runner):
    sub = _submit(instance, runner).run_id
    runner.finish("run-1")
    _report(instance, runner, "run-1", sub)
    calls = len(runner.calls)
    assert isinstance(_tick(instance, runner), SkipReason) and len(runner.calls) == calls


def test_reporting_survives_a_purged_submitting_run(instance, runner):
    runner.runs["run-9"] = {"id": "run-9", "status": "succeeded"}
    assert _report(instance, runner, "run-9", "no-such-dagster-run").success


# ── wiring ───────────────────────────────────────────────────────────────
def _project():
    return {"id": PID, "name": "analytics", "git_url": "https://github.com/acme/analytics.git", "git_ref": "main",
            "project_path": "", "schedule_cron": None, "updated_at": "2026-01-01T00:00:00+00:00"}


def test_every_per_repo_location_has_the_monitor_and_the_result_job():
    p = _project()
    repo = build_location_defs(location_name(p), [p]).get_repository_def()
    assert sorted(s.name for s in repo.sensor_defs) == ["dbt_run_monitor_sensor", "dependency_failure_sensor", "dependency_success_sensor", "git_commit_sensor", "project_schedule_sensor"]
    assert "dbt_run_result" in [j.name for j in repo.get_all_jobs()]
    job = next(j for j in repo.get_all_jobs() if j.name == "analytics")
    assert job.tags[monitoring.TAG_LOCATION] == location_name(p)                    # the tag its own monitor filters on


def test_the_generic_helm_location_has_only_the_monitor_and_no_schedule():
    repo = generic_defs.get_repository_def()
    assert sorted(s.name for s in repo.sensor_defs) == ["dbt_run_monitor_sensor", "dependency_failure_sensor", "dependency_success_sensor"]
    assert list(repo.schedule_defs) == []
    assert sorted(j.name for j in repo.get_all_jobs()) == ["dbt_run_result", "dbt_scheduled_run"]
    assert repo.get_job("dbt_scheduled_run").tags[monitoring.TAG_LOCATION] == monitoring.GENERIC_LOCATION


def test_the_monitor_runs_by_default_and_not_too_often():
    s = _sensor()
    assert s.default_status.value == "RUNNING" and s.minimum_interval_seconds >= 15


def test_the_ceiling_defaults_to_the_old_two_hours_and_can_be_changed(monkeypatch):
    assert monitoring.max_wait_seconds() == 7200.0
    monkeypatch.setenv("DBT_RUN_MAX_WAIT_SECONDS", "600")
    assert monitoring.max_wait_seconds() == 600.0
