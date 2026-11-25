"""The UCS check is a visible step you can turn off; the check itself is not.

Requested: "the UCS is optional, add a toggle — if the user wants the
UCS check after the trigger he can use that, or can turn it off and not
use UCS check after the trigger."

The node is genuinely optional, and these tests pin the part that is
easy to get wrong: turning the NODE off must not turn ENTITLEMENT
CHECKING off. _execute_flow runs the same check as pre-flight whenever a
flow has no gov node, so the toggle changes where the check happens and
whether it is visible in a trace — never whether it happens. A canvas
switch that silently disabled authorization would be the most damaging
possible reading of "optional".

The rewiring is the other half: inserting re-points every edge leaving
the Trigger, and removing reconnects the Trigger to whatever the check
fed, so a flow is never left severed by either operation.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.models import KnowledgeBase

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
        "name": f"UCS toggle {uuid.uuid4().hex[:6]}", "subtype": "Custom",
        "app_type": "chatbot", "plane": "App", "owner": "tests"})
    assert r.status_code == 200, r.text
    yield r.json()
    _destroy(r.json()["id"])


def _flow(agent_id):
    r = client.get(f"/api/agents/{agent_id}")
    assert r.status_code == 200, r.text
    return r.json()["flow"]


def _add(agent_id, ntype):
    r = client.post(f"/api/agents/{agent_id}/nodes", json={"type": ntype, "x": 260, "y": 140})
    assert r.status_code == 200, r.text
    return [n["id"] for n in r.json()["flow"]["nodes"] if n["type"] == ntype][-1]


def _edges_by_type(flow):
    t = {n["id"]: n["type"] for n in flow["nodes"]}
    return sorted((t[e["source"]], t[e["target"]]) for e in flow["edges"])


def _toggle(agent_id, enabled):
    r = client.patch(f"/api/agents/{agent_id}/ucs-check", json={"enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()


def _strip_gov(agent_id):
    """Puts the flow into the shape an agent created BEFORE the check was
    scaffolded has: no gov node at all.

    Done directly against the row because the product will not produce
    this state any more — creation places the node and delete_node
    refuses to remove it. That is the point of the endpoint under test:
    it is the way back for those older flows.
    """
    from app.models import Agent as AgentModel
    with Session(engine) as s:
        a = s.get(AgentModel, agent_id)
        gov_ids = {n["id"] for n in a.flow["nodes"] if n["type"] == "gov"}
        a.flow = {
            "nodes": [n for n in a.flow["nodes"] if n["id"] not in gov_ids],
            "edges": [e for e in a.flow["edges"]
                      if e["source"] not in gov_ids and e["target"] not in gov_ids],
        }
        s.add(a)
        s.commit()


class TestTheToggleRewiresCorrectly:
    def test_turning_it_on_inserts_the_check_between_trigger_and_the_rest(self, agent):
        _strip_gov(agent["id"])
        llm = _add(agent["id"], "llm")
        r = client.post(f"/api/agents/{agent['id']}/edges",
                        json={"source": "n1", "target": llm})
        assert r.status_code == 200, r.text
        assert _edges_by_type(_flow(agent["id"])) == [("trigger", "llm")]

        _toggle(agent["id"], True)
        assert _edges_by_type(_flow(agent["id"])) == [("gov", "llm"), ("trigger", "gov")], \
            "the check must sit between the Trigger and what the Trigger fed"

    def test_turning_it_off_reconnects_across_the_gap(self, agent):
        _strip_gov(agent["id"])
        llm = _add(agent["id"], "llm")
        client.post(f"/api/agents/{agent['id']}/edges", json={"source": "n1", "target": llm})
        _toggle(agent["id"], True)
        _toggle(agent["id"], False)

        flow = _flow(agent["id"])
        assert not any(n["type"] == "gov" for n in flow["nodes"])
        assert _edges_by_type(flow) == [("trigger", "llm")], \
            "removing the check must not leave the flow severed"

    def test_turning_it_on_twice_does_not_add_a_second_check(self, agent):
        _strip_gov(agent["id"])
        _toggle(agent["id"], True)
        _toggle(agent["id"], True)
        flow = _flow(agent["id"])
        assert len([n for n in flow["nodes"] if n["type"] == "gov"]) == 1

    def test_turning_it_off_when_absent_is_a_no_op(self, agent):
        _strip_gov(agent["id"])
        before = _flow(agent["id"])
        _toggle(agent["id"], False)
        assert _flow(agent["id"]) == before

    def test_a_new_agent_starts_with_it(self, agent):
        """Changed deliberately: the check is placed on every canvas now,
        switched on, and stood down with the switch on the node rather
        than by being absent. See test_new_agent_scaffold.py."""
        gov = [n for n in agent["flow"]["nodes"] if n["type"] == "gov"]
        assert len(gov) == 1
        assert gov[0]["config"].get("enabled") is True


class TestTurningItOffDoesNotDisableEntitlementChecking:
    @pytest.fixture
    def governed_kb(self):
        kb_id = f"ucs_toggle_kb_{uuid.uuid4().hex[:6]}"
        with Session(engine) as s:
            s.add(KnowledgeBase(id=kb_id, name=kb_id, provider="pgvector",
                                config={}, status="connected"))
            s.commit()
        yield kb_id
        with Session(engine) as s:
            s.exec(text("DELETE FROM knowledgebase WHERE id = :i"), params={"i": kb_id})
            s.commit()

    def _wire_governed_retrieval(self, agent_id, kb_id):
        rid = _add(agent_id, "retrieval")
        r = client.patch(f"/api/agents/{agent_id}/nodes/{rid}/config",
                         json={"config": {"knowledge_bases": [kb_id], "top_k": 3}})
        assert r.status_code == 200, r.text
        client.post(f"/api/agents/{agent_id}/edges", json={"source": "n1", "target": rid})

    DENIAL = {"object": "x", "tier_requested": "select", "agent_principal_ok": False,
              "agent_reason": "no matching grant", "invoking_user_ok": False,
              "user_reason": "no matching grant", "resolved": False}

    def test_a_denial_still_blocks_the_run_with_the_node_turned_off(self, agent, governed_kb):
        """The one that matters. With no gov node the check runs as
        pre-flight, so an unentitled identity is still refused."""
        self._wire_governed_retrieval(agent["id"], governed_kb)
        _toggle(agent["id"], False)
        assert not any(n["type"] == "gov" for n in _flow(agent["id"])["nodes"])

        with patch("app.harness.check_entitlement", return_value=self.DENIAL) as checked:
            r = client.post(f"/api/agents/{agent['id']}/run",
                            json={"input": "x", "invoking_user": "user:demo"})
        assert r.status_code == 200, r.text
        assert checked.called, "no entitlement check ran with the node turned off"
        assert r.json()["status"] == "denied"
        assert r.json()["fail_class"] == "ucs"

    def test_a_denial_blocks_the_run_with_the_node_turned_on_too(self, agent, governed_kb):
        self._wire_governed_retrieval(agent["id"], governed_kb)
        _toggle(agent["id"], True)

        with patch("app.harness.check_entitlement", return_value=self.DENIAL):
            r = client.post(f"/api/agents/{agent['id']}/run",
                            json={"input": "x", "invoking_user": "user:demo"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "denied"
        assert r.json()["fail_class"] == "ucs"


class TestNodesSomeoneAddedCanBeMoved:
    def test_an_output_node_can_be_repositioned(self, agent):
        """respond used to be refused by move_node as "fixed", which made
        sense only while creation always placed it and nothing could add
        one. It is a palette node now."""
        rid = _add(agent["id"], "respond")
        r = client.patch(f"/api/agents/{agent['id']}/nodes/{rid}/position",
                         json={"x": 500, "y": 300})
        assert r.status_code == 200, r.text
        moved = next(n for n in r.json()["flow"]["nodes"] if n["id"] == rid)
        assert (moved["x"], moved["y"]) == (500, 300)

    def test_a_ucs_check_node_can_be_repositioned(self, agent):
        _toggle(agent["id"], True)
        gid = next(n["id"] for n in _flow(agent["id"])["nodes"] if n["type"] == "gov")
        r = client.patch(f"/api/agents/{agent['id']}/nodes/{gid}/position",
                         json={"x": 420, "y": 260})
        assert r.status_code == 200, r.text

    def test_the_trigger_is_still_fixed(self, agent):
        """Unchanged on purpose: there is exactly one, creation places it,
        and the server has always refused to move it (§4.4)."""
        r = client.patch(f"/api/agents/{agent['id']}/nodes/n1/position",
                         json={"x": 400, "y": 400})
        assert r.status_code == 400
        assert "cannot be moved" in r.json()["detail"]
