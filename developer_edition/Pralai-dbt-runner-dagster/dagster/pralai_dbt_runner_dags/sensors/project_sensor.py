"""project_schedule_sensor — Dagster replacement for the built-in scheduler_loop.

One instance per code location (= per repo): ``make_project_schedule_sensor`` binds it
to the jobs of that location's projects, and each RunRequest goes to the project's own
job. A project registered after the location loaded has no job yet; it is skipped
WITHOUT moving its cursor, so it fires on the first tick after the location reloads.

How it works
------------
Every 30 s the sensor:
  1. Fetches all schedule-enabled projects from the dbt Runner
     (GET /api/v1/internal/projects/scheduled — internal endpoint) and keeps the
     ones that belong to this location.
  2. For each project, evaluates the cron expression against the last fire
     time stored in the sensor cursor (a JSON dict {project_id: iso_ts}).
  3. If the next fire time is now or in the past, emits a RunRequest for
     that project's job.

Deduplication
-------------
Each RunRequest carries a run_key = "<project_id>::<fire_time_iso>".
Dagster stores emitted run_keys and skips any it has already launched, so
a sensor tick that fires immediately after a restart cannot double-launch.
That is the first layer. The second one is in the dbt Runner (DG-006): the
same instant travels in the run config -> submit_run_op -> the trigger body
(`scheduled_fire_time`), and the Runner claims (project, instant) in its
ScheduledOccurrence table in the same transaction that creates the run. A
replay that gets past the run_key (run history pruned, cursor reset, a
re-created location) therefore gets the FIRST run back instead of starting
a second dbt run; the Runner's own scheduler shares that watermark.

Cursor
------
The cursor is a JSON object mapping project_id (str) → last fired ISO-8601
timestamp. It is updated in the same atomic sensor evaluation as the
RunRequests it produced, so an evaluation that partially fires (some
projects due, some not) only advances watermarks for the projects it
actually fired.

Backlog coalescing
------------------
When the daemon was down or the sensor was paused, multiple cron
occurrences may be overdue. We fire only the most-recently-due occurrence
per project (same policy as the native scheduler_loop's _latest_due_fire)
rather than replaying the whole backlog. The cursor is set to that most-
recent occurrence so future ticks continue from the right watermark.

The latest due occurrence is COMPUTED (one backwards step from "now"), never found by walking the backlog, so a `* * * * *` schedule after
a month of downtime costs the same as after a minute (DG-016). Only the size of the backlog is counted, and counting stops at
``BACKLOG_COUNT_CAP``: past it the tick logs a warning (``project_schedule_sensor.backlog_exceeds_cap``) and tags the run
``schedule_backlog_skipped``, so an operator can see that occurrences were skipped without the sensor paying for the count.

Time zones (DG-015)
-------------------
Every instant is handled as UTC. A timestamp that carries an offset is CONVERTED (``astimezone``), a naive one is taken as UTC;
``replace(tzinfo=UTC)`` on an aware value would relabel the zone without converting the instant.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from croniter import croniter
from dagster import (
    DefaultSensorStatus,
    JobDefinition,
    RunConfig,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    sensor,
)

from pralai_dbt_runner_dags.naming import location_name as project_location
from pralai_dbt_runner_dags.resources.dbt_runner_client import DbtRunnerClient

log = structlog.get_logger()

_UTC = timezone.utc
# Counting the missed occurrences stops here (DG-016): enough to say "a lot were skipped", never enough to hurt a tick.
BACKLOG_COUNT_CAP = 1_000


def _now() -> datetime:
    return datetime.now(_UTC)


def _parse_instant(value: str, default: datetime) -> datetime:
    """An ISO-8601 timestamp as an aware UTC instant: converted if it carries an offset (or `Z`), taken as UTC if naive (DG-015).
    `default` when it cannot be parsed."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return default
    return parsed.replace(tzinfo=_UTC) if parsed.tzinfo is None else parsed.astimezone(_UTC)


def _latest_due_fire(cron_expr: str, first_due: datetime, now: datetime) -> datetime:
    """The most recent occurrence <= now, found in ONE backwards step from now — O(1) however long the downtime was (DG-016).
    `first_due` (an occurrence already known to be <= now) is the floor."""
    start = now.replace(microsecond=0) + timedelta(seconds=1)      # get_prev is strictly before its start: this makes "<= now" inclusive
    latest = croniter(cron_expr, start).get_prev(datetime)
    return max(latest, first_due)


def _backlog_size(cron_expr: str, first_due: datetime, now: datetime, cap: int = BACKLOG_COUNT_CAP) -> tuple[int, bool]:
    """(occurrences from first_due up to now, counting at most `cap`; whether the cap was reached)."""
    it = croniter(cron_expr, first_due)
    count = 1                                                     # first_due itself
    while count < cap:
        if it.get_next(datetime) > now:
            return count, False
        count += 1
    return count, True


def _next_fire_after(cron_expr: str, last: datetime) -> datetime | None:
    try:
        return croniter(cron_expr, last).get_next(datetime)
    except (ValueError, KeyError):
        return None


def make_project_schedule_sensor(location: str, jobs: dict[str, JobDefinition]):
    """Cron sensor for one location. ``jobs`` maps project id -> that project's job."""

    @sensor(
        name="project_schedule_sensor",
        jobs=list(jobs.values()),
        minimum_interval_seconds=30,
        default_status=DefaultSensorStatus.RUNNING,
        description=(
            f"Evaluates the cron schedules of the dbt projects in {location} and fires each "
            "project's job when an occurrence is due. Replaces the dbt Runner's built-in "
            "scheduler_loop (set SCHEDULER_ENABLED=false on the dbt Runner)."
        ),
    )
    def project_schedule_sensor(
        context: SensorEvaluationContext,
        dbt_runner: DbtRunnerClient,
    ) -> SensorResult:
        now = _now()
        cursor: dict[str, str] = json.loads(context.cursor or "{}")

        try:
            projects: list[dict[str, Any]] = dbt_runner.list_scheduled_projects()
        except Exception as e:
            log.error("project_schedule_sensor.fetch_failed", error=str(e))
            # Don't crash the sensor — it will retry on the next tick.
            return SensorResult(run_requests=[], cursor=context.cursor)

        run_requests: list[RunRequest] = []
        updated_cursor = dict(cursor)

        for project in projects:
            if project_location(project) != location:
                continue
            pid = project["id"]
            job = jobs.get(pid)
            if job is None:
                # Registered after this location loaded: wait for the reload, keep the cursor.
                log.info("project_schedule_sensor.no_job_yet", project_id=pid, location=location)
                continue
            cron_expr = project.get("schedule_cron")
            if not cron_expr:
                continue

            # Watermark: last fire time from cursor, or project's updated_at as
            # a safe bootstrap value (same logic as the native scheduler).
            last_str = cursor.get(pid) or project.get("updated_at") or now.isoformat()
            last = _parse_instant(last_str, default=now)             # DG-015: converts an aware value, never relabels it

            next_fire = _next_fire_after(cron_expr, last)
            if next_fire is None:
                log.warning("project_schedule_sensor.bad_cron", project_id=pid, cron=cron_expr)
                continue

            if next_fire > now:
                continue  # not due yet

            # Coalesce any backlog to the most-recent due occurrence (computed, not walked: DG-016).
            first_due = next_fire
            next_fire = _latest_due_fire(cron_expr, first_due, now)
            skipped = 0
            if next_fire > first_due:
                counted, capped = _backlog_size(cron_expr, first_due, now)
                skipped = counted - 1                                  # all but the one we fire
                if capped:
                    log.warning("project_schedule_sensor.backlog_exceeds_cap", project_id=pid, cron=cron_expr, cap=BACKLOG_COUNT_CAP,
                                first_missed=first_due.isoformat(), firing=next_fire.isoformat())
                elif skipped:
                    log.info("project_schedule_sensor.backlog_coalesced", project_id=pid, skipped=skipped, firing=next_fire.isoformat())

            run_key = f"{pid}::{next_fire.isoformat()}"
            log.info(
                "project_schedule_sensor.firing",
                project_id=pid,
                project_name=project.get("name"),
                scheduled_fire_time=next_fire.isoformat(),
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
                                    "select": "",
                                    "scheduled_fire_time": next_fire.isoformat(),
                                }
                            },
                        }
                    ),
                    tags={
                        "project_id": pid,
                        "project_name": project.get("name", ""),
                        "trigger": "scheduled",
                        "scheduled_fire_time": next_fire.isoformat(),
                        **({"schedule_backlog_skipped": f"{skipped}{'+' if skipped >= BACKLOG_COUNT_CAP - 1 else ''}"} if skipped else {}),
                    },
                )
            )
            updated_cursor[pid] = next_fire.isoformat()

        return SensorResult(
            run_requests=run_requests,
            cursor=json.dumps(updated_cursor),
        )

    return project_schedule_sensor
