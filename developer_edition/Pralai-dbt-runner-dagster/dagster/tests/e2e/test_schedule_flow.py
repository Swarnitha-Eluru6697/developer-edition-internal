"""The production-topology schedule test (DG-021): a cron schedule set through the runner's API is picked up by the daemon's schedule sensor and
becomes exactly ONE dbt run per cron instant, with the instant claimed in the runner (idempotent), and everything is put back afterwards."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from .conftest import pytestmark, sql  # noqa: F401  (the opt-in marker)

WAIT_SECONDS = 200          # the sensor ticks every 30 s; a * * * * * schedule is due every minute


@pytest.fixture
def scheduled_project(runner):
    """An existing project that currently has NO schedule; given `* * * * *` for the test and restored after."""
    candidates = [p for p in runner.projects() if not p["schedule_enabled"] and not p.get("git_trigger_enabled")]
    if not candidates:
        pytest.skip("no unscheduled project to use")
    p = candidates[0]
    before = {"schedule_cron": p["schedule_cron"], "schedule_enabled": p["schedule_enabled"]}
    t0 = datetime.now(timezone.utc)
    r = runner.call("PATCH", f"/projects/{p['id']}", "dbt.projects.write", {"schedule_cron": "* * * * *", "schedule_enabled": True})
    assert r.status_code == 200, r.text
    yield p, t0
    runner.call("PATCH", f"/projects/{p['id']}", "dbt.projects.write", {"schedule_enabled": False})
    if before["schedule_cron"] is None:                          # the API cannot send "no cron" (null means "unchanged"): put the NULL back directly
        sql(f"update projects set schedule_cron = NULL, schedule_enabled = false where id = '{p['id']}'")
    else:
        runner.call("PATCH", f"/projects/{p['id']}", "dbt.projects.write",
                    {"schedule_cron": before["schedule_cron"], "schedule_enabled": before["schedule_enabled"]})
    # remove only the runs this test caused (scheduled, created after it started)
    sql(f"delete from run_events where run_id in (select id from runs where project_id='{p['id']}' and created_at >= '{t0.isoformat()}'); "
        f"delete from scheduled_occurrences where project_id='{p['id']}' and created_at >= '{t0.isoformat()}'; "
        f"delete from runs where project_id='{p['id']}' and created_at >= '{t0.isoformat()}'")


def test_a_cron_schedule_becomes_exactly_one_run_per_instant(scheduled_project):
    project, t0 = scheduled_project
    pid = project["id"]
    deadline = time.time() + WAIT_SECONDS
    rows: list[tuple[str, str]] = []
    while time.time() < deadline:
        out = sql(f"select coalesce(o.scheduled_fire_time::text,'-')||'|'||r.trigger from runs r left join scheduled_occurrences o on o.run_id = r.id "
                  f"where r.project_id='{pid}' and r.created_at >= '{t0.isoformat()}' order by r.created_at")
        rows = [tuple(line.split("|", 1)) for line in out.splitlines() if line]
        if len(rows) >= 2:                                      # two distinct instants: the schedule keeps firing, not just once
            break
        time.sleep(5)
    assert len(rows) >= 2, f"the schedule produced {len(rows)} run(s) in {WAIT_SECONDS}s: {rows}"
    instants = [r[0] for r in rows]
    assert "-" not in instants, f"a run was started without claiming a cron instant: {rows}"
    assert len(instants) == len(set(instants)), f"the same cron instant started two runs: {rows}"      # idempotency
    assert {r[1] for r in rows} == {"scheduled"}
    # every fired instant is a whole minute (the cron's own instants), in UTC
    for inst in instants:
        t = datetime.fromisoformat(inst)
        assert t.second == 0 and t.utcoffset().total_seconds() == 0
