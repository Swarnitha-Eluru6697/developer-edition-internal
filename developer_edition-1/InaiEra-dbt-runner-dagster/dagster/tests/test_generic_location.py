"""DG-001: the generic location the Helm chart deploys schedules nothing (DG-018: apart from the outcome monitor).

That is why the dbt Runner's own scheduler must stay on in a chart-based deployment (DEPLOYMENT.md,
"Who schedules dbt projects"). If someone adds a sensor/schedule here, update that documentation and the
runner chart's scheduler.externalOrchestrator guard together with this test.
"""
from __future__ import annotations

from inaiera_dbt_runner_dags.definitions import defs


def test_the_helm_generic_location_has_its_job_and_only_sensors_that_never_start_a_run_and_no_schedules():
    """DG-018 added dbt_run_monitor_sensor: it REPORTS the outcome of runs that were submitted (and cancels one that
    overruns); the dependency sensors only RECORD each project's result. None starts a run, so the runner's own scheduler must
    still stay on. See test_run_monitor.py and test_dependencies.py."""
    repo = defs.get_repository_def()
    assert sorted(j.name for j in repo.get_all_jobs()) == ["dbt_run_result", "dbt_scheduled_run"]
    assert sorted(s.name for s in repo.sensor_defs) == ["dbt_run_monitor_sensor", "dependency_failure_sensor", "dependency_success_sensor"]
    assert list(repo.schedule_defs) == []
