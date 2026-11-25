"""Job dependencies, enforced in Dagster.

A job's Upstream Jobs are the jobs it is linked below (the links are stored by the dbt Runner; Workspace edits them). A job must not
process anything unless every job directly above it has a RECENT SUCCESS:

  * its latest result is a success (a failure of ANY job above it, however far, also blocks), and
  * that success is no older than the upstream's schedule interval + GRACE, never less than MIN_VALID (10 minutes when the upstream has
    no schedule). The interval is the longest gap between two fires over a week, so a weekday-only job is not "stale" every weekend.

An upstream that has no result at all has "not run yet" and blocks too.

Two halves, both in this repo:

  * `sensors/dependency_sensor.py` — run status sensors on the ``dbt_run_result`` job: when the outcome of a project's dbt run is
    reported (SUCCESS or FAILURE) they record that project's status here (`record_status`).
  * `submit_run_op` calls `enforce` before it asks the Runner to start anything, for EVERY Dagster run of a project: cron, new commit,
    a launch from the Dagster UI, or the Workspace's Run now (which launches the Dagster job). A refused run does not reach the Runner;
    it fails with the reason and records the project as failed, so the jobs below it are refused too.

The status lives in the Dagster instance's own key-value storage (`run_storage` cursor values), so it is shared by every code location
(one per repo: a link may cross repos) and survives restarts.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import pairwise
from typing import Any

import structlog
from croniter import croniter
from dagster import DagsterInstance

log = structlog.get_logger()

# How recent an upstream's last success must be: its schedule interval + GRACE, never less than MIN_VALID.
MIN_VALID = timedelta(minutes=10)
GRACE = timedelta(minutes=5)

SUCCEEDED = "succeeded"
FAILED = "failed"

STATE_PREFIX = "inaiera/dependency_status/"


# ── the recorded status of a project ───────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Status:
    status: str                  # SUCCEEDED | FAILED
    finished_at: datetime
    run_id: str = ""             # the dbt Runner run (or Dagster run) behind it
    detail: str = ""             # for a refused run: why


def _key(project_id: str) -> str:
    return STATE_PREFIX + project_id


def read_statuses(instance: DagsterInstance, project_ids: Iterable[str]) -> dict[str, Status]:
    """The recorded status of each project that has one."""
    ids = list(dict.fromkeys(project_ids))
    if not ids:
        return {}
    raw = instance.run_storage.get_cursor_values({_key(p) for p in ids})
    out: dict[str, Status] = {}
    for pid in ids:
        value = raw.get(_key(pid))
        if not value:
            continue
        try:
            data = json.loads(value)
            out[pid] = Status(data["status"], datetime.fromisoformat(data["finished_at"]), data.get("run_id", ""), data.get("detail", ""))
        except (ValueError, KeyError, TypeError):
            log.warning("dependency.status_unreadable", project_id=pid)        # treated as "no result"
    return out


def record_status(instance: DagsterInstance, project_id: str, status: str, finished_at: datetime, *, run_id: str = "",
                  detail: str = "") -> bool:
    """Record `project_id`'s latest result. An older result never replaces a newer one (two sensors, and a refused run, can report out of
    order). Returns whether it was written."""
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    current = read_statuses(instance, [project_id]).get(project_id)
    if current and current.finished_at > finished_at:
        return False
    instance.run_storage.set_cursor_values({_key(project_id): json.dumps({
        "status": status, "finished_at": finished_at.isoformat(), "run_id": run_id, "detail": detail[:1000]})})
    return True


# ── how long a success stays valid ─────────────────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=512)
def _longest_gap(cron: str, day: str) -> timedelta | None:
    """The longest gap between two fires of `cron` over at least a week (or at least three fires), or None for an invalid cron.
    `day` only keys the cache: the answer can differ from one week to the next (month ends, leap days)."""
    try:
        start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        it = croniter(cron, start)
        fires = [it.get_next(datetime)]
        while len(fires) < 400 and not (len(fires) >= 3 and fires[-1] - fires[0] >= timedelta(days=7)):
            fires.append(it.get_next(datetime))
    except Exception:  # noqa: BLE001  (a malformed cron, or one that never fires again)
        return None
    return max((b - a for a, b in pairwise(fires)), default=None)


def valid_for(cron: str | None, enabled: bool, now: datetime) -> timedelta:
    """How old an upstream's last success may be: its schedule interval + GRACE, never less than MIN_VALID."""
    gap = _longest_gap(cron, now.date().isoformat()) if cron and enabled else None
    return max(MIN_VALID, gap + GRACE) if gap else MIN_VALID


# ── the rule ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Blocker:
    project_id: str
    name: str
    reason: str                          # "failed" | "stale" | "never_ran"
    age_minutes: int | None = None       # stale: how long ago the last success finished
    valid_minutes: int | None = None     # stale: how long it may be


def ancestors(project_id: str, projects: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every project above `project_id` (its upstream jobs, theirs, ...), nearest first."""
    by_id = {p["id"]: p for p in projects}
    seen = {project_id}
    order: list[str] = []
    queue = deque([project_id])
    while queue:
        for up in sorted(by_id.get(queue.popleft(), {}).get("upstream_project_ids") or [], key=lambda i: (by_id.get(i, {}).get("name", ""), i)):
            if up not in seen:
                seen.add(up)
                order.append(up)
                queue.append(up)
    return order


def evaluate(project_id: str, projects: Sequence[Mapping[str, Any]], statuses: Mapping[str, Status], now: datetime) -> list[Blocker]:
    """What stops `project_id` from running now: a failed project anywhere above it, and for each DIRECT upstream one that has not run or
    whose last success is too old. Nearest first. Empty means it may run."""
    by_id = {p["id"]: p for p in projects}
    me = by_id.get(project_id)
    if me is None:
        return []
    direct = set(me.get("upstream_project_ids") or [])
    found: list[Blocker] = []
    for up in ancestors(project_id, projects):
        info = by_id.get(up, {})
        name = info.get("name", up)
        status = statuses.get(up)
        if status is not None and status.status == FAILED:
            found.append(Blocker(up, name, "failed"))
        elif up in direct:
            if status is None:
                found.append(Blocker(up, name, "never_ran"))
            else:
                window, age = valid_for(info.get("schedule_cron"), bool(info.get("schedule_enabled")), now), now - status.finished_at
                if age > window:
                    found.append(Blocker(up, name, "stale", int(age.total_seconds() // 60), int(window.total_seconds() // 60)))
    return found


def _span(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)} hours"
    return f"{round(minutes / 1440)} days"


def reason_text(blockers: Sequence[Blocker]) -> str:
    parts = []
    for b in blockers:
        if b.reason == "failed":
            parts.append(f"upstream project '{b.name}' failed")
        elif b.reason == "stale":
            parts.append(f"upstream project '{b.name}' last succeeded {_span(b.age_minutes or 0)} ago, "
                         f"more than the {_span(b.valid_minutes or 0)} allowed")
        else:
            parts.append(f"upstream project '{b.name}' has not run yet")
    return f"Not started: {'; '.join(parts)}. It can run again once every upstream project has a recent successful run."


# ── the start check ────────────────────────────────────────────────────────────────────────────────────────────────────────
class DependencyBlocked(Exception):
    def __init__(self, reason: str, blockers: Sequence[Blocker]):
        super().__init__(reason)
        self.reason = reason
        self.blockers = list(blockers)


def enforce(instance: DagsterInstance, client: Any, project_id: str, now: datetime | None = None) -> None:
    """Refuse to start `project_id` unless every job above it has a recent success. Called at the top of every submission.

    A refused run records the project as FAILED (finished now), so the jobs below it are refused as well: A fails, so B is refused and
    fails, so C is refused. `client` is the dbt Runner client: it supplies the links (upstream_project_ids) and each project's schedule.
    Raises DependencyBlocked; returns when the run may go ahead."""
    now = now or datetime.now(timezone.utc)
    if not (client.get_project(project_id).get("upstream_project_ids") or []):
        return                                                                  # nothing above it: nothing to check
    projects = client.list_projects()
    blockers = evaluate(project_id, projects, read_statuses(instance, ancestors(project_id, projects)), now)
    if not blockers:
        return
    reason = reason_text(blockers)
    record_status(instance, project_id, FAILED, now, detail=reason)
    log.warning("dagster.dependency_blocked", project_id=project_id, blockers=[(b.name, b.reason) for b in blockers])
    raise DependencyBlocked(reason, blockers)
