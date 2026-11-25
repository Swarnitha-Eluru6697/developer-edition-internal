"""Dagster code location for InAiEra dbt Runner — Phase 2 orchestration.

This package lives alongside (not inside) the pralai-dbt-runner app
package. It does NOT import any app.* modules; instead it talks to the
dbt Runner over its own HTTP API. That keeps the Dagster code server
dependency-free with respect to dbt, asyncpg, and the rest of the app's
heavy stack.

Scheduling flow:
  1. project_schedule_sensor (one per repo location) fires every 30 s, fetches all
     schedule-enabled projects from the dbt Runner API, evaluates the cron expressions of
     its repo's projects, and emits a RunRequest for any occurrence that is now due.
  2. Each RunRequest launches that project's own Dagster job (one job per dbt project,
     grouped in a code location per Git repo — see location_manager.py) with the
     project_id and scheduled_fire_time as config.
  3. submit_run_op posts POST /api/v1/internal/projects/{id}/run to the dbt Runner
     and records the returned run_id in Dagster metadata. For a new-commit run it
     also sends the commit the sensor saw (git_sha) and fails if the Runner froze a
     different one, so a run labelled with commit A never builds commit B.
  4. The Dagster run ends there: it does not wait for the dbt run (DG-018), so it never holds a run slot while
     the Kubernetes Job builds. dbt_run_monitor_sensor (one per location) watches the Runner for each submitted run
     and, when it finishes, launches a dbt_run_result run that records the outcome (succeeded / failed / cancelled)
     and fails if the dbt run did.

This means:
  - Dagster UI shows every scheduled run, the dbt_run_result run with its outcome, and a link to the
    run's models / duration as Dagster output metadata.
  - The Workspace UI continues working unchanged: runs are written to the
    same `runs` table by the dbt Runner's own RunService.
  - The dbt Runner's reconciler, leases, k8s Job isolation, artifact store
    and OpenLineage outbox operate exactly as in manual-run mode.
"""

from pralai_dbt_runner_dags import log_scrub as _log_scrub  # noqa: E402

_log_scrub.install()  # DG-011: no repository credential reaches a log line, whichever call site logged it
