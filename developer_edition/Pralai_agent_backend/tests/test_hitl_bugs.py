"""
Real tests for three HITL-related bugs (section 0.74, GAP_REPORT).

Bug 1 - weak HITL response validation: respond_to_hitl never checked
body.action_id against this request's own real, configured actions,
nor validated submitted fields against the configured form schema.

Bug 2 - a real race condition: read HitlRequest(status=pending) ->
resume -> set status=resolved -> commit left a real window where two
concurrent requests could both observe "pending". Fixed with a real
atomic claim plus real idempotency-key support for a repeated
frontend/network retry.

Bug 3 - batch HITL status persisted as "success": a paused row was
silently relabeled "success", and no per-row Trace was ever created
despite the endpoint's own docstring claiming one.

These tests exercise the real, running API and real harness functions
directly - not mocked, except where genuine infrastructure (a running
Temporal server) isn't available in this environment.
"""
import sys
import os
import uuid

# Maintenance operations moved off the product API surface behind the
# /internal prefix and an operator credential (app/internal_auth.py).
# These tests exercise the operation, not the boundary — that is
# test_maintenance_boundary.py — so they present a token, which is what
# a real operator or CronJob does.
MAINTENANCE_HEADERS = {"X-Maintenance-Token": "test-operator-token"}


def _as_operator(monkeypatch=None):
    import os
    os.environ["MAINTENANCE_TOKEN"] = "test-operator-token"
    return MAINTENANCE_HEADERS


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import threading
import httpx
from datetime import datetime, timedelta
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app
from app.harness import run_flow_sync, resume_flow_sync, resume_delegated_subagent_flow, _resolve_paused_flow
from app.models import Agent, AgentVersion, Run, HitlRequest

client = TestClient(app)

HITL_FLOW = {
    "nodes": [
        {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
        {"id": "n2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
            "delivery_methods": ["web_app"], "form_content": "Please review",
            "fields": [{"name": "notes", "type": "select", "options": ["yes", "no"]}],
            "actions": [{"id": "approve", "title": "Approve"}, {"id": "reject", "title": "Reject"}],
        }},
        {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
        {"id": "n4", "type": "respond", "x": 200, "y": 100, "sub": "", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
        {"id": "e2", "source": "n2", "target": "n3", "source_handle": "approve"},
        {"id": "e3", "source": "n2", "target": "n4", "source_handle": "reject"},
    ],
}


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        for table in ("hitlrequest", "run", "trace", "agentlog", "agentversion"):
            s.exec(text(f"DELETE FROM {table} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


def make_hitl_agent() -> str:
    r = client.post("/api/agents", json={
        "name": f"HITL Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
        "template_flow": HITL_FLOW,
    })
    return r.json()["id"]


def pause_and_create_request(agent_id: str, invoking_user: str = "user:test") -> str:
    with Session(engine) as s:
        agent = s.get(Agent, agent_id)
        result = run_flow_sync(s, agent, invoking_user, "hello")
        assert result["status"] == "paused_for_human_input"
        request_id = f"hitl-{uuid.uuid4().hex[:10]}"
        s.add(HitlRequest(
            id=request_id, agent_id=agent_id, node_id=result["hitl_node_id"], invoking_user=invoking_user,
            rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
            actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
            ctx_snapshot=result.get("hitl_ctx_snapshot") or {},
        ))
        s.commit()
        return request_id


class TestRealHitlValidation:
    def test_an_unknown_action_id_is_rejected_with_422(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                             json={"action_id": "totally_bogus_action", "field_values": {"notes": "yes"}})
            assert r.status_code == 422
            assert "approve" in r.json()["detail"]["errors"][0]
        finally:
            cleanup_agent(agent_id)

    def test_a_missing_required_field_is_rejected_with_422(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                             json={"action_id": "approve", "field_values": {}})
            assert r.status_code == 422
            assert "notes" in r.json()["detail"]["errors"][0]
        finally:
            cleanup_agent(agent_id)

    def test_an_invalid_select_option_is_rejected_with_422(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                             json={"action_id": "approve", "field_values": {"notes": "not_a_real_option"}})
            assert r.status_code == 422
        finally:
            cleanup_agent(agent_id)

    def test_a_genuinely_valid_response_succeeds(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                             json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "success"
        finally:
            cleanup_agent(agent_id)


class TestRealHitlAtomicClaim:
    def test_ten_genuinely_concurrent_responses_produce_exactly_one_success(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            results = []

            def respond():
                r = httpx.post(f"http://127.0.0.1:8000/api/agents/{agent_id}/hitl/{request_id}/respond",
                                json={"action_id": "approve", "field_values": {"notes": "yes"}}, timeout=15)
                results.append(r.status_code)

            threads = [threading.Thread(target=respond) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert results.count(200) == 1
            assert results.count(409) == 9
        finally:
            cleanup_agent(agent_id)

    def test_a_second_response_after_the_first_resolves_is_a_clean_409(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r1 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                              json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r1.status_code == 200
            r2 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                              json={"action_id": "reject", "field_values": {"notes": "no"}})
            assert r2.status_code == 409
        finally:
            cleanup_agent(agent_id)


class TestRealHitlIdempotency:
    def test_a_retry_with_the_same_idempotency_key_replays_the_cached_result(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r1 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                              json={"action_id": "reject", "field_values": {"notes": "no"}, "idempotency_key": "retry-key-abc"})
            assert r1.status_code == 200
            first = r1.json()

            r2 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                              json={"action_id": "reject", "field_values": {"notes": "no"}, "idempotency_key": "retry-key-abc"})
            assert r2.status_code == 200
            assert r2.json()["run_id"] == first["run_id"]
            assert r2.json()["trace_id"] == first["trace_id"]

            with Session(engine) as s:
                count = s.exec(text("SELECT count(*) FROM run WHERE agent_id = :a"), params={"a": agent_id}).first()
                assert count[0] == 1
        finally:
            cleanup_agent(agent_id)

    def test_a_retry_without_the_idempotency_key_is_a_plain_409_not_a_replay(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            r1 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                              json={"action_id": "approve", "field_values": {"notes": "yes"}, "idempotency_key": "key-1"})
            assert r1.status_code == 200
            r2 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond",
                              json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r2.status_code == 409
        finally:
            cleanup_agent(agent_id)


class TestRealBatchStatusAndTrace:
    def test_every_batch_row_gets_its_own_real_run_and_trace(self):
        r = client.post("/api/agents", json={
            "name": f"Batch Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "output", "x": 100, "y": 0, "sub": "", "config": {"variables": [{"name": "result"}]}},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                ],
            },
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/batch-run", json={
                "rows": [{"input": "row one"}, {"input": "row two"}], "invoking_user": "user:test",
            })
            assert r2.status_code == 200, r2.text
            body = r2.json()
            assert body["succeeded"] == 2

            with Session(engine) as s:
                run_count = s.exec(text("SELECT count(*) FROM run WHERE agent_id = :a"), params={"a": agent_id}).first()
                trace_count = s.exec(text("SELECT count(*) FROM trace WHERE agent_id = :a"), params={"a": agent_id}).first()
                assert run_count[0] == 2
                assert trace_count[0] == 2
                spans = s.exec(text("SELECT spans FROM trace WHERE agent_id = :a"), params={"a": agent_id}).all()
                assert all(sp[0] == 3 for sp in spans)
        finally:
            cleanup_agent(agent_id)

    def test_a_hitl_agent_is_rejected_upfront_by_batch_rather_than_creating_an_orphaned_pause(self):
        """§0.98, GAP_REPORT — a real, reported gap this closes: a
        batch row that genuinely paused for human input used to
        proceed anyway, start a real, separate, durable Temporal
        workflow, and report "paused_for_human_input" in the results
        table — but nothing in this reference implementation could
        ever resume it, so it sat as real, orphaned Temporal state
        forever. The whole batch call is now rejected upfront, before
        any row (and any real Temporal workflow) ever starts, for any
        agent whose own resolved flow has a Human Input node at all -
        no Run/Trace of any kind is ever created for a call rejected
        this way."""
        r = client.post("/api/agents", json={
            "name": f"Batch Pause Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": HITL_FLOW,
        })
        agent_id = r.json()["id"]
        try:
            r2 = client.post(f"/api/agents/{agent_id}/batch-run", json={
                "rows": [{"input": "row one"}], "invoking_user": "user:test",
            })
            assert r2.status_code == 400, r2.text
            assert "Human Input node" in r2.json()["detail"]

            with Session(engine) as s:
                run_count = s.exec(text("SELECT count(*) FROM run WHERE agent_id = :a"), params={"a": agent_id}).first()
                assert run_count[0] == 0
        finally:
            cleanup_agent(agent_id)


VERSIONED_HITL_FLOW_TEMPLATE = {
    "nodes": [
        {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
        {"id": "n2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
            "delivery_methods": ["web_app"], "form_content": "Please review",
            "fields": [], "actions": [{"id": "approve", "title": "Approve"}],
        }},
        {"id": "n3", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "V1_OUTPUT"}},
    ],
    "edges": [
        {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
        {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
    ],
}


class TestRealResumedExecutionNeverResolvesAVersionAgain:
    """Real tests for section 0.94, GAP_REPORT — the actual, reported
    gap: a genuinely paused run's own eventual resume read agent.flow
    directly - the same real, live, mutable field the Builder edits -
    rather than the exact, frozen snapshot the ORIGINAL invocation
    actually resolved and started executing against. The real,
    reported scenario: a parent published at v5, its own Sub-Agent
    node pauses waiting on a real, nested child's own HITL, and while
    genuinely waiting a Builder edit changes the parent's own live
    flow - resuming would then continue using nodes that were never
    part of what was actually invoked.

    The fix chains through the real, persisted trail:
    HitlRequest.run_id (§0.91) -> Run.agent_version_id (§0.91) ->
    AgentVersion.flow - and applies the SAME principle to BOTH real
    sync-path resume functions (resume_flow_sync, the primary,
    reported gap's own broader instance; resume_delegated_subagent_
    flow, the report's own specifically-named one), not just the one
    the report cited by name.

    A real bug was found and fixed while proving this live, not just
    reading the code: the initial fix correctly resolved the frozen
    flow to find the next node to resume from, but _execute_flow
    itself (the shared traversal engine both resume functions hand
    off to) has its OWN, separate flow= override parameter - neither
    function was passing the resolved flow through to it, so
    execution continued past the first resumed node by silently
    falling back to agent.flow all over again. These tests exercise
    the FULL resume, not just the first node, so this exact gap
    couldn't have passed unnoticed."""

    def _publish_versioned_agent(self, marker: str = "V1_OUTPUT") -> tuple:
        flow = {
            "nodes": [dict(n) for n in VERSIONED_HITL_FLOW_TEMPLATE["nodes"]],
            "edges": [dict(e) for e in VERSIONED_HITL_FLOW_TEMPLATE["edges"]],
        }
        flow["nodes"][2] = {**flow["nodes"][2], "config": {"template": marker}}
        r = client.post("/api/agents", json={
            "name": f"Resumed Version Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": flow,
        })
        agent_id = r.json()["id"]
        pub = client.post(f"/api/agents/{agent_id}/publish")
        assert pub.status_code == 200, pub.text
        return agent_id, pub.json()["version"]

    def _pause_with_frozen_version(self, agent_id: str) -> tuple:
        """Real, full production setup: a real Run row pointing at the
        agent's own current AgentVersion, and a real HitlRequest
        pointing at that Run via run_id - the exact real trail §0.91
        established and this fix now reads back."""
        with Session(engine) as s:
            from sqlmodel import select
            agent = s.get(Agent, agent_id)
            version = s.exec(
                select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.is_current == True)
            ).first()
            result = run_flow_sync(s, agent, "user:test", "hi")
            assert result["status"] == "paused_for_human_input"

            run_id = f"r-test-{uuid.uuid4().hex[:8]}"
            s.add(Run(
                id=run_id, agent_id=agent_id, agent_name=agent.name, status="paused",
                duration="0.0s", cost="$0.00", hops=1, ts="00:00:00",
                agent_version_id=version.id, agent_version=version.version,
            ))
            s.commit()

            request_id = f"hitl-{uuid.uuid4().hex[:10]}"
            s.add(HitlRequest(
                id=request_id, agent_id=agent_id, node_id=result["hitl_node_id"], invoking_user="user:test",
                rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
                actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
                ctx_snapshot=result.get("hitl_ctx_snapshot") or {}, run_id=run_id,
            ))
            s.commit()
            return request_id, run_id

    def test_resume_flow_sync_uses_the_frozen_version_not_a_later_live_edit(self):
        """The primary, most commonly-hit resume path - a real,
        reported gap broader than the one instance the report named."""
        agent_id, v1 = self._publish_versioned_agent("V1_OUTPUT")
        try:
            request_id, run_id = self._pause_with_frozen_version(agent_id)

            # Real, live Builder edit + republish WHILE genuinely paused.
            edit = client.patch(f"/api/agents/{agent_id}/nodes/n3/config", json={"config": {"template": "V2_OUTPUT"}})
            assert edit.status_code == 200, edit.text
            pub2 = client.post(f"/api/agents/{agent_id}/publish")
            assert pub2.status_code == 200, pub2.text
            assert pub2.json()["version"] != v1

            with Session(engine) as s:
                agent = s.get(Agent, agent_id)
                hitl = s.get(HitlRequest, request_id)
                result = resume_flow_sync(s, agent, hitl, None, {})
            assert result["output"] == "V1_OUTPUT"
            assert result["output"] != "V2_OUTPUT"
        finally:
            cleanup_agent(agent_id)

    def test_resolve_paused_flow_falls_back_to_live_flow_for_a_row_with_no_run_id(self):
        """Real, honest fallback for a genuinely pre-existing
        HitlRequest row that predates this fix - no run_id to chain
        through, so the live agent.flow is the only real option, the
        same honest convention this codebase already uses for every
        other real, unbackfillable historical field."""
        agent_id, _ = self._publish_versioned_agent("LIVE_FALLBACK_OUTPUT")
        try:
            with Session(engine) as s:
                agent = s.get(Agent, agent_id)
                hitl = HitlRequest(
                    id=f"hitl-{uuid.uuid4().hex[:10]}", agent_id=agent_id, node_id="n2",
                    invoking_user="user:test", rendered_content="x", fields=[], actions=[],
                    delivery_status={}, ctx_snapshot={}, run_id=None,
                )
                flow = _resolve_paused_flow(s, agent, hitl)
            assert flow == agent.flow
        finally:
            cleanup_agent(agent_id)

    def test_resume_delegated_subagent_flow_uses_the_frozen_parent_version_not_a_later_live_edit(self):
        """The report's own specifically-named function - a parent's
        own Sub-Agent node pauses waiting on a real, nested child's
        own HITL; while genuinely waiting, a Builder edit changes the
        PARENT's own live flow. Resuming must continue using the
        parent's own frozen, published snapshot, not the live one."""
        child = client.post("/api/agents", json={
            "name": f"Delegated Resume Child {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": {
                "nodes": [{"id": "c1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}],
                "edges": [],
            },
        })
        child_id = child.json()["id"]
        parent_flow_v1 = {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "subagent", "x": 100, "y": 0, "sub": "", "config": {"target_agent_id": child_id}},
                {"id": "n3", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "PARENT_V1_OUTPUT {{subagent_output}}"}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
            ],
        }
        r = client.post("/api/agents", json={
            "name": f"Delegated Resume Version Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
            "template_flow": parent_flow_v1,
        })
        agent_id = r.json()["id"]
        pub = client.post(f"/api/agents/{agent_id}/publish")
        assert pub.status_code == 200, pub.text
        v1 = pub.json()["version"]
        try:
            from sqlmodel import select
            with Session(engine) as s:
                agent = s.get(Agent, agent_id)
                version = s.exec(
                    select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.is_current == True)
                ).first()

                run_id = f"r-test-{uuid.uuid4().hex[:8]}"
                s.add(Run(
                    id=run_id, agent_id=agent_id, agent_name=agent.name, status="paused",
                    duration="0.0s", cost="$0.00", hops=1, ts="00:00:00",
                    agent_version_id=version.id, agent_version=version.version,
                ))
                s.commit()

                request_id = f"hitl-{uuid.uuid4().hex[:10]}"
                s.add(HitlRequest(
                    id=request_id, agent_id=agent_id, node_id="n2", invoking_user="user:test",
                    rendered_content="child form", fields=[], actions=[],
                    delivery_status={}, ctx_snapshot={"input": "hi", "vars": {}, "output": None},
                    run_id=run_id, delegated_child_agent_id=child_id,
                ))
                s.commit()

            # Real, live Builder edit + republish WHILE the parent is
            # genuinely still waiting on its own delegated child.
            edit = client.patch(f"/api/agents/{agent_id}/nodes/n3/config", json={"config": {"template": "PARENT_V2_OUTPUT {{subagent_output}}"}})
            assert edit.status_code == 200, edit.text
            pub2 = client.post(f"/api/agents/{agent_id}/publish")
            assert pub2.status_code == 200, pub2.text
            assert pub2.json()["version"] != v1

            with Session(engine) as s:
                agent = s.get(Agent, agent_id)
                hitl = s.get(HitlRequest, request_id)
                # Real, deliberate choice: child_result carries no real
                # "output" of its own (a genuinely valid real case - a
                # child whose own last node didn't produce a final
                # answer) so the downstream template node's own render
                # is what actually determines ctx["output"] (see
                # handle_template's own real "ctx[output] or rendered"
                # convention) - the clean, unambiguous way to prove
                # WHICH version's node content actually executed,
                # rather than child_result's own output value masking
                # it (a real, separate, pre-existing convention this
                # test deliberately works with, not around).
                result = resume_delegated_subagent_flow(s, agent, hitl, {"status": "success", "output": None})
            assert result["output"] == "PARENT_V1_OUTPUT None"
            assert "PARENT_V2_OUTPUT" not in result["output"]
        finally:
            cleanup_agent(agent_id)
            cleanup_agent(child_id)


class TestRealHitlStuckResolvingRecovery:
    """Real tests for section 0.106, GAP_REPORT.

    The real, reported gap: the atomic claim (pending -> resolving)
    correctly closed the concurrent-request race (§0.74), but nothing
    ever moved a row back OUT of "resolving" if the resume attempt
    that followed then genuinely failed (Temporal temporarily
    unreachable, a workflow-signaling failure, a worker outage, a
    genuine resume exception) — confirmed directly, no recovery
    mechanism existed, and a stuck row's own request_id could never be
    legitimately retried again (status != "pending" always 409s).

    The fix: a real, bounded lease (HitlRequest.resolving_until), a
    real try/except around the actual resume attempt that rolls a row
    straight back to "pending" the instant IT catches a failure (the
    report's own faster, first recovery option), and a real, separate
    periodic reconciler (reconcile_stale_hitl_requests) that rolls
    back any row whose lease has genuinely expired — the real backstop
    for the case where the process handling a request dies entirely
    before its own except clause can even run."""

    def test_a_resume_failure_rolls_the_request_back_to_pending_not_stuck_resolving(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)

            def failing_resume(*args, **kwargs):
                raise RuntimeError("simulated Temporal/worker outage")

            with patch("app.routers.execution.resume_flow_sync", side_effect=failing_resume):
                r = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond", json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r.status_code == 503
            assert "rolled back to pending" in r.json()["detail"]

            with Session(engine) as s:
                row = s.get(HitlRequest, request_id)
                assert row.status == "pending"
                assert row.resolving_until is None
        finally:
            cleanup_agent(agent_id)

    def test_a_legitimate_retry_after_a_resume_failure_now_succeeds(self):
        """The exact, real consequence the report described: before
        this fix, a stuck "resolving" row could never be legitimately
        retried again at all (a permanent 409). This proves the full,
        real lifecycle end to end: fail once, roll back, retry, and
        genuinely succeed this time."""
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)

            def failing_resume(*args, **kwargs):
                raise RuntimeError("simulated outage")

            with patch("app.routers.execution.resume_flow_sync", side_effect=failing_resume):
                r1 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond", json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r1.status_code == 503

            r2 = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond", json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r2.status_code == 200, r2.text

            with Session(engine) as s:
                row = s.get(HitlRequest, request_id)
                assert row.status == "resolved"
        finally:
            cleanup_agent(agent_id)

    def test_a_resume_failure_also_rolls_the_associated_run_back_to_paused(self):
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            run_id = f"r-test-{uuid.uuid4().hex[:8]}"
            with Session(engine) as s:
                s.add(Run(id=run_id, agent_id=agent_id, agent_name="x", status="paused",
                           duration="0s", cost="$0.00", hops=1, ts="00:00:00"))
                hitl = s.get(HitlRequest, request_id)
                hitl.run_id = run_id
                s.add(hitl)
                s.commit()

            def failing_resume(*args, **kwargs):
                raise RuntimeError("simulated outage")

            with patch("app.routers.execution.resume_flow_sync", side_effect=failing_resume):
                r = client.post(f"/api/agents/{agent_id}/hitl/{request_id}/respond", json={"action_id": "approve", "field_values": {"notes": "yes"}})
            assert r.status_code == 503

            with Session(engine) as s:
                run_row = s.get(Run, run_id)
                assert run_row.status == "paused"
        finally:
            cleanup_agent(agent_id)

    def test_the_reconciler_rolls_back_a_row_whose_lease_has_genuinely_expired(self):
        """Direct, unit-level proof of the real reconciler mechanism —
        the actual backstop for a process that died entirely before
        its own try/except could run."""
        from app.routers.execution import reconcile_stale_hitl_requests
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            with Session(engine) as s:
                hitl = s.get(HitlRequest, request_id)
                hitl.status = "resolving"
                hitl.resolving_until = datetime.utcnow() - timedelta(seconds=30)
                s.add(hitl)
                s.commit()

                count = reconcile_stale_hitl_requests(s)
                assert count == 1

                s.refresh(hitl)
                assert hitl.status == "pending"
                assert hitl.resolving_until is None
        finally:
            cleanup_agent(agent_id)

    def test_the_reconciler_never_touches_a_row_whose_lease_is_still_valid(self):
        """A real, still-in-flight resolving row (its own lease not
        yet expired) must never be mistaken for an abandoned one — a
        genuine, in-progress resume attempt would otherwise be raced
        by the reconciler itself."""
        from app.routers.execution import reconcile_stale_hitl_requests
        agent_id = make_hitl_agent()
        try:
            request_id = pause_and_create_request(agent_id)
            with Session(engine) as s:
                hitl = s.get(HitlRequest, request_id)
                hitl.status = "resolving"
                hitl.resolving_until = datetime.utcnow() + timedelta(seconds=30)
                s.add(hitl)
                s.commit()

                count = reconcile_stale_hitl_requests(s)
                assert count == 0

                s.refresh(hitl)
                assert hitl.status == "resolving"
        finally:
            cleanup_agent(agent_id)

    def test_the_reconciler_trigger_endpoint_works(self):
        r = client.post("/internal/maintenance/hitl/reconcile-stale", headers=_as_operator())
        assert r.status_code == 200
        assert "reconciled" in r.json()
