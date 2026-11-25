"""Dagster op that reports the outcome of a dbt Runner run (DG-018).

Launched by ``dbt_run_monitor_sensor`` once the Runner says a run has finished (or the monitor gave up on it): one HTTP
call, so the run slot it uses is held for seconds, never for the length of the dbt build. It records the same outcome
metadata the old polling step did, and fails when the dbt run failed, was cancelled or never finished, so a bad dbt run
is a failed Dagster run exactly as before.
"""
import structlog
from dagster import AssetMaterialization, Config, MetadataValue, OpExecutionContext, op

from pralai_dbt_runner_dags import monitoring
from pralai_dbt_runner_dags.assets import DATA_LINEAGE_KINDS, job_asset_key, project_asset_key
from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

log = structlog.get_logger()

TERMINAL = {"succeeded", "failed", "cancelled"}


class ReportRunResultConfig(Config):
    run_id: str                       # the dbt Runner run
    submitted_by: str = ""            # the Dagster run that submitted it (its tag is moved to "reported")
    timed_out: bool = False           # the monitor stopped waiting and asked the Runner to cancel it


@op(required_resource_keys={"dbt_runner"})
def report_run_result_op(context: OpExecutionContext, config: ReportRunResultConfig) -> None:
    client: DbtRunnerClient = context.resources.dbt_runner
    run_id = config.run_id
    run_data = client.get_run(run_id)
    status = run_data.get("status", "unknown")
    duration_ms = run_data.get("duration_ms") or 0
    models_total = run_data.get("models_total") or 0
    models_failed = run_data.get("models_failed") or 0
    error_message = run_data.get("error_message")

    context.add_output_metadata({
        "run_id": MetadataValue.text(run_id),
        "status": MetadataValue.text(status),
        "duration_seconds": MetadataValue.float(duration_ms / 1000),
        "models_total": MetadataValue.int(models_total),
        "models_succeeded": MetadataValue.int(run_data.get("models_succeeded") or 0),
        "models_failed": MetadataValue.int(models_failed),
        "git_sha": MetadataValue.text(run_data.get("git_sha") or ""),
        "error": MetadataValue.text(error_message or ""),
        "submitted_by_dagster_run": MetadataValue.text(config.submitted_by or "n/a"),
    })
    log.info("dagster.run_complete", run_id=run_id, status=status, duration_ms=duration_ms,
             models_total=models_total, models_failed=models_failed, timed_out=config.timed_out)

    # The submitting run is done being monitored. Best effort: if it has been purged there is nothing to mark.
    if config.submitted_by:
        try:
            context.instance.add_run_tags(config.submitted_by, {
                monitoring.TAG_MONITOR: monitoring.REPORTED, monitoring.TAG_STATUS: status})
        except Exception as e:  # noqa: BLE001
            log.warning("dagster.report_tag_failed", submitted_by=config.submitted_by, error=str(e))

    if status == "succeeded":
        _record_materialization(context, config.submitted_by, run_data)

    if status == "failed":
        raise RuntimeError(
            f"dbt run {run_id} failed. models_failed={models_failed}/{models_total}. "
            f"error: {error_message or '(see Workspace UI or Runner logs)'}")
    if status == "cancelled":
        raise RuntimeError(f"dbt run {run_id} was cancelled.")
    if status not in TERMINAL:
        raise RuntimeError(
            f"dbt run {run_id} had not finished within {monitoring.max_wait_seconds():.0f} s (status {status!r}); "
            "the monitor asked the Runner to cancel it.")


def _record_materialization(context: OpExecutionContext, submitted_by: str, run_data: dict) -> None:
    """The project's nodes on the Lineage page were built: record it, keyed like the nodes — the submitting run's code location and job.
    A dbt project or SQL job has a DATA node (assets.py) and, like every job of any kind, a JOB node in the group job_lineage. Runs of the
    generic job, or whose submitting run is gone, have no node to record."""
    if not submitted_by:
        return
    try:
        submitter = context.instance.get_run_by_id(submitted_by)
    except Exception as e:  # noqa: BLE001
        log.warning("dagster.materialization_lookup_failed", submitted_by=submitted_by, error=str(e))
        return
    location = (submitter.tags or {}).get(monitoring.TAG_LOCATION) if submitter else None
    if not submitter or not location or location == monitoring.GENERIC_LOCATION:
        return
    metadata = {
        "dbt_run_id": MetadataValue.text(run_data.get("id") or ""),
        "git_sha": MetadataValue.text(run_data.get("git_sha") or ""),
        "models_total": MetadataValue.int(run_data.get("models_total") or 0),
        "duration_seconds": MetadataValue.float((run_data.get("duration_ms") or 0) / 1000),
        "submitted_by_dagster_run": MetadataValue.text(submitted_by),
    }
    if (submitter.tags or {}).get("project_kind", "dbt") in DATA_LINEAGE_KINDS:
        context.log_event(AssetMaterialization(
            asset_key=project_asset_key(location, submitter.job_name),
            description="run succeeded in the InAiEra Runner", metadata=metadata))
    context.log_event(AssetMaterialization(
        asset_key=job_asset_key(location, submitter.job_name),
        description="job succeeded in the InAiEra Runner", metadata=metadata))
