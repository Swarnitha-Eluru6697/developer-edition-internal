"""Dagster jobs: submit a dbt run to the dbt Runner and wait for completion.

Two kinds of job use the same submit -> poll steps:

  * ``dbt_scheduled_run`` — generic job (project chosen in the launch config), kept in
    its own location for manual runs. No sensor targets it.
  * one job PER dbt project (``make_project_job``), living in a code location named
    after the project's repo. The project is baked in as the default config, so a
    manual launch is one click, and the per-location sensors fire it on schedule /
    on a new commit.

Both sensors set the same config: select="" (full build); the schedule sensor also
records scheduled_fire_time. Each RunRequest carries a run_key so Dagster
deduplicates per project per fire time / per SHA.
"""
from dagster import graph, job

from inaiera_dbt_runner_dags import monitoring
from inaiera_dbt_runner_dags.ops.report_result import report_run_result_op
from inaiera_dbt_runner_dags.ops.trigger_and_poll import submit_run_op


@job(
    name="dbt_scheduled_run",
    tags={monitoring.TAG_LOCATION: monitoring.GENERIC_LOCATION},
    description=(
        "Generic job: submit a dbt build for the project given in the launch config to the "
        "InAiEra dbt Runner. The Dagster run ends once the Runner has accepted it (it does not hold a run slot while "
        "dbt builds); dbt_run_monitor_sensor reports the outcome in a dbt_run_result run. Manual use only: scheduled "
        "and commit-triggered runs use the per-project jobs."
    ),
)
def dbt_scheduled_run_job() -> None:
    """submit_run_op only (DG-018)."""
    submit_run_op()


@job(
    name="dbt_run_result",
    description=(
        "Reports the outcome of one dbt Runner run (launched by dbt_run_monitor_sensor): records its status and "
        "metadata, and fails if the dbt run failed, was cancelled or did not finish."
    ),
)
def dbt_run_result_job() -> None:
    report_run_result_op()


@graph(name="dbt_project_run")
def _dbt_project_graph() -> None:
    submit_run_op()


_KIND_LABEL = {"dbt": "dbt project", "sql": "SQL job", "python": "Python job"}


def make_project_job(job_name: str, project_id: str, project_name: str, location: str = monitoring.GENERIC_LOCATION,
                     kind: str = "dbt"):
    """A job that submits ONE registered project — a dbt project or a SQL job; the Runner runs either the same way. `location`
    is the code location whose monitor sensor reports its runs (it is tagged on every run of the job)."""
    return _dbt_project_graph.to_job(
        name=job_name,
        description=(f"Submit the {_KIND_LABEL.get(kind, kind)} {project_name} to the InAiEra Runner. The run ends once the "
                     "Runner has accepted it; dbt_run_monitor_sensor reports the outcome."),
        config={
            "ops": {
                "submit_run_op": {"config": {"project_id": project_id, "select": "", "scheduled_fire_time": ""}},
            }
        },
        tags={"project_id": project_id, "project_name": project_name, "project_kind": kind, monitoring.TAG_LOCATION: location},
    )
