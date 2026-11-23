"""dependency_success_sensor / dependency_failure_sensor — keep each project's latest result for the dependency check.

Both are Dagster run status sensors on ``dbt_run_result``: that job is launched once the Runner reports how a dbt run ended (see
run_monitor.py), and it FAILS when the run failed. (The submitting Dagster run is not the result: it is green as soon as the Runner has
accepted the run.) On each event they record the project's status with `dependencies.record_status`; `submit_run_op` reads it through
`dependencies.enforce` before any run starts, which is what makes a job below fail when one above it failed or has gone stale.

  * SUCCESS -> the project succeeded now.
  * FAILURE -> the project failed now, unless the Runner's run was CANCELLED (a cancelled run is not a result).

A run with no project (the result of a run submitted without a project_id tag) is ignored. An older result never replaces a newer one.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from dagster import (
    DagsterInstance,
    DagsterRunStatus,
    DefaultSensorStatus,
    JobDefinition,
    RunsFilter,
    RunStatusSensorContext,
    run_status_sensor,
)

from inaiera_dbt_runner_dags import dependencies, monitoring

log = structlog.get_logger()

INTERVAL_SECONDS = 15


def _finished_at(instance: DagsterInstance, run_id: str) -> datetime:
    """When the Dagster run ended (now, if the record has no end time yet)."""
    record = next(iter(instance.get_run_records(filters=RunsFilter(run_ids=[run_id]), limit=1)), None)
    end = getattr(record, "end_time", None)
    return datetime.fromtimestamp(end, tz=timezone.utc) if end else datetime.now(timezone.utc)


def record_result(context: RunStatusSensorContext, status: str) -> None:
    run = context.dagster_run
    tags = run.tags or {}
    project_id = tags.get("project_id")
    if not project_id:
        return
    if status == dependencies.FAILED:
        submitted_by = tags.get(monitoring.TAG_SUBMITTED_BY)
        submitter = context.instance.get_run_by_id(submitted_by) if submitted_by else None
        if submitter is not None and (submitter.tags or {}).get(monitoring.TAG_STATUS) == "cancelled":
            return                                                    # a cancelled run is not a result
    written = dependencies.record_status(context.instance, project_id, status, _finished_at(context.instance, run.run_id),
                                         run_id=tags.get(monitoring.TAG_RUNNER_RUN, run.run_id))
    log.info("dependency.status_recorded", project_id=project_id, status=status, written=written, dagster_run=run.run_id)


def make_dependency_status_sensors(location: str, result_job: JobDefinition) -> list:
    @run_status_sensor(
        run_status=DagsterRunStatus.SUCCESS, monitored_jobs=[result_job], name="dependency_success_sensor",
        minimum_interval_seconds=INTERVAL_SECONDS, default_status=DefaultSensorStatus.RUNNING,
        description=(f"Records that a project of {location} succeeded, when its dbt run's outcome is reported. The jobs below it "
                     "are refused unless every job above them has a recent success (dependencies.py)."),
    )
    def dependency_success_sensor(context: RunStatusSensorContext) -> None:
        record_result(context, dependencies.SUCCEEDED)

    @run_status_sensor(
        run_status=DagsterRunStatus.FAILURE, monitored_jobs=[result_job], name="dependency_failure_sensor",
        minimum_interval_seconds=INTERVAL_SECONDS, default_status=DefaultSensorStatus.RUNNING,
        description=(f"Records that a project of {location} failed, when its dbt run's outcome is reported (a cancelled run is not a "
                     "result). The jobs below it are refused until it succeeds again (dependencies.py)."),
    )
    def dependency_failure_sensor(context: RunStatusSensorContext) -> None:
        record_result(context, dependencies.FAILED)

    return [dependency_success_sensor, dependency_failure_sensor]
