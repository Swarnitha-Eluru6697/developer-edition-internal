"""dbt_run_monitor_sensor — reports the outcome of the dbt runs this location submitted (DG-018).

Submitting a dbt run no longer keeps a Dagster run busy until it finishes. Instead ``submit_run_op`` tags its own Dagster
run (``inaiera/dbt_monitor=pending`` plus the Runner's run id, see monitoring.py) and ends. Every tick this sensor:

  1. lists this location's Dagster runs still tagged ``pending`` (state in Dagster's own database: nothing to lose on a
     restart, no cursor to grow),
  2. asks the dbt Runner for each one's status (one GET each),
  3. for a run that reached ``succeeded`` / ``failed`` / ``cancelled``, requests a ``dbt_run_result`` run
     (``run_key = dbt-result::<runner run id>``, so a repeated tick or a replay never reports twice); that run records
     the outcome and fails if the dbt run did,
  4. for a run still going after ``DBT_RUN_MAX_WAIT_SECONDS`` (default 2 h, the old polling ceiling), asks the Runner to
     cancel it (DG-017) and requests the result run, which then fails with "did not finish".

The result run moves the submitting run's tag to ``reported``. A Runner that cannot be reached leaves everything
pending and is tried again next tick; a run the Runner no longer knows (404) is reported as failed.
"""
import httpx
import structlog
from dagster import (
    DefaultSensorStatus,
    JobDefinition,
    RunRequest,
    RunsFilter,
    SensorEvaluationContext,
    SensorResult,
    SkipReason,
    sensor,
)

from inaiera_dbt_runner_dags import monitoring
from inaiera_dbt_runner_dags.ops.report_result import TERMINAL
from inaiera_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

log = structlog.get_logger()

MAX_PENDING_PER_TICK = 200


def make_run_monitor_sensor(location: str, result_job: JobDefinition):
    @sensor(
        name="dbt_run_monitor_sensor",
        job=result_job,
        minimum_interval_seconds=20,
        default_status=DefaultSensorStatus.RUNNING,
        description=(
            f"Reports the outcome of the dbt runs submitted from {location}: launches {result_job.name} when the dbt Runner "
            "says a run finished, and cancels one that exceeds DBT_RUN_MAX_WAIT_SECONDS. Replaces holding a Dagster run "
            "slot while the dbt run builds."
        ),
    )
    def dbt_run_monitor_sensor(context: SensorEvaluationContext, dbt_runner: DbtRunnerClient):
        import datetime

        records = context.instance.get_run_records(
            filters=RunsFilter(tags={monitoring.TAG_MONITOR: monitoring.PENDING, monitoring.TAG_LOCATION: location}),
            limit=MAX_PENDING_PER_TICK, order_by="create_timestamp", ascending=True)
        if not records:
            return SkipReason("no dbt run is waiting for its outcome")

        now = datetime.datetime.now(datetime.timezone.utc)
        max_wait = monitoring.max_wait_seconds()
        requests: list[RunRequest] = []
        for rec in records:
            submitted = rec.dagster_run
            runner_run = submitted.tags.get(monitoring.TAG_RUNNER_RUN)
            if not runner_run:
                continue
            try:
                state = dbt_runner.get_run(runner_run)
                status = state.get("status", "unknown")
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    log.warning("dbt_run_monitor.runner_error", runner_run=runner_run, error=str(e))
                    continue
                status = "unknown"           # the Runner no longer has it (purged): report it rather than wait for ever
            except Exception as e:  # noqa: BLE001 — the Runner may be restarting: try again next tick
                log.warning("dbt_run_monitor.runner_unreachable", runner_run=runner_run, error=str(e))
                continue

            timed_out = False
            if status not in TERMINAL and status != "unknown":
                age = (now - rec.create_timestamp).total_seconds()
                if age <= max_wait:
                    continue
                timed_out = True
                log.warning("dbt_run_monitor.timeout", runner_run=runner_run, age_seconds=int(age), status=status)
                try:
                    dbt_runner.cancel_run(runner_run)       # DG-017: it must not go on building unobserved
                except Exception as e:  # noqa: BLE001
                    log.error("dbt_run_monitor.cancel_failed", runner_run=runner_run, error=str(e))

            requests.append(RunRequest(
                run_key=f"dbt-result::{runner_run}",
                run_config={"ops": {"report_run_result_op": {"config": {
                    "run_id": runner_run, "submitted_by": submitted.run_id, "timed_out": timed_out}}}},
                tags={monitoring.TAG_RUNNER_RUN: runner_run, monitoring.TAG_SUBMITTED_BY: submitted.run_id,
                      "project_id": submitted.tags.get("project_id", ""),
                      "project_name": submitted.tags.get("project_name", "")},
            ))
        if not requests:
            return SkipReason(f"{len(records)} dbt run(s) still building")
        return SensorResult(run_requests=requests)

    return dbt_run_monitor_sensor
