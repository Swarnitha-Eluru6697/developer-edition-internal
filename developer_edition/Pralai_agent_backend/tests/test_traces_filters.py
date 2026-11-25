"""
Date filtering on GET /api/traces.

It belongs on the server because this endpoint is paginated (100 rows by
default, 685 traces in this deployment). Filtering in the browser would
narrow only the page already fetched and silently hide every match on
later pages, so the answer would depend on how far someone had
scrolled — the agent filter had exactly that bug before this.

Filtered on `started_at`, the typed timestamp the query already orders
by (§0.79). `ts` is a display string with no ordering a SQL comparison
could trust.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app
from app.models import Agent, Trace

client = TestClient(app)

PREFIX = f"trf{uuid.uuid4().hex[:8]}"
# Fixed, far enough back that nothing else in the database collides.
DAY = datetime(2026, 3, 10, 12, 0, 0)


def _seed():
    with Session(engine) as s:
        a = Agent(id=f"{PREFIX}a", name=f"{PREFIX} agent", type="App",
                  subtype="Workflow", plane="control", owner="tests",
                  status="draft", version="v1", health_status="—",
                  flow={"nodes": [], "edges": []}, metrics={})
        b = Agent(id=f"{PREFIX}b", name=f"{PREFIX} other", type="App",
                  subtype="Workflow", plane="control", owner="tests",
                  status="draft", version="v1", health_status="—",
                  flow={"nodes": [], "edges": []}, metrics={})
        s.add(a); s.add(b)
        for i, offset in enumerate([-2, -1, 0, 1]):      # four consecutive days
            s.add(Trace(id=f"{PREFIX}-t{i}", agent_id=a.id, agent_name=a.name,
                        version="v1", principal="user:test", plane="App", spans=1,
                        spans_json=[], status="ok",
                        started_at=DAY + timedelta(days=offset)))
        s.add(Trace(id=f"{PREFIX}-other", agent_id=b.id, agent_name=b.name,
                    version="v1", principal="user:test", plane="App", spans=1,
                    spans_json=[], status="ok", started_at=DAY))
        s.commit()


def _cleanup():
    with Session(engine) as s:
        s.exec(text("DELETE FROM trace WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.exec(text("DELETE FROM agent WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.commit()


def _ids(**params):
    params.setdefault("agent_id", f"{PREFIX}a")
    q = "&".join(f"{k}={v}" for k, v in params.items() if v)
    r = client.get(f"/api/traces?{q}")
    assert r.status_code == 200, r.text
    return {t["id"] for t in r.json()}


def test_from_date_is_inclusive_of_that_day():
    _seed()
    try:
        got = _ids(from_date=DAY.date().isoformat())
        assert f"{PREFIX}-t2" in got, "the day itself must be included"
        assert f"{PREFIX}-t3" in got
        assert f"{PREFIX}-t0" not in got
        assert f"{PREFIX}-t1" not in got
    finally:
        _cleanup()


def test_to_date_includes_the_whole_day_not_just_midnight():
    """A bare date compared with <= midnight would exclude everything
    that happened during that day, which is not what picking it means."""
    _seed()
    try:
        got = _ids(to_date=DAY.date().isoformat())
        assert f"{PREFIX}-t2" in got, "a trace at midday on the To date must be kept"
        assert f"{PREFIX}-t3" not in got
    finally:
        _cleanup()


def test_the_same_date_in_both_boxes_means_that_day():
    """The case that would otherwise return nothing at all."""
    _seed()
    try:
        d = DAY.date().isoformat()
        assert _ids(from_date=d, to_date=d) == {f"{PREFIX}-t2"}
    finally:
        _cleanup()


def test_a_range_returns_only_what_falls_inside_it():
    _seed()
    try:
        got = _ids(from_date=(DAY - timedelta(days=1)).date().isoformat(),
                   to_date=DAY.date().isoformat())
        assert got == {f"{PREFIX}-t1", f"{PREFIX}-t2"}
    finally:
        _cleanup()


def test_dates_combine_with_the_agent_filter():
    """Both narrow together; neither silently replaces the other."""
    _seed()
    try:
        d = DAY.date().isoformat()
        assert _ids(agent_id=f"{PREFIX}b", from_date=d, to_date=d) == {f"{PREFIX}-other"}
        assert f"{PREFIX}-other" not in _ids(from_date=d, to_date=d)
    finally:
        _cleanup()


def test_a_full_timestamp_is_accepted_not_just_a_date():
    _seed()
    try:
        got = _ids(from_date=(DAY + timedelta(hours=1)).isoformat())
        # 13:00 on the day excludes that day's midday trace, keeps later ones.
        assert f"{PREFIX}-t2" not in got
        assert f"{PREFIX}-t3" in got
    finally:
        _cleanup()


def test_no_dates_returns_everything_for_the_agent():
    _seed()
    try:
        assert len(_ids()) == 4
    finally:
        _cleanup()


def test_a_malformed_date_is_a_400_not_a_silent_full_listing():
    """Ignoring an unparseable filter would return every trace while the
    UI showed a date selected — the worst possible answer."""
    r = client.get("/api/traces?from_date=last-tuesday")
    assert r.status_code == 400
    assert "from_date" in r.json()["detail"]


def test_an_impossible_range_returns_nothing_rather_than_erroring():
    _seed()
    try:
        assert _ids(from_date="2026-03-12", to_date="2026-03-09") == set()
    finally:
        _cleanup()
