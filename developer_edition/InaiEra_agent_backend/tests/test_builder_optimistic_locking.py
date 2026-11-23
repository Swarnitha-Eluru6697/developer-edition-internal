"""
Real tests for the Builder optimistic-locking fix (section 0.71,
GAP_REPORT).

The real bug, exactly as reported: every node/edge mutation did a
genuine read-full-flow / modify-local-copy / write-full-flow round
trip with no revision number, no ETag, no optimistic locking, and no
compare-and-swap at all - two concurrent Builder tabs editing the same
agent could each read the same flow, make different real changes, and
the second write would silently overwrite the first's, with no
detection.

The fix: a real draft_revision column, incremented by exactly 1 inside
the same real transaction as every flow write (see routers/agents.py's
own _save_flow), and a real, optional If-Match request header every
flow-mutating endpoint now checks via _check_if_match - present and
mismatched means someone else's write landed first, rejected with a
real 409 Conflict; present and matching proceeds normally; absent
entirely still works exactly as before.

These tests exercise the real, running API directly - not mocked.
"""
import sys
import os
import uuid
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app

client = TestClient(app)


@pytest.fixture()
def real_agent():
    r = client.post("/api/agents", json={
        "name": f"OCC Test Agent {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "llm", "x": 100, "y": 100, "sub": "", "config": {}},
                {"id": "n3", "type": "respond", "x": 200, "y": 200, "sub": "", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        },
    })
    agent_id = r.json()["id"]
    yield agent_id
    with Session(engine) as s:
        s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealDraftRevisionBasics:
    def test_a_new_agent_starts_at_revision_one(self, real_agent):
        r = client.get(f"/api/agents/{real_agent}")
        assert r.json()["draft_revision"] == 1

    def test_a_real_mutation_increments_the_revision_by_exactly_one(self, real_agent):
        before = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        client.patch(f"/api/agents/{real_agent}/nodes/n2/position", json={"x": 50, "y": 50})
        after = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        assert after == before + 1


class TestRealConcurrentEditScenario:
    def test_a_stale_write_is_rejected_with_409_not_silently_applied(self, real_agent):
        rev = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]

        r_a = client.patch(f"/api/agents/{real_agent}/nodes/n2/position", json={"x": 111, "y": 111},
                            headers={"If-Match": str(rev)})
        assert r_a.status_code == 200
        assert r_a.json()["draft_revision"] == rev + 1

        r_b = client.patch(f"/api/agents/{real_agent}/nodes/n3/position", json={"x": 222, "y": 222},
                            headers={"If-Match": str(rev)})
        assert r_b.status_code == 409
        assert str(rev) in r_b.json()["detail"]
        assert str(rev + 1) in r_b.json()["detail"]

        flow = client.get(f"/api/agents/{real_agent}").json()["flow"]
        n2 = next(n for n in flow["nodes"] if n["id"] == "n2")
        assert (n2["x"], n2["y"]) == (111, 111)

    def test_after_a_conflict_reloading_and_retrying_with_the_fresh_revision_succeeds(self, real_agent):
        rev = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        client.patch(f"/api/agents/{real_agent}/nodes/n2/position", json={"x": 1, "y": 1},
                     headers={"If-Match": str(rev)})

        fresh_rev = client.get(f"/api/agents/{real_agent}").json()["draft_revision"]
        r = client.patch(f"/api/agents/{real_agent}/nodes/n2/position", json={"x": 222, "y": 222},
                          headers={"If-Match": str(fresh_rev)})
        assert r.status_code == 200
        assert r.json()["draft_revision"] == fresh_rev + 1

    def test_omitting_if_match_entirely_remains_backward_compatible(self, real_agent):
        r = client.patch(f"/api/agents/{real_agent}/nodes/n2/position", json={"x": 5, "y": 5})
        assert r.status_code == 200

    def test_a_non_integer_if_match_is_a_real_honest_400_not_a_500(self, real_agent):
        r = client.patch(f"/api/agents/{real_agent}/nodes/n2/position", json={"x": 5, "y": 5},
                          headers={"If-Match": "not-a-number"})
        assert r.status_code == 400


class TestRealCoverageAcrossMutatingEndpoints:
    def test_add_node_respects_if_match(self, real_agent):
        r = client.post(f"/api/agents/{real_agent}/nodes", json={"type": "llm", "x": 0, "y": 0, "sub": "", "config": {}},
                         headers={"If-Match": "999"})
        assert r.status_code == 409

    def test_delete_node_respects_if_match(self, real_agent):
        r = client.delete(f"/api/agents/{real_agent}/nodes/n2", headers={"If-Match": "999"})
        assert r.status_code == 409

    def test_add_edge_respects_if_match(self, real_agent):
        r = client.post(f"/api/agents/{real_agent}/edges", json={"source": "n1", "target": "n3", "source_handle": None},
                         headers={"If-Match": "999"})
        assert r.status_code == 409

    def test_delete_edge_respects_if_match(self, real_agent):
        r = client.delete(f"/api/agents/{real_agent}/edges/e1", headers={"If-Match": "999"})
        assert r.status_code == 409

    def test_insert_snippet_respects_if_match(self, real_agent):
        r = client.post(f"/api/agents/{real_agent}/insert-snippet", json={"snippet_id": "nonexistent", "x": 0, "y": 0},
                         headers={"If-Match": "999"})
        assert r.status_code == 409


class TestRealGenuineConcurrentRace:
    """Real tests for section 0.88, GAP_REPORT — the actual, reported
    gap in the ORIGINAL §0.71 fix: _check_if_match's own plain Python
    comparison, followed by a separate a.draft_revision += 1;
    session.commit(), left a real window where two genuinely
    concurrent requests could both read the same draft_revision and
    both pass the same check before either write landed — "last writer
    wins," with no detection. TestRealConcurrentEditScenario above
    only ever tested a STALE request (A fully completes, THEN B is
    attempted) — genuinely different from two transactions racing the
    SAME check at the same time, which is what these tests exercise
    for real, against the real, live running server (TestClient's own
    in-process ASGI dispatch doesn't genuinely exercise concurrent
    database transactions the way real, separate httpx connections to
    a real, running server do — the same real reason
    test_hitl_bugs.py's own TestRealHitlAtomicClaim needs a real,
    live server too, not just this file's own existing TestClient)."""

    def test_ten_genuinely_concurrent_writes_against_the_same_revision_produce_exactly_one_success(self):
        r = client.post("/api/agents", json={
            "name": f"CAS Race Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {"nodes": [{"id": "t1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
        })
        agent_id = r.json()["id"]
        rev = r.json()["draft_revision"]
        try:
            results = []

            def add_node(i):
                r = httpx.post(
                    f"http://127.0.0.1:8000/api/agents/{agent_id}/nodes",
                    json={"type": "template", "x": i, "y": 0, "sub": "", "config": {}},
                    headers={"If-Match": str(rev)}, timeout=15,
                )
                results.append(r.status_code)

            threads = [threading.Thread(target=add_node, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert results.count(200) == 1
            assert results.count(409) == 9

            # Real, direct proof the write itself wasn't corrupted by
            # the real race - exactly one real new node landed, the
            # real revision counter reflects exactly one real
            # increment, not ten, and not zero.
            final = client.get(f"/api/agents/{agent_id}").json()
            assert final["draft_revision"] == rev + 1
            assert len(final["flow"]["nodes"]) == 2
        finally:
            with Session(engine) as s:
                s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent_id})
                s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
                s.commit()

    def test_atomic_save_agent_directly_confirms_zero_rows_updated_on_a_stale_expected_revision(self):
        from app.routers.agents import _atomic_save_agent
        from app.models import Agent
        from fastapi import HTTPException

        r = client.post("/api/agents", json={
            "name": f"CAS Direct Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {"nodes": [{"id": "t1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
        })
        agent_id = r.json()["id"]
        try:
            with Session(engine) as s:
                with pytest.raises(HTTPException) as exc_info:
                    _atomic_save_agent(s, agent_id, 999, flow={"nodes": [], "edges": []})
                assert exc_info.value.status_code == 409

            # The real, stale attempt must not have mutated anything.
            with Session(engine) as s:
                a = s.get(Agent, agent_id)
                assert a.draft_revision == 1
                assert len(a.flow["nodes"]) == 1
        finally:
            with Session(engine) as s:
                s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent_id})
                s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
                s.commit()
