"""DG-015 / DG-016 (and DG-021's schedule/idempotency/time-zone unit coverage): how the cron sensor decides what is due.

DG-015: a timestamp that already carried an offset was RELABELLED as UTC (`.replace(tzinfo=UTC)`) instead of converted, so a watermark of
11:30+05:30 (= 06:00 UTC) was read as 11:30 UTC and a due schedule did not fire for hours.
DG-016: coalescing a backlog walked every missed occurrence (up to 100 000 steps, and silently stopped there): after a long outage a
high-frequency schedule burned CPU in the tick and could return a stale "latest". Now the latest due occurrence is computed in one step, and
only the SIZE of the backlog is counted, capped, with a warning.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import structlog
from croniter import croniter
from inaiera_dbt_runner_dags.location_defs import build_location_defs
from inaiera_dbt_runner_dags.naming import location_name
from inaiera_dbt_runner_dags.sensors import project_sensor as ps

from dagster import build_sensor_context

UTC = timezone.utc
URL = "https://github.com/acme/demo.git"


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


# ── DG-015 ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("2026-10-14T06:00:00+00:00", "2026-10-14T06:00:00+00:00"),
    ("2026-10-14T11:30:00+05:30", "2026-10-14T06:00:00+00:00"),          # converted, NOT relabelled
    ("2026-10-14T01:00:00-05:00", "2026-10-14T06:00:00+00:00"),
    ("2026-10-14T06:00:00Z", "2026-10-14T06:00:00+00:00"),
    ("2026-10-14T06:00:00", "2026-10-14T06:00:00+00:00"),                # naive: taken as UTC (the documented intent)
    ("2026-10-14T23:30:00-02:00", "2026-10-15T01:30:00+00:00"),          # crosses a day boundary
])
def test_an_instant_is_converted_to_utc_never_relabelled(value, expected):
    assert ps._parse_instant(value, default=datetime(2000, 1, 1, tzinfo=UTC)) == dt(expected)


@pytest.mark.parametrize("bad", ["", "not a time", None, "2026-13-45T00:00:00"])
def test_an_unparsable_instant_falls_back_to_the_default(bad):
    default = datetime(2000, 1, 1, tzinfo=UTC)
    assert ps._parse_instant(bad, default=default) == default


# ── helpers for sensor-level tests ──────────────────────────────────────────────────────
def _project(cron="0 * * * *", updated_at="2026-10-14T06:00:00+00:00", pid="1"):
    return {"id": pid, "name": "demo", "git_url": URL, "git_ref": "main", "project_path": "", "schedule_cron": cron,
            "schedule_enabled": True, "updated_at": updated_at, "last_synced_sha": None}


class _Runner:
    def __init__(self, projects):
        self._projects = projects

    def list_scheduled_projects(self):
        return self._projects


def _tick(projects, now, cursor=None, monkeypatch=None):
    monkeypatch.setattr(ps, "_now", lambda: now)
    defs = build_location_defs(location_name(projects[0]), projects)
    sensor = next(s for s in defs.sensors if s.name == "project_schedule_sensor")
    return sensor(build_sensor_context(resources={"dbt_runner": _Runner(projects)}, cursor=cursor))


def _fire(result):
    (req,) = result.run_requests
    return req


def test_a_project_updated_at_in_another_offset_fires_at_the_right_instant(monkeypatch):
    """11:30+05:30 is 06:00 UTC: at 07:05 UTC the 07:00 occurrence is due. The old code read it as 11:30 UTC (next fire 12:00): nothing fired."""
    result = _tick([_project(updated_at="2026-10-14T11:30:00+05:30")], dt("2026-10-14T07:05:00+00:00"), monkeypatch=monkeypatch)
    assert _fire(result).tags["scheduled_fire_time"] == "2026-10-14T07:00:00+00:00"


def test_a_cursor_in_another_offset_is_converted_too(monkeypatch):
    cursor = json.dumps({"1": "2026-10-14T09:00:00+03:00"})                 # = 06:00 UTC
    result = _tick([_project()], dt("2026-10-14T07:05:00+00:00"), cursor=cursor, monkeypatch=monkeypatch)
    assert _fire(result).tags["scheduled_fire_time"] == "2026-10-14T07:00:00+00:00"


def test_the_fire_instant_sent_to_the_runner_is_utc(monkeypatch):
    req = _fire(_tick([_project(updated_at="2026-10-14T11:30:00+05:30")], dt("2026-10-14T07:05:00+00:00"), monkeypatch=monkeypatch))
    sent = req.run_config["ops"]["submit_run_op"]["config"]["scheduled_fire_time"]
    assert datetime.fromisoformat(sent).utcoffset() == timedelta(0)


# ── idempotency ─────────────────────────────────────────────────────────────────────────────
def test_the_same_tick_twice_yields_the_same_run_key_and_the_cursor_stops_a_third(monkeypatch):
    now = dt("2026-10-14T07:05:00+00:00")
    first = _tick([_project()], now, monkeypatch=monkeypatch)
    again = _tick([_project()], now, monkeypatch=monkeypatch)
    assert _fire(first).run_key == _fire(again).run_key == "1::2026-10-14T07:00:00+00:00"        # Dagster dedupes on this
    after = _tick([_project()], now, cursor=first.cursor, monkeypatch=monkeypatch)              # the cursor moved on: nothing more
    assert after.run_requests == []


def test_nothing_fires_before_the_next_occurrence(monkeypatch):
    assert _tick([_project()], dt("2026-10-14T06:59:59+00:00"), monkeypatch=monkeypatch).run_requests == []


def test_an_occurrence_exactly_at_now_is_due(monkeypatch):
    assert _fire(_tick([_project()], dt("2026-10-14T07:00:00+00:00"), monkeypatch=monkeypatch)).tags["scheduled_fire_time"] == "2026-10-14T07:00:00+00:00"


def test_a_bad_cron_is_skipped_not_fatal(monkeypatch):
    assert _tick([_project(cron="not a cron")], dt("2026-10-14T07:05:00+00:00"), monkeypatch=monkeypatch).run_requests == []


# ── DG-016 ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cron", ["* * * * *", "*/5 * * * *", "0 * * * *", "15 3 * * 1", "0 0 1 * *", "30 2 * * *"])
@pytest.mark.parametrize("gap", [timedelta(minutes=3), timedelta(hours=7, minutes=11), timedelta(days=9, minutes=1), timedelta(days=400)])
def test_the_computed_latest_due_equals_the_brute_force_walk(cron, gap):
    start = dt("2024-01-01T00:00:00+00:00")
    now = start + gap + timedelta(seconds=17)
    first_due = croniter(cron, start).get_next(datetime)
    if first_due > now:
        pytest.skip("nothing due in this window")
    it, brute = croniter(cron, first_due), first_due
    while (cand := it.get_next(datetime)) <= now:
        brute = cand
    assert ps._latest_due_fire(cron, first_due, now) == brute


def test_now_exactly_on_an_occurrence_is_inclusive():
    first_due = dt("2026-10-14T06:00:00+00:00")
    assert ps._latest_due_fire("0 * * * *", first_due, dt("2026-10-14T09:00:00+00:00")) == dt("2026-10-14T09:00:00+00:00")


def test_a_year_of_downtime_on_a_per_minute_schedule_is_cheap_and_correct(monkeypatch):
    """The old walk stopped after 100 000 steps (~69 days of per-minute occurrences) and returned a STALE instant."""
    now = dt("2026-10-14T07:05:30+00:00")
    stale = _project(cron="* * * * *", updated_at="2025-10-14T07:05:00+00:00")
    t0 = time.perf_counter()
    result = _tick([stale], now, monkeypatch=monkeypatch)
    assert time.perf_counter() - t0 < 2.0
    assert _fire(result).tags["scheduled_fire_time"] == "2026-10-14T07:05:00+00:00"          # the latest, not 69 days after the watermark


def test_the_work_per_tick_is_bounded_by_the_cap_not_by_the_backlog(monkeypatch):
    calls = {"n": 0}
    real = croniter.get_next

    def counting(self, *a, **kw):
        calls["n"] += 1
        return real(self, *a, **kw)
    monkeypatch.setattr(croniter, "get_next", counting)
    _tick([_project(cron="* * * * *", updated_at="2020-01-01T00:00:00+00:00")], dt("2026-10-14T07:05:30+00:00"), monkeypatch=monkeypatch)
    assert calls["n"] <= ps.BACKLOG_COUNT_CAP + 5


def test_a_backlog_over_the_cap_warns_and_tags_the_run(monkeypatch):
    with structlog.testing.capture_logs() as logs:
        result = _tick([_project(cron="* * * * *", updated_at="2025-10-14T07:05:00+00:00")], dt("2026-10-14T07:05:30+00:00"), monkeypatch=monkeypatch)
    (warning,) = [e for e in logs if e["event"] == "project_schedule_sensor.backlog_exceeds_cap"]
    assert warning["cap"] == ps.BACKLOG_COUNT_CAP and warning["project_id"] == "1" and warning["log_level"] == "warning"
    assert _fire(result).tags["schedule_backlog_skipped"].endswith("+")


def test_a_small_backlog_is_counted_exactly_and_logged_as_info(monkeypatch):
    with structlog.testing.capture_logs() as logs:
        result = _tick([_project(cron="*/10 * * * *", updated_at="2026-10-14T06:00:00+00:00")], dt("2026-10-14T07:05:00+00:00"), monkeypatch=monkeypatch)
    # due at 06:10 .. 07:00 -> 6 occurrences, fire the last, 5 skipped
    assert _fire(result).tags["scheduled_fire_time"] == "2026-10-14T07:00:00+00:00"
    assert _fire(result).tags["schedule_backlog_skipped"] == "5"
    assert [e["skipped"] for e in logs if e["event"] == "project_schedule_sensor.backlog_coalesced"] == [5]
    assert not [e for e in logs if e["event"] == "project_schedule_sensor.backlog_exceeds_cap"]


def test_no_backlog_no_tag_no_warning(monkeypatch):
    with structlog.testing.capture_logs() as logs:
        result = _tick([_project()], dt("2026-10-14T07:05:00+00:00"), monkeypatch=monkeypatch)
    assert "schedule_backlog_skipped" not in _fire(result).tags
    assert not [e for e in logs if "backlog" in e["event"]]


def test_backlog_size_stops_at_the_cap():
    count, capped = ps._backlog_size("* * * * *", dt("2020-01-01T00:00:00+00:00"), dt("2026-01-01T00:00:00+00:00"))
    assert (count, capped) == (ps.BACKLOG_COUNT_CAP, True)
    assert ps._backlog_size("0 * * * *", dt("2026-10-14T06:00:00+00:00"), dt("2026-10-14T08:30:00+00:00")) == (3, False)
