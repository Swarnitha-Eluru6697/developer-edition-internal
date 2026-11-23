"""Dagster Definitions — a single GENERIC code location with the ``dbt_scheduled_run`` job.

NOT served by the local docker-compose stack any more: there, every dbt project is a job in a
per-repo code location (see ``location_defs.py`` and ``location_manager.py``). This module is
kept for the Helm chart (docker/helm), which still deploys one generic code server. The
job takes the project in its launch config and has NO sensor or schedule that STARTS runs (only the DG-018 monitor,
which reports the outcome of runs that were submitted, and cancels one that overruns): in a Helm
deployment Dagster only runs projects someone launches by hand, and the dbt Runner's own
scheduler (SCHEDULER_ENABLED=true, the chart default) is what runs them on their cron (DG-001).
Don't turn that scheduler off until per-repo locations with the sensors are deployed.

Resource configuration is controlled by the ``DAGSTER_DEPLOYMENT_TARGET``
environment variable:

  local   — suitable defaults for docker-compose local dev. The dbt Runner
             runs on localhost:8082 (mapped from the dbtrunner container).
             ``SCHEDULER_JWT_SECRET`` / ``ORG_ID`` can be any value (they must
             equal the runner's).

  prod    — all sensitive config comes from ``EnvVar()``, which Dagster
             resolves from the process environment at runtime. In k8s these
             are mounted from Kubernetes Secrets by the Helm chart.

To add a project, register it via the dbt Runner API or the Workspace UI. The
location manager notices it within ``SYNC_INTERVAL_SECONDS`` (default 30 s), adds the
project's job to its repo's folder, and the sensors pick up its schedule.
"""
from __future__ import annotations

from dagster import Definitions

from inaiera_dbt_runner_dags import monitoring
from inaiera_dbt_runner_dags.jobs.dbt_run_job import (
    dbt_run_result_job,
    dbt_scheduled_run_job,
)
from inaiera_dbt_runner_dags.resources.factory import make_resources
from inaiera_dbt_runner_dags.sensors.dependency_sensor import make_dependency_status_sensors
from inaiera_dbt_runner_dags.sensors.run_monitor import make_run_monitor_sensor

defs = Definitions(
    jobs=[dbt_scheduled_run_job, dbt_run_result_job],
    sensors=[make_run_monitor_sensor(monitoring.GENERIC_LOCATION, dbt_run_result_job),
             *make_dependency_status_sensors(monitoring.GENERIC_LOCATION, dbt_run_result_job)],
    resources=make_resources(),
)
