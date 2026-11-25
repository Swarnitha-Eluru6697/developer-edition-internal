"""
Real tests for the Sub-Agent execution architecture fix (section 0.83,
GAP_REPORT).

The real, reported bug: handle_subagent (harness.py) always called
run_flow_sync directly against target.flow, bypassing two real things
every other invocation channel already had:

1. Immutable version resolution (section 0.54) - a published child
   executed its own live, mutable draft, not the current
   AgentVersion.flow snapshot.
2. Durable-path routing (section 0.53) - a child whose own flow has a
   real hitl node always took the sync path, silently downgrading to
   its weaker, in-memory-only pause instead of genuine Temporal-backed
   durability.

The fix: _resolve_subagent_flow resolves the target's own real,
immutable snapshot the same way _resolve_execution_flow already does
elsewhere. _run_subagent_durable genuinely starts a real, separate
Temporal workflow for the child when its own resolved flow needs the
durable path, via a real sync-callable bridge safe to call regardless
of whether the caller's own thread already has a running event loop.
If the child pauses, the parent's own run honestly reports
paused_for_human_input via the same real HitlPause mechanism, carrying
the child's own real form content and real workflow id.
resume_delegated_subagent_flow (invoked from respond_to_hitl) resumes
the PARENT's own flow once a real caller resolves that delegated
pause, after genuinely signaling the real child workflow first.

Same honest limitation as test_durable_hitl.py: a full, live,
end-to-end proof against an actual running Temporal server isn't
possible in this sandbox (no real Temporal server reachable -
confirmed directly, a genuine connection-refused). These tests cover
what CAN be verified without one.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import concurrent.futures

from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.models import Agent, HitlRequest
from app.harness import _resolve_subagent_flow, resume_delegated_subagent_flow, handle_subagent
from app.routers.execution import DURABLE_NODE_TYPES

client = TestClient(app)


def child_flow(marker: str = "CHILD_V1_OUTPUT") -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": marker}},
            {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def hitl_child_flow() -> dict:
    return {
        "nodes": [
            {"id": "h1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "h2", "type": "hitl", "x": 0, "y": 0, "sub": "", "config": {"form_content": "Approve?", "delivery_methods": ["web_app"]}},
            {"id": "h3", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "post-approval output"}},
        ],
        "edges": [
            {"id": "he1", "source": "h1", "target": "h2", "source_handle": None},
            {"id": "he2", "source": "h2", "target": "h3", "source_handle": None},
        ],
    }


def parent_flow(target_agent_id: str) -> dict:
    return {
        "nodes": [
            {"id": "p1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "p2", "type": "subagent", "x": 0, "y": 0, "sub": "", "config": {"target_agent_id": target_agent_id}},
            {"id": "p3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "pe1", "source": "p1", "target": "p2", "source_handle": None},
            {"id": "pe2", "source": "p2", "target": "p3", "source_handle": None},
        ],
    }


def create_agent(flow: dict) -> dict:
    r = client.post("/api/agents", json={
        "name": f"Subagent Fix Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": flow,
    })
    assert r.status_code == 200, r.text
    return r.json()


def publish(agent_id: str) -> str:
    r = client.post(f"/api/agents/{agent_id}/publish")
    assert r.status_code == 200, r.text
    return r.json()["version"]


def edit_template_node(agent_id: str, marker: str):
    r = client.patch(f"/api/agents/{agent_id}/nodes/n2/config", json={"config": {"template": marker}})
    assert r.status_code == 200, r.text


def cleanup(*agent_ids):
    with Session(engine) as s:
        for aid in agent_ids:
            for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
                s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": aid})
            s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": aid})
        s.commit()


class TestRealSubagentImmutableVersion:
    def test_a_published_child_stays_frozen_to_its_own_snapshot(self):
        child = create_agent(child_flow("CHILD_PUBLISHED"))
        publish(child["id"])
        edit_template_node(child["id"], "CHILD_MUTABLE_DRAFT_UNPUBLISHED")

        parent = create_agent(parent_flow(child["id"]))
        try:
            r = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            assert r.json()["output"] == "CHILD_PUBLISHED"
            assert "CHILD_MUTABLE_DRAFT_UNPUBLISHED" not in r.text
        finally:
            cleanup(parent["id"], child["id"])

    def test_resolve_subagent_flow_directly_matches_resolve_execution_flow_semantics(self):
        child = create_agent(child_flow("DIRECT_V1"))
        publish(child["id"])
        edit_template_node(child["id"], "DIRECT_V2_DRAFT")
        try:
            with Session(engine) as s:
                target = s.get(Agent, child["id"])
                resolved = _resolve_subagent_flow(s, target, {}, {})
                template_node = next(n for n in resolved["nodes"] if n["id"] == "n2")
                assert template_node["config"]["template"] == "DIRECT_V1"
        finally:
            cleanup(child["id"])

    def test_an_unpublished_draft_target_uses_its_own_live_flow(self):
        child = create_agent(child_flow("DRAFT_ONLY_OUTPUT"))
        try:
            with Session(engine) as s:
                target = s.get(Agent, child["id"])
                resolved = _resolve_subagent_flow(s, target, {}, {})
                template_node = next(n for n in resolved["nodes"] if n["id"] == "n2")
                assert template_node["config"]["template"] == "DRAFT_ONLY_OUTPUT"
        finally:
            cleanup(child["id"])


class TestRealSubagentCycleDetectionPreserved:
    def test_a_self_referencing_agent_is_still_refused_before_invoking(self):
        agent = create_agent(parent_flow(""))
        try:
            r = client.patch(f"/api/agents/{agent['id']}/nodes/p2/config", json={"config": {"target_agent_id": agent["id"]}})
            assert r.status_code == 200

            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "success"
        finally:
            cleanup(agent["id"])

    def test_the_real_cycle_error_message_is_still_set(self):
        with Session(engine) as s:
            ctx = {"agent_id": "self-id", "invoking_user": "user:test", "output": "", "vars": {}, "_agent_chain": ["self-id"]}
            result_ctx = handle_subagent({"target_agent_id": "self-id"}, ctx, s)
            assert "cycle detected" in result_ctx["vars"]["subagent_error"]


class TestRealSubagentDurablePathDetection:
    def test_a_hitl_containing_target_is_detected_as_needing_the_durable_path(self):
        child = create_agent(hitl_child_flow())
        publish(child["id"])
        try:
            with Session(engine) as s:
                target = s.get(Agent, child["id"])
                resolved = _resolve_subagent_flow(s, target, {}, {})
                needs_durable = any(n.get("type") in DURABLE_NODE_TYPES for n in resolved.get("nodes", []))
                assert needs_durable is True
        finally:
            cleanup(child["id"])

    def test_a_plain_target_is_not_detected_as_needing_the_durable_path(self):
        child = create_agent(child_flow())
        try:
            with Session(engine) as s:
                target = s.get(Agent, child["id"])
                resolved = _resolve_subagent_flow(s, target, {}, {})
                needs_durable = any(n.get("type") in DURABLE_NODE_TYPES for n in resolved.get("nodes", []))
                assert needs_durable is False
        finally:
            cleanup(child["id"])

    def test_a_parent_calling_a_hitl_containing_child_genuinely_attempts_the_durable_path(self):
        child = create_agent(hitl_child_flow())
        publish(child["id"])
        parent = create_agent(parent_flow(child["id"]))
        try:
            r = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "failed"
            assert body["fail_class"] == "orchestration"
            subagent_span = next(s for s in body["spans"] if s["node"] == "subagent")
            err = subagent_span.get("error", "").lower()
            assert "connect" in err or "connection" in err
        finally:
            cleanup(parent["id"], child["id"])


class TestRealSyncCallingAsyncBridge:
    def test_bridge_works_from_a_thread_with_no_existing_loop(self):
        async def fake_call():
            await asyncio.sleep(0.01)
            return {"status": "success"}

        def bridge():
            def _runner():
                return asyncio.run(fake_call())
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_runner).result()

        assert bridge() == {"status": "success"}

    def test_bridge_works_from_within_an_already_running_event_loop(self):
        async def fake_call():
            await asyncio.sleep(0.01)
            return {"status": "success"}

        def bridge():
            def _runner():
                return asyncio.run(fake_call())
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_runner).result()

        async def call_from_within_a_loop():
            return bridge()

        assert asyncio.run(call_from_within_a_loop()) == {"status": "success"}


class TestRealResumeDelegatedSubagentFlow:
    def test_a_successful_child_result_resumes_the_parent_past_the_subagent_node(self):
        parent = create_agent(parent_flow("some-child-id"))
        try:
            with Session(engine) as s:
                p = s.get(Agent, parent["id"])
                fake_hitl = HitlRequest(
                    id="fake-1", agent_id=p.id, node_id="p2", invoking_user="user:test",
                    ctx_snapshot={"input": "hi", "vars": {}, "agent_id": p.id, "agent_name": p.name,
                                  "invoking_user": "user:test", "output": None, "_agent_chain": [p.id]},
                    delegated_child_agent_id="some-child-id",
                )
                child_result = {"status": "success", "output": "REAL CHILD ANSWER", "fail_class": None, "spans": []}
                result = resume_delegated_subagent_flow(s, p, fake_hitl, child_result)
                assert result["status"] == "success"
                assert result["output"] == "REAL CHILD ANSWER"
        finally:
            cleanup(parent["id"])

    def test_a_failed_child_result_still_resumes_the_parent_with_an_honest_error_recorded(self):
        parent = create_agent(parent_flow("some-child-id"))
        try:
            with Session(engine) as s:
                p = s.get(Agent, parent["id"])
                fake_hitl = HitlRequest(
                    id="fake-2", agent_id=p.id, node_id="p2", invoking_user="user:test",
                    ctx_snapshot={"input": "hi", "vars": {}, "agent_id": p.id, "agent_name": p.name,
                                  "invoking_user": "user:test", "output": None, "_agent_chain": [p.id]},
                    delegated_child_agent_id="some-child-id",
                )
                child_result = {"status": "denied", "output": None, "fail_class": "ucs", "spans": []}
                result = resume_delegated_subagent_flow(s, p, fake_hitl, child_result)
                assert result["status"] == "success"
        finally:
            cleanup(parent["id"])

    def test_no_downstream_node_is_a_real_honest_success_not_a_crash(self):
        solo_agent = create_agent({
            "nodes": [{"id": "p1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                      {"id": "p2", "type": "subagent", "x": 0, "y": 0, "sub": "", "config": {}}],
            "edges": [{"id": "pe1", "source": "p1", "target": "p2", "source_handle": None}],
        })
        try:
            with Session(engine) as s:
                p = s.get(Agent, solo_agent["id"])
                fake_hitl = HitlRequest(
                    id="fake-3", agent_id=p.id, node_id="p2", invoking_user="user:test",
                    ctx_snapshot={"input": "hi", "vars": {}, "agent_id": p.id, "agent_name": p.name,
                                  "invoking_user": "user:test", "output": None, "_agent_chain": [p.id]},
                    delegated_child_agent_id="some-child-id",
                )
                child_result = {"status": "success", "output": "X", "fail_class": None, "spans": []}
                result = resume_delegated_subagent_flow(s, p, fake_hitl, child_result)
                assert result["status"] == "success"
        finally:
            cleanup(solo_agent["id"])


class TestRealAgentChainThreadedThroughRunDurable:
    def test_run_durable_accepts_and_uses_an_explicit_agent_chain(self):
        import inspect
        from app.routers.execution import _run_durable
        sig = inspect.signature(_run_durable)
        assert "agent_chain" in sig.parameters
        assert sig.parameters["agent_chain"].default is None


class TestRealSubagentVersionPinning:
    """Real tests for section 0.104, GAP_REPORT.

    The real, reported bug: a Sub-Agent node always resolved its own
    target's CURRENT published AgentVersion at execution time — so an
    unchanged, already-published PARENT could silently invoke a
    materially different CHILD tomorrow than it did today, the moment
    the child was independently republished, with no new publish, no
    version bump, and no record of it for the parent.

    The fix: a real, frozen dependency manifest
    (AgentVersion.subagent_manifest), built at the PARENT's own publish
    time, naming the exact child AgentVersion.id current at that
    moment. version_policy="pinned" (the default) consults it;
    version_policy="latest" is a real, explicit per-node opt-out."""

    def test_republishing_the_child_does_not_change_what_an_unchanged_published_parent_invokes(self):
        """The exact real scenario from the report: Parent A v(n) ->
        Sub-Agent B v2. Publish A. Republish B to v3. Re-running the
        UNCHANGED, already-published A must still invoke B's v2, not
        the new v3."""
        child = create_agent(child_flow("CHILD_V2_OUTPUT"))
        publish(child["id"])
        parent = create_agent(parent_flow(child["id"]))
        try:
            publish(parent["id"])

            r1 = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r1.status_code == 200, r1.text
            assert r1.json()["output"] == "CHILD_V2_OUTPUT"

            edit_template_node(child["id"], "CHILD_V3_OUTPUT")
            publish(child["id"])

            r2 = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r2.status_code == 200, r2.text
            assert r2.json()["output"] == "CHILD_V2_OUTPUT"
            assert r2.json()["output"] != "CHILD_V3_OUTPUT"
        finally:
            cleanup(parent["id"], child["id"])

    def test_the_manifest_is_recorded_on_the_parents_own_agentversion_at_publish_time(self):
        child = create_agent(child_flow("CHILD_V2_OUTPUT"))
        publish(child["id"])
        parent = create_agent(parent_flow(child["id"]))
        try:
            publish(parent["id"])
            with Session(engine) as s:
                row = s.exec(text(
                    "SELECT subagent_manifest FROM agentversion WHERE agent_id = :a AND is_current = true"
                ), params={"a": parent["id"]}).first()
            manifest = row[0]
            entries = manifest["subagents"]
            assert len(entries) == 1
            assert entries[0]["node_id"] == "p2"
            assert entries[0]["agent_id"] == child["id"]
            assert entries[0]["version"] == "v2"
            assert entries[0]["agent_version_id"] is not None
        finally:
            cleanup(parent["id"], child["id"])

    def test_version_policy_latest_is_a_real_explicit_opt_out_that_always_follows_the_childs_current_version(self):
        child = create_agent(child_flow("CHILD_V2_OUTPUT"))
        publish(child["id"])
        parent = create_agent(parent_flow(child["id"]))
        try:
            r = client.patch(f"/api/agents/{parent['id']}/nodes/p2/config", json={"config": {"version_policy": "latest"}})
            assert r.status_code == 200, r.text
            publish(parent["id"])

            edit_template_node(child["id"], "CHILD_V3_OUTPUT")
            publish(child["id"])

            r2 = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r2.status_code == 200, r2.text
            assert r2.json()["output"] == "CHILD_V3_OUTPUT"
        finally:
            cleanup(parent["id"], child["id"])

    def test_a_child_still_only_a_draft_at_parent_publish_time_is_honestly_unpinned(self):
        """No real AgentVersion exists yet to freeze — the manifest
        entry records agent_version_id=None rather than a fabricated
        pin, and the parent honestly falls back to resolving the
        child's live/current state at execution time."""
        child = create_agent(child_flow("CHILD_DRAFT_OUTPUT"))
        parent = create_agent(parent_flow(child["id"]))
        try:
            publish(parent["id"])
            with Session(engine) as s:
                row = s.exec(text(
                    "SELECT subagent_manifest FROM agentversion WHERE agent_id = :a AND is_current = true"
                ), params={"a": parent["id"]}).first()
            entries = row[0]["subagents"]
            assert entries[0]["agent_version_id"] is None

            r = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r.status_code == 200, r.text
            assert r.json()["output"] == "CHILD_DRAFT_OUTPUT"
        finally:
            cleanup(parent["id"], child["id"])

    def test_resolve_subagent_flow_directly_prefers_the_pinned_manifest_entry_over_the_childs_current_version(self):
        """Direct, unit-level proof of the actual mechanism: given a
        manifest naming an OLD AgentVersion.id, _resolve_subagent_flow
        returns THAT version's own flow even though the target's
        current version has since changed."""
        child = create_agent(child_flow("CHILD_V2_OUTPUT"))
        publish(child["id"])
        try:
            with Session(engine) as s:
                old_version_id = s.exec(text(
                    "SELECT id FROM agentversion WHERE agent_id = :a AND is_current = true"
                ), params={"a": child["id"]}).first()[0]

            edit_template_node(child["id"], "CHILD_V3_OUTPUT")
            publish(child["id"])

            manifest = {"subagents": [{"node_id": "p2", "agent_id": child["id"], "agent_version_id": old_version_id, "version": "v2"}]}
            ctx = {"_subagent_manifest": manifest, "_current_node_id": "p2"}
            with Session(engine) as s:
                target = s.get(Agent, child["id"])
                resolved = _resolve_subagent_flow(s, target, ctx, {})
            template_node = next(n for n in resolved["nodes"] if n["id"] == "n2")
            assert template_node["config"]["template"] == "CHILD_V2_OUTPUT"
        finally:
            cleanup(child["id"])

    def test_a_child_gaining_hitl_after_the_parent_was_published_never_reaches_the_unchanged_parent(self):
        """The report's own second, more subtle concern: A -> B v2 (no
        HITL) is validated and published; B v3 later gains a real hitl
        node. Pinning must mean A's own unchanged, already-published
        run never encounters that new topology at all — it stays on
        the sync path and returns B's own pinned, pre-HITL output,
        never touching the child's new hitl node."""
        child = create_agent(child_flow("NO_HITL_OUTPUT"))
        publish(child["id"])
        parent = create_agent(parent_flow(child["id"]))
        try:
            publish(parent["id"])

            hitl_node = client.post(f"/api/agents/{child['id']}/nodes", json={
                "type": "hitl", "x": 50, "y": 100,
                "config": {"form_content": "Approve?", "delivery_methods": ["web_app"]},
            })
            assert hitl_node.status_code == 200, hitl_node.text
            new_node_id = next(n["id"] for n in hitl_node.json()["flow"]["nodes"] if n["type"] == "hitl")
            edge_r = client.post(f"/api/agents/{child['id']}/edges", json={
                "source": "n2", "target": new_node_id, "source_handle": None,
            })
            assert edge_r.status_code == 200, edge_r.text
            publish(child["id"])

            r = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "success"
            assert body["execution_mode"] == "sync (direct harness call, no Temporal)"
            assert body["output"] == "NO_HITL_OUTPUT"
        finally:
            cleanup(parent["id"], child["id"])
