"""Definitions for ONE per-repo code location.

The code-location manager starts one gRPC server per repo with ``DBT_LOCATION=<repo name>``;
this module (``--module-name pralai_dbt_runner_dags.location_defs --attribute defs``) then
builds that location's jobs and sensors from the projects registered in the dbt Runner:

    location (folder)  inaiera-dbt-test
        jobs           customers, products, revenue, ...        one per project
        assets         one per dbt project, linked by data dependency, and one per job of any kind in the group job_lineage, linked
                       by the job links (assets.py — the Lineage page)
        sensors        project_schedule_sensor, git_commit_sensor   shared by those jobs
                       dbt_run_monitor_sensor                        reports each submitted dbt run's outcome (DG-018)
                       dependency_success_sensor / dependency_failure_sensor   record each project's result for the dependency check
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
import structlog
from dagster import Definitions

from pralai_dbt_runner_dags.assets import job_lineage_asset_specs, project_asset_specs
from pralai_dbt_runner_dags.jobs.dbt_run_job import (
    dbt_run_result_job,
    make_project_job,
)
from pralai_dbt_runner_dags.naming import build_job_names, group_by_location
from pralai_dbt_runner_dags.resources.dbt_runner_client import mint_scheduler_token
from pralai_dbt_runner_dags.resources.factory import make_resources
from pralai_dbt_runner_dags.sensors.dependency_sensor import make_dependency_status_sensors
from pralai_dbt_runner_dags.sensors.git_sensor import make_git_commit_sensor
from pralai_dbt_runner_dags.sensors.project_sensor import make_project_schedule_sensor
from pralai_dbt_runner_dags.sensors.run_monitor import make_run_monitor_sensor

log = structlog.get_logger()


def fetch_all_projects(attempts: int = 5, delay_seconds: float = 3.0) -> list[dict[str, Any]]:
    """GET /internal/projects with the same env the resource uses (works for local and prod).

    Authenticates like the resource does (DBT-003): a fresh scoped scheduler token per attempt."""
    base = os.environ.get("DBT_RUNNER_BASE_URL", "http://localhost:8082")
    secret = os.environ.get("SCHEDULER_JWT_SECRET", "local-dev-dbtrunner-scheduler-secret")
    org_id = os.environ.get("ORG_ID", "local-dev")
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = httpx.get(
                f"{base}/api/v1/internal/projects",
                headers={"X-Internal-Service-Token": mint_scheduler_token(secret, org_id, "dbt.projects.read")},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(delay_seconds)
    raise RuntimeError(f"could not list projects from the dbt Runner at {base}: {last}")


def build_location_defs(location: str, projects: list[dict[str, Any]] | None = None) -> Definitions:
    projects = fetch_all_projects() if projects is None else projects
    mine = group_by_location(projects).get(location, [])
    if not mine:
        raise RuntimeError(f"no registered dbt project belongs to location {location!r}")
    names = build_job_names(mine)
    jobs = {p["id"]: make_project_job(names[p["id"]], p["id"], p.get("name", names[p["id"]]), location, p.get("kind", "dbt"))
            for p in mine}
    log.info("location_defs.built", location=location, jobs=sorted(names.values()))
    return Definitions(
        assets=[*project_asset_specs(location, projects), *job_lineage_asset_specs(location, projects)],
        jobs=[*jobs.values(), dbt_run_result_job],
        sensors=[make_project_schedule_sensor(location, jobs), make_git_commit_sensor(location, jobs),
                 make_run_monitor_sensor(location, dbt_run_result_job), *make_dependency_status_sensors(location, dbt_run_result_job)],
        resources=make_resources(),
    )


# Only built when a manager started this process for a location (not on a plain import).
defs = build_location_defs(os.environ["DBT_LOCATION"]) if os.environ.get("DBT_LOCATION") else None
