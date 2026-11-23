"""
Monitoring's "Filter by agent" dropdown returned nothing real.

GET /api/monitoring/metrics had two branches. The unscoped one computed
every metric from real Run rows. The scoped one returned the
`Agent.metrics` JSON column — which nothing in the codebase ever writes
outside seed.py's hardcoded AGENT_METRICS fixture. So picking an agent
showed fabricated numbers for seeded demo agents (a2..a8) and a dash for
every metric on every agent a user actually created, even when that
agent had hundreds of real runs.

Same class of bug §0.77 fixed for the platform-wide numbers; the scoped
branch was left behind. These tests pin the scoped branch to real,
computed, per-agent arithmetic.
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
from app.models import Agent, Run

client = TestClient(app)

PREFIX = f"scopetest{uuid.uuid4().hex[:8]}"


def _mk_agent(agent_id, name):
    return Agent(
        id=agent_id, name=name, type="App", subtype="Workflow", plane="control",
        owner="tests", status="draft", version="v1",
        health_status="—", flow={"nodes": [], "edges": []}, metrics={},
    )


def _mk_run(agent_id, i, *, status="success", duration_ms=1000, cost=0.01, fail_class=None):
    return Run(
        id=f"{PREFIX}-{agent_id}-{i}",
        agent_id=agent_id,
        agent_name=agent_id,
        status=status,
        duration_ms=duration_ms,
        cost_usd=cost,
        fail_class=fail_class,
        created_at=datetime.utcnow() - timedelta(hours=1),
    )


def _seed():
    """Two agents with deliberately different run profiles, so a scoped
    answer cannot accidentally match the other agent's or the platform's."""
    with Session(engine) as s:
        a = _mk_agent(f"{PREFIX}a", f"{PREFIX} A")
        b = _mk_agent(f"{PREFIX}b", f"{PREFIX} B")
        s.add(a); s.add(b)
        # A: 10 runs, 8 success, 1 harness fail, 1 denied, 2s each, $0.05
        for i in range(8):
            s.add(_mk_run(a.id, i, duration_ms=2000, cost=0.05))
        s.add(_mk_run(a.id, 8, status="failed", fail_class="harness", duration_ms=2000, cost=0.05))
        s.add(_mk_run(a.id, 9, status="denied", duration_ms=2000, cost=0.05))
        # B: 4 runs, all success, 6s each, $0.90
        for i in range(4):
            s.add(_mk_run(b.id, i, duration_ms=6000, cost=0.90))
        s.commit()
        return a.id, b.id


def _cleanup():
    with Session(engine) as s:
        s.exec(text("DELETE FROM run WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.exec(text("DELETE FROM agent WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.commit()


def test_scoped_metrics_are_computed_from_that_agent_s_own_runs():
    a_id, b_id = _seed()
    try:
        ma = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()["metrics"]
        mb = client.get(f"/api/monitoring/metrics?agent_id={b_id}").json()["metrics"]

        # Agent A: 10 runs, 8 success, 1 harness, 1 denied.
        assert ma["sample_size"] == 10
        assert ma["success"] == 80.0
        assert ma["stepLimit"] == 10.0
        assert ma["entPass"] == 90.0
        assert ma["p50"] == "2.0s"
        assert ma["costAvg"] == "$0.0500"

        # Agent B: a different profile entirely, so neither answer can
        # be the platform total or the other agent's.
        assert mb["sample_size"] == 4
        assert mb["success"] == 100.0
        assert mb["p50"] == "6.0s"
        assert mb["costAvg"] == "$0.9000"
    finally:
        _cleanup()


def test_the_dead_metrics_column_is_no_longer_what_gets_served():
    """The regression: a hand-written Agent.metrics value used to be
    echoed back verbatim. It must now be ignored in favour of real runs."""
    a_id, _ = _seed()
    try:
        with Session(engine) as s:
            row = s.get(Agent, a_id)
            row.metrics = {"success": "99.9%", "p50": "0.1s", "costAvg": "$1.234"}
            s.add(row)
            s.commit()
        m = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()["metrics"]
        assert m["success"] == 80.0, "the stored column leaked back into the response"
        assert m["p50"] == "2.0s"
        assert m["costAvg"] == "$0.0500"
    finally:
        _cleanup()


def test_an_agent_with_no_runs_reports_an_honest_empty_window():
    with Session(engine) as s:
        a = _mk_agent(f"{PREFIX}z", f"{PREFIX} Z")
        s.add(a); s.commit()
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={PREFIX}z").json()["metrics"]
        assert m["sample_size"] == 0
        assert m["window_days"] == 7
        # Nulls, not a fabricated 0% or a division-by-zero placeholder.
        for key in ("success", "p50", "p95", "entPass", "costAvg"):
            assert m[key] is None, key
    finally:
        _cleanup()


def test_scope_and_agent_name_are_reported_for_the_header_chip():
    a_id, _ = _seed()
    try:
        body = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()
        assert body["scope"] == a_id
        assert body["agent_name"] == f"{PREFIX} A"
    finally:
        _cleanup()


def test_scoped_totals_never_exceed_the_platform_total():
    a_id, _ = _seed()
    try:
        scoped = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()["metrics"]
        platform = client.get("/api/monitoring/metrics").json()["metrics"]
        assert scoped["sample_size"] <= platform["sample_size"]
        assert scoped["window_days"] == platform["window_days"]
    finally:
        _cleanup()


def test_unknown_agent_is_a_404_not_an_empty_success():
    """A deleted or mistyped id used to answer 200 with `{}`, which the
    UI renders exactly like a healthy agent that has no runs yet."""
    r = client.get("/api/monitoring/metrics?agent_id=doesnotexist000")
    assert r.status_code == 404


def test_all_is_still_the_platform_wide_scope():
    body = client.get("/api/monitoring/metrics?agent_id=all").json()
    assert body["scope"] == "platform"


# --- §0.77's four "no real data" metrics, revisited ------------------------
#
# Two of them became computable as the schema grew, and were still
# hardcoded to None long after the data existed:
#
#   chainDepth — §0.77 said "no real parent/child link between Sub-Agent
#                Run rows exists". Run.hops was added later and carries
#                real values.
#   loopIter   — §0.77 said a container span records "{node, node_id,
#                status} only, genuinely no iteration count". True of one
#                span, but a container re-executes its children, so each
#                child's span is appended once per pass.
#
# The other two are genuinely unbacked and must stay None.

def test_chain_depth_is_computed_from_real_hops():
    a_id, _ = _seed()
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()["metrics"]
        # Every seeded run has the default hops=1.
        assert m["chainDepth"] == 1.0
    finally:
        _cleanup()


def test_chain_depth_averages_multi_hop_runs():
    """A 1-hop and a 3-hop run must average to 2, not report either one."""
    with Session(engine) as s:
        a = _mk_agent(f"{PREFIX}h", f"{PREFIX} H")
        s.add(a)
        r1 = _mk_run(a.id, 0)
        r2 = _mk_run(a.id, 1)
        r2.hops = 3
        s.add(r1); s.add(r2)
        s.commit()
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={PREFIX}h").json()["metrics"]
        assert m["chainDepth"] == 2.0
    finally:
        _cleanup()


def test_chain_depth_is_null_only_when_there_are_no_runs():
    with Session(engine) as s:
        s.add(_mk_agent(f"{PREFIX}n", f"{PREFIX} N")); s.commit()
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={PREFIX}n").json()["metrics"]
        assert m["sample_size"] == 0
        assert m["chainDepth"] is None
    finally:
        _cleanup()


def test_loop_iterations_count_repeated_child_spans():
    """Two passes of a two-node loop body, then an exit — the shape a real
    persisted trace has."""
    from app.models import Trace
    spans = [
        {"node": "trigger", "node_id": "n1", "status": "ok"},
        {"node": "loop", "node_id": "lp", "status": "ok"},
        {"node": "template", "node_id": "lp1", "status": "ok"},
        {"node": "condition", "node_id": "lp2", "status": "ok"},
        {"node": "template", "node_id": "lp1", "status": "ok"},
        {"node": "condition", "node_id": "lp2", "status": "ok"},
        {"node": "exit_loop", "node_id": "lp3", "status": "ok"},
    ]
    with Session(engine) as s:
        a = _mk_agent(f"{PREFIX}l", f"{PREFIX} L")
        s.add(a)
        s.add(_mk_run(a.id, 0))
        s.add(Trace(id=f"{PREFIX}-tr-1", agent_id=a.id, agent_name=a.name,
                    version="v1", principal="user:test", plane="App", spans=len(spans),
                    spans_json=spans, status="ok",
                    started_at=datetime.utcnow() - timedelta(minutes=5)))
        s.commit()
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={PREFIX}l").json()["metrics"]
        assert m["loopIter"] == 2.0, m["loopIter"]
    finally:
        with Session(engine) as s:
            s.exec(text("DELETE FROM trace WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
            s.commit()
        _cleanup()


def test_a_run_with_no_container_is_excluded_not_counted_as_zero():
    """Averaging container-free runs in would drag a real loop's figure
    toward 0 and make it look like it barely iterates."""
    a_id, _ = _seed()   # ten runs, no traces, no containers
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()["metrics"]
        assert m["loopIter"] is None
    finally:
        _cleanup()


def test_the_two_genuinely_unbacked_metrics_stay_null():
    """retries: the only `attempts` column belongs to KBIngestionOutbox.
    cacheHit: this service has no entitlement-plan cache at all.
    Inventing either would be exactly the bug §0.77 fixed."""
    a_id, _ = _seed()
    try:
        m = client.get(f"/api/monitoring/metrics?agent_id={a_id}").json()["metrics"]
        assert m["retries"] is None
        assert m["cacheHit"] is None
    finally:
        _cleanup()
