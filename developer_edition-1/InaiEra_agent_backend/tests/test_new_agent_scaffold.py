"""A new agent is scaffolded with a Trigger and a UCS check, and the check
has its own switch.

The scaffold has moved twice, so the current contract is worth stating
plainly: creation places the two nodes every flow has in common — the
Trigger, and the UCS check wired straight after it. Everything else is
dragged in. (It was once trigger -> gov -> respond, where `respond` was
a guess about a flow nobody had designed; then briefly the Trigger
alone.)

The UCS check is placed rather than dragged because it belongs to every
flow, and it carries `config["enabled"]` so it can be stood down in
place instead of deleted — no gap in the graph, and its state is visible
where the node is.

What these tests really protect is the property that makes the switch
safe to offer at all: turning the NODE off does not turn ENTITLEMENT
CHECKING off. _execute_flow runs the same check as pre-flight whenever
no enabled gov node will do it. Measured with the gate removed, an
identity belonging to no group read a governed knowledge base
successfully:

    user:demo   status=success   retrieval span: ok

A canvas switch that silently disabled authorization would be the most
damaging possible reading of "optional", so the switch changes where the
check happens and whether it is a visible step — never whether it runs.
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
    """A genuinely fresh agent, created the way the product creates one.

    Deleted afterwards on purpose: these tests create agents through the
    real API against the real database, and a suite that leaves them
    behind puts its fixtures in the operator's own agent list.
    """
    r = client.post("/api/agents", json={
        "name": f"Scaffold test {uuid.uuid4().hex[:6]}", "subtype": "Custom",
        "app_type": "chatbot", "plane": "App", "owner": "tests"})
    assert r.status_code == 200, r.text
    a = r.json()
    yield a
    _destroy(a["id"])


def _flow(agent_id):
    r = client.get(f"/api/agents/{agent_id}")
    assert r.status_code == 200, r.text
    return r.json()["flow"]


def _gov_id(agent_id):
    return next(n["id"] for n in _flow(agent_id)["nodes"] if n["type"] == "gov")


def _add(agent_id, ntype):
    r = client.post(f"/api/agents/{agent_id}/nodes", json={"type": ntype, "x": 400, "y": 140})
    assert r.status_code == 200, r.text
    return [n["id"] for n in r.json()["flow"]["nodes"] if n["type"] == ntype][-1]


class TestTheScaffold:
    def test_a_new_agent_has_a_trigger_and_a_ucs_check(self, agent):
        types = [n["type"] for n in agent["flow"]["nodes"]]
        assert types == ["trigger", "gov"], f"unexpected scaffold: {types}"

    def test_the_check_is_wired_to_the_trigger(self, agent):
        nodes = {n["id"]: n["type"] for n in agent["flow"]["nodes"]}
        edges = [(nodes[e["source"]], nodes[e["target"]]) for e in agent["flow"]["edges"]]
        assert edges == [("trigger", "gov")]

    def test_the_check_arrives_switched_on(self, agent):
        gov = next(n for n in agent["flow"]["nodes"] if n["type"] == "gov")
        assert gov["config"].get("enabled") is True

    def test_nothing_else_is_scaffolded(self, agent):
        """`respond` in particular: it is a palette node now, and placing
        one was a guess about a flow that did not exist yet."""
        types = [n["type"] for n in agent["flow"]["nodes"]]
        assert "respond" not in types
        assert "llm" not in types

    def test_a_snippet_still_opens_completely_empty(self):
        r = client.post("/api/agents", json={
            "name": f"Scaffold snippet {uuid.uuid4().hex[:6]}", "subtype": "Custom",
            "app_type": "chatbot", "plane": "Snippet", "owner": "tests"})
        assert r.status_code == 200, r.text
        a = r.json()
        try:
            assert a["flow"]["nodes"] == []
            assert a["flow"]["edges"] == []
        finally:
            _destroy(a["id"])


class TestTheSwitchOnTheNode:
    def test_switching_it_off_leaves_the_node_in_place(self, agent):
        """Stood down, not deleted — deleting it would leave a gap in the
        graph and lose the person's own placement."""
        gov = _gov_id(agent["id"])
        r = client.patch(f"/api/agents/{agent['id']}/nodes/{gov}/config",
                         json={"config": {"enabled": False}})
        assert r.status_code == 200, r.text
        flow = r.json()["flow"]
        node = next(n for n in flow["nodes"] if n["id"] == gov)
        assert node["config"]["enabled"] is False
        nodes = {n["id"]: n["type"] for n in flow["nodes"]}
        assert [(nodes[e["source"]], nodes[e["target"]]) for e in flow["edges"]] == [("trigger", "gov")]

    def test_it_can_be_switched_back_on(self, agent):
        gov = _gov_id(agent["id"])
        client.patch(f"/api/agents/{agent['id']}/nodes/{gov}/config", json={"config": {"enabled": False}})
        r = client.patch(f"/api/agents/{agent['id']}/nodes/{gov}/config", json={"config": {"enabled": True}})
        assert next(n for n in r.json()["flow"]["nodes"] if n["id"] == gov)["config"]["enabled"] is True


DENIAL = {"object": "x", "tier_requested": "select", "agent_principal_ok": False,
          "agent_reason": "no matching grant", "invoking_user_ok": False,
          "user_reason": "no matching grant", "resolved": False}

ALLOWED = {"object": "x", "tier_requested": "select", "agent_principal_ok": True,
           "agent_reason": "granted", "invoking_user_ok": True,
           "user_reason": "granted", "resolved": True}


class TestEntitlementIsCheckedEitherWay:
    @pytest.fixture
    def governed_kb(self):
        kb_id = f"scaffold_gate_kb_{uuid.uuid4().hex[:6]}"
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
        client.post(f"/api/agents/{agent_id}/edges", json={"source": _gov_id(agent_id), "target": rid})

    def _run(self, agent_id, who):
        return client.post(f"/api/agents/{agent_id}/run",
                           json={"input": "x", "invoking_user": who, "preview": True})

    def test_a_denial_blocks_the_run_with_the_switch_on(self, agent, governed_kb):
        self._wire_governed_retrieval(agent["id"], governed_kb)
        with patch("app.harness.check_entitlement", return_value=DENIAL):
            r = self._run(agent["id"], "user:demo")
        assert r.status_code == 200, r.text
        assert (r.json()["status"], r.json()["fail_class"]) == ("denied", "ucs")

    def test_a_denial_still_blocks_the_run_with_the_switch_off(self, agent, governed_kb):
        """The property that makes the switch safe. With the node stood
        down the check runs as pre-flight, so an unentitled identity is
        still refused."""
        self._wire_governed_retrieval(agent["id"], governed_kb)
        gov = _gov_id(agent["id"])
        client.patch(f"/api/agents/{agent['id']}/nodes/{gov}/config", json={"config": {"enabled": False}})

        with patch("app.harness.check_entitlement", return_value=DENIAL) as checked:
            r = self._run(agent["id"], "user:demo")
        assert r.status_code == 200, r.text
        assert checked.called, "no entitlement check ran with the node switched off"
        assert (r.json()["status"], r.json()["fail_class"]) == ("denied", "ucs")

    def test_the_check_node_cannot_be_deleted_at_all(self, agent):
        """It is placed on every canvas like the Trigger, so it is not the
        person's to remove — the switch is how you stand it down, which
        also means it cannot be deleted as a way around the gate."""
        r = client.delete(f"/api/agents/{agent['id']}/nodes/{_gov_id(agent['id'])}")
        assert r.status_code == 400
        assert "cannot be deleted" in r.json()["detail"]
        assert "Switch it off" in r.json()["detail"]

    def test_an_entitled_identity_is_not_blocked_by_the_gate(self, agent, governed_kb):
        """The gate must deny for lack of a grant, not for existing."""
        self._wire_governed_retrieval(agent["id"], governed_kb)
        with patch("app.harness.check_entitlement", return_value=ALLOWED):
            r = self._run(agent["id"], "user:alice")
        assert r.json()["status"] != "denied"

    def test_the_switched_off_node_is_not_checked_twice(self, agent, governed_kb):
        """Pre-flight does the work; the node must not repeat it."""
        self._wire_governed_retrieval(agent["id"], governed_kb)
        gov = _gov_id(agent["id"])
        client.patch(f"/api/agents/{agent['id']}/nodes/{gov}/config", json={"config": {"enabled": False}})
        with patch("app.harness.check_entitlement", return_value=ALLOWED) as checked:
            self._run(agent["id"], "user:alice")
        calls = [c for c in checked.call_args_list if governed_kb in str(c)]
        assert len(calls) == 1, f"expected one check, saw {len(calls)}"

    def test_the_switched_on_node_is_the_only_checker(self, agent, governed_kb):
        self._wire_governed_retrieval(agent["id"], governed_kb)
        with patch("app.harness.check_entitlement", return_value=ALLOWED) as checked:
            self._run(agent["id"], "user:alice")
        calls = [c for c in checked.call_args_list if governed_kb in str(c)]
        assert len(calls) == 1, f"expected one check, saw {len(calls)}"


class TestTheTraceSaysWhichHappened:
    def test_a_stood_down_node_reports_skipped_not_ok(self, agent):
        """"The gate passed" and "the gate stood aside" are different
        facts when reading back a denial, so the span has to distinguish
        them rather than showing a green tick for no work."""
        gov = _gov_id(agent["id"])
        client.patch(f"/api/agents/{agent['id']}/nodes/{gov}/config", json={"config": {"enabled": False}})
        r = client.post(f"/api/agents/{agent['id']}/run",
                        json={"input": "x", "invoking_user": "user:alice", "preview": True})
        assert r.status_code == 200, r.text
        gov_spans = [s for s in r.json()["spans"] if s.get("node") == "gov"]
        assert gov_spans, "the node still runs, so it should still have a span"
        assert gov_spans[0]["status"] == "skipped"

    def test_an_enabled_node_reports_ok(self, agent):
        r = client.post(f"/api/agents/{agent['id']}/run",
                        json={"input": "x", "invoking_user": "user:alice", "preview": True})
        gov_spans = [s for s in r.json()["spans"] if s.get("node") == "gov"]
        assert gov_spans and gov_spans[0]["status"] == "ok"


class TestNodesSomeoneAddedCanBeRemoved:
    def test_an_output_node_can_be_deleted(self, agent):
        """It was refused as "fixed" while creation always placed one and
        the palette had no entry. It is a palette node now, so a node you
        can add has to be one you can remove."""
        rid = _add(agent["id"], "respond")
        r = client.delete(f"/api/agents/{agent['id']}/nodes/{rid}")
        assert r.status_code == 200, r.text
        assert not any(n["id"] == rid for n in r.json()["flow"]["nodes"])

    def test_the_trigger_still_cannot_be_deleted(self, agent):
        r = client.delete(f"/api/agents/{agent['id']}/nodes/n1")
        assert r.status_code == 400
        assert "cannot be deleted" in r.json()["detail"]


class TestNodesSomeoneAddedCanBeMoved:
    def test_an_output_node_can_be_repositioned(self, agent):
        rid = _add(agent["id"], "respond")
        r = client.patch(f"/api/agents/{agent['id']}/nodes/{rid}/position", json={"x": 500, "y": 300})
        assert r.status_code == 200, r.text
        moved = next(n for n in r.json()["flow"]["nodes"] if n["id"] == rid)
        assert (moved["x"], moved["y"]) == (500, 300)

    def test_the_ucs_check_node_can_be_repositioned(self, agent):
        r = client.patch(f"/api/agents/{agent['id']}/nodes/{_gov_id(agent['id'])}/position",
                         json={"x": 420, "y": 260})
        assert r.status_code == 200, r.text

    def test_the_trigger_is_still_fixed(self, agent):
        r = client.patch(f"/api/agents/{agent['id']}/nodes/n1/position", json={"x": 400, "y": 400})
        assert r.status_code == 400
        assert "cannot be moved" in r.json()["detail"]
