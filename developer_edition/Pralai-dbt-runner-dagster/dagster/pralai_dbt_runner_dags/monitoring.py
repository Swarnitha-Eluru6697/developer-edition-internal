"""How a submitted dbt run is tracked after its Dagster run has finished (DG-018).

The submit job used to be submit -> poll: the Dagster run, and so a Dagster run slot, stayed busy for as long as the
Runner's Kubernetes Job took (up to two hours), only to wait. Now the job ends as soon as the Runner has accepted the run
and marks its own Dagster run with the tags below; `dbt_run_monitor_sensor` watches the tagged runs and launches
`dbt_run_result` once the Runner reports an outcome. The state lives in Dagster's own database (these run tags), so it
survives restarts and needs nothing else.
"""
from __future__ import annotations

import os

# On the Dagster run that SUBMITTED a dbt run.
TAG_MONITOR = "inaiera/dbt_monitor"            # "pending" until the outcome has been reported, then "reported"
TAG_RUNNER_RUN = "inaiera/dbt_run_id"          # the dbt Runner's run id
TAG_LOCATION = "inaiera/dbt_location"          # the code location whose monitor sensor owns it
TAG_STATUS = "inaiera/dbt_status"              # the Runner's final status, set when reported
TAG_TRIGGERED_BY = "inaiera/triggered_by"      # who started the run by hand (the Workspace's Run now sets it): recorded as an api run
# On the Dagster run that REPORTS one.
TAG_SUBMITTED_BY = "inaiera/dbt_submitted_by"  # the Dagster run id that submitted it

PENDING = "pending"
REPORTED = "reported"

# The longest a submitted run may take before the monitor cancels it (the old polling ceiling, 2 h).
DEFAULT_MAX_WAIT_SECONDS = 7200.0


def max_wait_seconds() -> float:
    return float(os.environ.get("DBT_RUN_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS))


# One generic (Helm) location has no repo name of its own.
GENERIC_LOCATION = "pralai-dbt-runner-dags"
