"""
The last two hardcoded dashes on the Monitoring page.

Both shipped as literal `v="—"` in the frontend citing GAP_REPORT
§0.77 — the backend never computed either one. §0.77 was right when it
was written and wrong by the time this was read:

  Spend (7d)  — §0.77 said "every real Run.cost is unconditionally
                $0.00". §0.78 then made per-run cost real, and the
                Cost-per-run card has shown a genuine average ever
                since, so the total was computable the whole time.
                Confirmed live: 290 runs carrying $4.58 over 7 days
                while the card read "not tracked".

  Templates   — genuinely unbacked, and this fixes the cause rather
                than the symptom. `template_name` was accepted at
                creation, used to deep-copy the template's flow, then
                discarded, so nothing recorded where an agent came
                from. It is now stored on the agent.

Reported as a COUNT, not a percentage: agents created before the column
existed carry NULL, and dividing by a total that includes them would
present "hardly anything is reused" as a finding when it is an artifact
of when tracking started.
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

PREFIX = f"cost{uuid.uuid4().hex[:8]}"


def _agent(suffix, template_name=None):
    return Agent(
        id=f"{PREFIX}{suffix}", name=f"{PREFIX} {suffix}", type="App",
        subtype="Workflow", plane="control", owner="tests", status="draft",
        version="v1", health_status="—", flow={"nodes": [], "edges": []},
        metrics={}, template_name=template_name,
    )


def _run(agent_id, i, cost):
    return Run(
        id=f"{PREFIX}-{agent_id}-{i}", agent_id=agent_id, agent_name=agent_id,
        status="success", duration_ms=1000, cost_usd=cost,
        created_at=datetime.utcnow() - timedelta(hours=1),
    )


def _cleanup():
    with Session(engine) as s:
        s.exec(text("DELETE FROM run WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.exec(text("DELETE FROM agent WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.commit()


def _metrics(agent_id=None):
    q = f"?agent_id={agent_id}" if agent_id else ""
    return client.get(f"/api/monitoring/metrics{q}").json()["metrics"]


def test_spend_is_the_sum_of_real_run_costs():
    with Session(engine) as s:
        a = _agent("a")
        s.add(a)
        for i, cost in enumerate([0.25, 0.50, 0.25]):
            s.add(_run(a.id, i, cost))
        s.commit()
    try:
        m = _metrics(f"{PREFIX}a")
        assert m["costWindow"] == "$1.0000", m["costWindow"]
        # Four decimals for the same reason the average uses them: a
        # single run often costs well under a cent.
        assert m["sample_size"] == 3
    finally:
        _cleanup()


def test_spend_is_scoped_to_the_selected_agent():
    with Session(engine) as s:
        a, b = _agent("a"), _agent("b")
        s.add(a); s.add(b)
        s.add(_run(a.id, 0, 1.00))
        s.add(_run(b.id, 0, 9.00))
        s.commit()
    try:
        assert _metrics(f"{PREFIX}a")["costWindow"] == "$1.0000"
        assert _metrics(f"{PREFIX}b")["costWindow"] == "$9.0000"
    finally:
        _cleanup()


def test_spend_is_null_when_there_are_no_runs():
    """A dash for "no runs in this window" is honest; $0.0000 would
    imply the runs happened and were free."""
    with Session(engine) as s:
        s.add(_agent("z")); s.commit()
    try:
        m = _metrics(f"{PREFIX}z")
        assert m["sample_size"] == 0
        assert m["costWindow"] is None
    finally:
        _cleanup()


def test_spend_is_no_longer_hardcoded_null_platform_wide():
    """The regression: real cost data existed and the card said
    "not tracked" anyway."""
    m = _metrics()
    if m["sample_size"] == 0:
        return  # nothing ran in the window; nothing to assert
    assert m["costWindow"] is not None
    assert m["costWindow"].startswith("$")


def test_template_derived_agents_are_counted():
    with Session(engine) as s:
        s.add(_agent("t", template_name="Task-Automation"))
        s.commit()
    try:
        assert _metrics(f"{PREFIX}t")["templateAgents"] == 1
    finally:
        _cleanup()


def test_an_agent_with_no_template_is_not_counted():
    with Session(engine) as s:
        s.add(_agent("n")); s.commit()
    try:
        assert _metrics(f"{PREFIX}n")["templateAgents"] == 0
    finally:
        _cleanup()


def test_creating_from_a_template_records_the_provenance():
    """The actual fix. The name was accepted, used, and thrown away, so
    the metric had nothing to count."""
    templates = client.get("/api/templates").json()
    templates = templates if isinstance(templates, list) else templates.get("items", [])
    if not templates:
        return
    name = templates[0]["name"]
    template_id = templates[0]["id"]
    created = client.post("/api/agents", json={
        "name": f"{PREFIX} from template", "subtype": "Workflow",
        "owner": "tests", "template_id": template_id,
    })
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    try:
        assert client.get(f"/api/agents/{agent_id}").json()["template_name"] == name
    finally:
        with Session(engine) as s:
            row = s.get(Agent, agent_id)
            if row:
                s.delete(row)
                s.commit()


def test_an_agent_created_without_a_template_records_none():
    created = client.post("/api/agents", json={
        "name": f"{PREFIX} bespoke", "subtype": "Workflow", "owner": "tests",
    })
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    try:
        assert client.get(f"/api/agents/{agent_id}").json()["template_name"] is None
    finally:
        with Session(engine) as s:
            row = s.get(Agent, agent_id)
            if row:
                s.delete(row)
                s.commit()
