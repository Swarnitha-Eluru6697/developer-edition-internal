"""Saved results need real provenance and real ownership.

Three reported problems, all confirmed in the code before this:

  1. `invoking_user` was caller-supplied with a default of "user:demo",
     so every saved result looked like it belonged to the same fictional
     person and nothing tied a row to whoever actually made it.
  2. `input`, `output` and `structured_output` were caller-supplied too,
     so a client could POST a row indistinguishable from a real
     execution of an agent — with output it invented.
  3. Listing returned EVERY user's rows for the agent, and delete
     accepted any row id as long as the agent matched. Users of the same
     agent were not isolated from one another at all.

The release criterion is the third: owners must not be able to read or
delete each other's saved results. That is what most of this file tests.

One honest boundary, stated rather than implied: with no authentication
in front of this service, a caller can still PRESENT another user's name
and be treated as them. What the fix guarantees is that the same
identity governs create, list and delete consistently — so isolation is
real per identity, and a gateway-verified identity always overrides a
claimed one. Closing the remaining gap is authentication's job, not this
endpoint's.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.db import engine
from app.main import app
from app.models import Agent, Run, SavedResult, Trace
from conftest import gateway_headers

client = TestClient(app)

ALICE = "user:alice-provenance"
BOB = "user:bob-provenance"


def _as(owner):
    """Every request now carries a gateway-verified identity (deny by
    default), and that identity — not a claimed invoking_user — is the
    owner. So acting as Alice means arriving through the gateway as Alice."""
    return gateway_headers(user=owner)


@pytest.fixture
def agent_id():
    r = client.post("/api/agents", json={"name": "Saved result provenance test",
                                         "subtype": "Workflow", "app_type": "workflow"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    yield aid
    with Session(engine) as s:
        s.exec(delete(SavedResult).where(SavedResult.agent_id == aid))
        s.exec(delete(Run).where(Run.agent_id == aid))
        s.exec(delete(Trace).where(Trace.agent_id == aid))
        s.commit()
    from app.routers.agents import delete_agent
    with Session(engine) as s:
        try:
            delete_agent(aid, s)
        except Exception:
            pass


def _save(agent_id, owner, **body):
    return client.post(f"/api/agents/{agent_id}/saved-results",
                       json={"invoking_user": owner, **body}, headers=_as(owner))


def _list(agent_id, owner):
    return client.get(f"/api/agents/{agent_id}/saved-results",
                      params={"invoking_user": owner}, headers=_as(owner))


class TestCrossUserIsolation:
    """The release criterion."""

    def test_one_owner_cannot_see_anothers_saved_results(self, agent_id):
        assert _save(agent_id, ALICE, input="alice's private question").status_code == 200
        assert _save(agent_id, BOB, input="bob's private question").status_code == 200

        alice_rows = _list(agent_id, ALICE).json()
        bob_rows = _list(agent_id, BOB).json()

        assert [r["input"] for r in alice_rows] == ["alice's private question"]
        assert [r["input"] for r in bob_rows] == ["bob's private question"]
        # And specifically not each other's inputs, which is what leaked.
        assert all(r["invoking_user"] == ALICE for r in alice_rows)
        assert all(r["invoking_user"] == BOB for r in bob_rows)

    def test_one_owner_cannot_delete_anothers_saved_result(self, agent_id):
        created = _save(agent_id, ALICE, input="alice's row").json()
        row_id = created["id"]

        # Bob knows the id and tries to remove it.
        attempt = client.delete(f"/api/agents/{agent_id}/saved-results/{row_id}",
                                params={"invoking_user": BOB}, headers=_as(BOB))
        assert attempt.status_code == 404, attempt.text
        # Reported as absent, not forbidden: a 403 would confirm the row
        # exists and belongs to someone.
        assert "not found" in attempt.json()["detail"].lower()

        # Alice's row is untouched.
        assert [r["id"] for r in _list(agent_id, ALICE).json()] == [row_id]

    def test_the_owner_can_delete_their_own(self, agent_id):
        row_id = _save(agent_id, ALICE, input="alice's row").json()["id"]
        r = client.delete(f"/api/agents/{agent_id}/saved-results/{row_id}",
                          params={"invoking_user": ALICE}, headers=_as(ALICE))
        assert r.status_code == 200 and r.json() == {"deleted": True}
        assert _list(agent_id, ALICE).json() == []

    def test_listing_without_a_claim_returns_only_the_verified_callers_rows(self, agent_id):
        _save(agent_id, ALICE, input="a")
        _save(agent_id, BOB, input="b")
        r = client.get(f"/api/agents/{agent_id}/saved-results", headers=_as(ALICE))
        assert r.status_code == 200
        assert [row["input"] for row in r.json()] == ["a"]

    def test_a_claimed_owner_cannot_override_the_verified_one(self, agent_id):
        _save(agent_id, ALICE, input="alice's")
        r = client.get(f"/api/agents/{agent_id}/saved-results",
                       params={"invoking_user": ALICE}, headers=_as(BOB))
        assert r.status_code == 200 and r.json() == []

    def test_saving_without_a_claim_is_owned_by_the_verified_caller_not_user_demo(self, agent_id):
        r = client.post(f"/api/agents/{agent_id}/saved-results", json={"input": "no owner"},
                        headers=_as(ALICE))
        assert r.status_code == 200, r.text
        assert r.json()["invoking_user"] == ALICE


class TestProvenance:
    def test_a_save_without_a_run_is_recorded_as_manual(self, agent_id):
        row = _save(agent_id, ALICE, input="typed by hand").json()
        assert row["provenance"] == "manual"
        assert row["run_id"] is None

    def test_a_save_citing_a_real_run_is_recorded_as_execution(self, agent_id):
        run_id = f"r-prov-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(Run(id=run_id, agent_id=agent_id, agent_name="x", status="success",
                      duration="1s", cost="$0", hops=1, ts="12:00:00",
                      invoking_user=ALICE, user_input="the real question",
                      agent_version="v3"))
            s.commit()

        row = _save(agent_id, ALICE, run_id=run_id, input="IGNORED", output="ALSO IGNORED").json()
        assert row["provenance"] == "execution"
        assert row["run_id"] == run_id
        # Derived from the Run, not from the request.
        assert row["input"] == "the real question"
        assert row["agent_version"] == "v3"

    def test_the_output_is_read_from_the_linked_trace_not_the_request(self, agent_id):
        run_id = f"r-prov-{uuid.uuid4().hex[:8]}"
        trace_id = f"tr-prov-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            # status is constrained (ck_trace_status_enum): 'ok', not 'success'.
            s.add(Trace(id=trace_id, agent_id=agent_id, agent_name="x", version="v1",
                        principal=ALICE, plane="Control", spans=1, duration="1s",
                        ts="12:00:00", status="ok", user_input="q",
                        final_output="what the platform actually produced"))
            s.add(Run(id=run_id, agent_id=agent_id, agent_name="x", status="success",
                      duration="1s", cost="$0", hops=1, ts="12:00:00",
                      invoking_user=ALICE, user_input="q", trace_id=trace_id))
            s.commit()

        row = _save(agent_id, ALICE, run_id=run_id, output="a fabricated answer").json()
        assert row["output"] == "what the platform actually produced"
        assert row["trace_id"] == trace_id

    def test_a_client_cannot_cite_another_owners_run(self, agent_id):
        """The fabrication path: Bob knows Alice's run id and tries to
        attach a saved result to it."""
        run_id = f"r-prov-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(Run(id=run_id, agent_id=agent_id, agent_name="x", status="success",
                      duration="1s", cost="$0", hops=1, ts="12:00:00",
                      invoking_user=ALICE, user_input="alice's question"))
            s.commit()

        r = _save(agent_id, BOB, run_id=run_id, input="x")
        assert r.status_code == 404, r.text

    def test_a_client_cannot_cite_a_run_of_a_different_agent(self, agent_id):
        # A second real agent: run.agent_id has a foreign key, so the
        # test cannot invent one (and must not assume seeded demo data).
        other = client.post("/api/agents", json={"name": "Other provenance agent",
                                                  "subtype": "Workflow",
                                                  "app_type": "workflow"}).json()["id"]
        run_id = f"r-prov-{uuid.uuid4().hex[:8]}"
        with Session(engine) as s:
            s.add(Run(id=run_id, agent_id=other, agent_name="other", status="success",
                      duration="1s", cost="$0", hops=1, ts="12:00:00",
                      invoking_user=ALICE, user_input="elsewhere"))
            s.commit()
        try:
            r = _save(agent_id, ALICE, run_id=run_id, input="x")
            assert r.status_code == 404, r.text
        finally:
            with Session(engine) as s:
                s.exec(delete(Run).where(Run.agent_id == other))
                s.commit()
            from app.routers.agents import delete_agent
            with Session(engine) as s:
                try:
                    delete_agent(other, s)
                except Exception:
                    pass

    def test_a_made_up_run_id_is_refused(self, agent_id):
        assert _save(agent_id, ALICE, run_id="r-does-not-exist", input="x").status_code == 404

    def test_a_manual_save_still_needs_input(self, agent_id):
        assert _save(agent_id, ALICE).status_code == 400


class TestTheRunTraceLinkIsServerSide:
    def test_a_real_run_records_which_trace_it_produced(self, agent_id):
        """Provenance cannot rest on a client handing back a matching
        trace_id, so the server records the relationship itself."""
        with Session(engine) as s:
            agent = s.get(Agent, agent_id)
            agent.flow = {"nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ], "edges": [{"id": "e1", "source": "n1", "target": "n2"}]}
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(agent, "flow")
            s.add(agent)
            s.commit()

        r = client.post(f"/api/agents/{agent_id}/run",
                        json={"input": "hello", "invoking_user": ALICE}, headers=_as(ALICE))
        assert r.status_code == 200, r.text
        body = r.json()

        with Session(engine) as s:
            run = s.get(Run, body["run_id"])
            assert run is not None
            assert run.trace_id == body["trace_id"], "the run does not record its own trace"

    def test_saving_that_run_derives_everything_from_the_server_records(self, agent_id):
        with Session(engine) as s:
            agent = s.get(Agent, agent_id)
            agent.flow = {"nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
            ], "edges": [{"id": "e1", "source": "n1", "target": "n2"}]}
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(agent, "flow")
            s.add(agent)
            s.commit()

        run = client.post(f"/api/agents/{agent_id}/run",
                          json={"input": "the genuine input", "invoking_user": ALICE},
                          headers=_as(ALICE)).json()
        row = _save(agent_id, ALICE, run_id=run["run_id"],
                    input="not this", output="nor this").json()
        assert row["provenance"] == "execution"
        assert row["input"] == "the genuine input"
        assert row["run_id"] == run["run_id"]
        assert row["trace_id"] == run["trace_id"]
