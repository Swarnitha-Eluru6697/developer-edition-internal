"""An agent can be renamed, and the rename reaches what depends on the name.

There was no way to rename an agent anywhere in the product: MetaIn
carried tags, description and input_variables, so the name was fixed at
creation by whatever was typed into the New Agent dialog. Correcting a
typo meant deleting the agent and rebuilding it.

The part worth pinning is which copies of the name follow a rename and
which deliberately do not. Four tables denormalize it:

  * Role   — CURRENT configuration. It is what the Grant/Revoke view
             labels each binding with, so a stale copy names an agent
             that no longer exists. Rewritten.
  * Run,
    Trace,
    Alert  — HISTORY. Each records what the agent was called when it
             ran. Rewriting them would retitle the past to match the
             present. Left alone.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select, text

from app.main import app
from app.db import engine
from app.models import Agent as AgentModel, Role, Run, Trace

client = TestClient(app)


def _destroy(agent_id):
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest", "role"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


@pytest.fixture
def agent():
    r = client.post("/api/agents", json={
        "name": f"Original {uuid.uuid4().hex[:6]}", "subtype": "Custom",
        "app_type": "chatbot", "plane": "App", "owner": "tests"})
    assert r.status_code == 200, r.text
    yield r.json()
    _destroy(r.json()["id"])


def _rename(agent_id, name):
    return client.patch(f"/api/agents/{agent_id}/meta", json={"name": name})


class TestRenaming:
    def test_the_name_actually_changes(self, agent):
        r = _rename(agent["id"], "Renamed agent")
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Renamed agent"
        assert client.get(f"/api/agents/{agent['id']}").json()["name"] == "Renamed agent"

    def test_whitespace_is_trimmed(self, agent):
        assert _rename(agent["id"], "   Padded   ").json()["name"] == "Padded"

    def test_a_blank_name_is_refused(self, agent):
        r = _rename(agent["id"], "   ")
        assert r.status_code == 400
        assert "needs a name" in r.json()["detail"]
        assert client.get(f"/api/agents/{agent['id']}").json()["name"] == agent["name"]

    def test_an_absurdly_long_name_is_refused(self, agent):
        r = _rename(agent["id"], "x" * 200)
        assert r.status_code == 400
        assert "too long" in r.json()["detail"]

    def test_omitting_the_name_leaves_it_alone(self, agent):
        """The field is optional — editing only the description must not
        blank the name out."""
        r = client.patch(f"/api/agents/{agent['id']}/meta",
                         json={"description": "just the description"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == agent["name"]
        assert r.json()["description"] == "just the description"

    def test_description_and_name_can_be_saved_together(self, agent):
        r = client.patch(f"/api/agents/{agent['id']}/meta",
                         json={"name": "Both at once", "description": "and a description"})
        assert r.status_code == 200, r.text
        assert (r.json()["name"], r.json()["description"]) == ("Both at once", "and a description")


class TestWhatFollowsTheRename:
    def test_role_bindings_are_relabelled(self, agent):
        """Role.agent_name is current configuration — the Grant/Revoke
        view labels each binding with it."""
        r = client.post(f"/api/agents/{agent['id']}/roles",
                        json={"group": "data-engineers", "tier": "select"})
        assert r.status_code == 200, r.text

        _rename(agent["id"], "Relabelled")
        with Session(engine) as s:
            names = {x.agent_name for x in
                     s.exec(select(Role).where(Role.agent_id == agent["id"])).all()}
        assert names == {"Relabelled"}, f"stale role labels: {names}"

    def test_history_keeps_the_name_it_ran_under(self, agent):
        """A past run is a record of what happened, including what the
        agent was called at the time."""
        with Session(engine) as s:
            s.add(Run(id=f"r-rename-{uuid.uuid4().hex[:8]}", agent_id=agent["id"],
                      agent_name=agent["name"], status="success",
                      invoking_user="user:tests", started_at="2026-01-01T00:00:00"))
            s.commit()

        _rename(agent["id"], "Renamed after the run")

        with Session(engine) as s:
            runs = s.exec(select(Run).where(Run.agent_id == agent["id"])).all()
        assert runs, "fixture run missing"
        assert all(r.agent_name == agent["name"] for r in runs), \
            "a rename rewrote history — past runs should keep the name they ran under"
