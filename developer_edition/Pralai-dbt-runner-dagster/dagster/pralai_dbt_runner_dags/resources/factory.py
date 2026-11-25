"""Resource wiring shared by the generic location and the per-repo locations."""
from __future__ import annotations

import os

from dagster import EnvVar

from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

_TARGET = os.environ.get("DAGSTER_DEPLOYMENT_TARGET", "local")


def make_resources() -> dict:
    if _TARGET == "prod":
        return {
            "dbt_runner": DbtRunnerClient(
                base_url=EnvVar("DBT_RUNNER_BASE_URL"),
                scheduler_jwt_secret=EnvVar("SCHEDULER_JWT_SECRET"),
                org_id=EnvVar("ORG_ID"),
            )
        }
    # local / default
    return {
        "dbt_runner": DbtRunnerClient(
            base_url=os.environ.get("DBT_RUNNER_BASE_URL", "http://localhost:8082"),
            scheduler_jwt_secret=os.environ.get("SCHEDULER_JWT_SECRET", "local-dev-dbtrunner-scheduler-secret"),
            org_id=os.environ.get("ORG_ID", "local-dev"),
        )
    }
