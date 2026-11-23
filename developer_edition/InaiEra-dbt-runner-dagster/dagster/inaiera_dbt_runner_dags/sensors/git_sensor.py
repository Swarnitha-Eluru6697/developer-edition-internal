"""git_commit_sensor — fires an incremental dbt build when a new commit lands.

How it works
------------
Every 60 s the sensor iterates the dbt projects that have "run on new commit" switched on (``git_trigger_enabled``: its own
setting and its own listing, independent of whether the project also has a cron schedule — DG-008). For each project it calls the dbt Runner to get the last
successfully-built SHA, then asks the dbt Runner for the commit the project's ref points to right now
(``GET /internal/projects/{id}/head`` — the Runner runs ``git ls-remote`` with the project's own credential; Dagster holds no Git
credentials and needs no Git egress, DG-009). If the two differ, a
full build is queued. (An incremental ``state:modified+`` run needs a saved dbt
manifest to compare against, which the dbt Runner does not keep yet.)

run_key = "<project_id>::<remote_sha>" so Dagster deduplicates at the
orchestrator level even across daemon restarts. The same SHA is sent to the
dbt Runner (run config -> submit_run_op -> `git_sha`), which builds exactly that
commit, so a run labelled with commit A cannot execute a later commit B (DG-007).

Note
----
This sensor fires on *every new commit* of the projects with git_trigger_enabled, independently of the cron schedule in
project_schedule_sensor: a project may have either, both or neither. To stop commit-triggered builds of one project, turn its
git_trigger_enabled off (PATCH /projects/{id}); to stop them all, stop this sensor.
"""

from typing import Any

import structlog
from dagster import (
    DefaultSensorStatus,
    JobDefinition,
    RunConfig,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    sensor,
)

from inaiera_dbt_runner_dags.naming import location_name as project_location
from inaiera_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

log = structlog.get_logger()


def make_git_commit_sensor(location: str, jobs: dict[str, JobDefinition]):
    """New-commit sensor for one location. ``jobs`` maps project id -> that project's job."""

    @sensor(
        name="git_commit_sensor",
        jobs=list(jobs.values()),
        minimum_interval_seconds=60,
        default_status=DefaultSensorStatus.RUNNING,
        description=(
            f"Detects new commits on each dbt project's git_ref in {location} and fires that "
            "project's job (a full build), for the projects with git_trigger_enabled. Independent of "
            "the cron schedule sensor: a project may have a schedule, a commit trigger, both or neither."
        ),
    )
    def git_commit_sensor(
        context: SensorEvaluationContext,
        dbt_runner: DbtRunnerClient,
    ) -> SensorResult:
        try:
            projects: list[dict[str, Any]] = dbt_runner.list_git_triggered_projects()
        except Exception as e:
            log.error("git_commit_sensor.fetch_failed", error=str(e))
            return SensorResult(run_requests=[], cursor=context.cursor)

        run_requests: list[RunRequest] = []

        for project in projects:
            if project_location(project) != location:
                continue
            pid = project["id"]
            job = jobs.get(pid)
            if job is None:
                log.info("git_commit_sensor.no_job_yet", project_id=pid, location=location)
                continue
            # DG-009: the RUNNER resolves the ref (it holds the Git credentials and the Git egress); Dagster never runs git.
            try:
                remote_sha = dbt_runner.get_project_head(pid)
            except Exception as e:
                # unreachable remote, a ref that does not exist yet, a runner hiccup: not known this tick — try again at the next one
                log.warning("git_commit_sensor.head_unavailable", project_id=pid, error=str(e)[:200])
                continue

            last_synced_sha: str | None = project.get("last_synced_sha")

            if last_synced_sha == remote_sha:
                continue  # nothing new

            run_key = f"{pid}::{remote_sha}"
            select = ""  # full build; "state:modified+" needs a saved manifest the Runner lacks

            log.info(
                "git_commit_sensor.new_commit",
                project_id=pid,
                project_name=project.get("name"),
                previous_sha=last_synced_sha,
                new_sha=remote_sha,
                run_key=run_key,
            )

            run_requests.append(
                RunRequest(
                    run_key=run_key,
                    job_name=job.name,
                    run_config=RunConfig(
                        ops={
                            "submit_run_op": {
                                "config": {
                                    "project_id": pid,
                                    "select": select,
                                    "scheduled_fire_time": "",
                                    # DG-007: the run builds exactly the commit this tick saw
                                    "git_sha": remote_sha,
                                }
                            },
                        }
                    ),
                    tags={
                        "project_id": pid,
                        "project_name": project.get("name", ""),
                        "trigger": "git_push",
                        "git_sha": remote_sha,
                        "select": select,
                    },
                )
            )

        # cursor is not used by this sensor (SHA comparison is stateless via the
        # Runner's last_synced_sha field), but we pass it through unchanged.
        return SensorResult(run_requests=run_requests, cursor=context.cursor)

    return git_commit_sensor
