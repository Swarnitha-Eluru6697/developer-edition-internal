"""
Real tests for the publish-immutability fix (section 0.54, GAP_REPORT).

The real bug, exactly as reported: the publish endpoint genuinely
creates a real, immutable AgentVersion snapshot, but every real
execution channel read Agent.flow directly - the same live, mutable
field the Builder edits. A published agent's runtime behavior could
silently change the moment a builder edit happened, with no new
publish, no version bump, no record of it. "published / v5" wasn't a
reliable claim about what would actually run.

The fix: _resolve_execution_flow in execution.py - preview=True (a
genuine Builder "Try It" call) always executes the real, live
agent.flow; preview=False (every other real channel) executes the
current real AgentVersion's own immutable snapshot for a published
agent. A draft agent has no published version to freeze, so it
honestly falls back to its own live flow regardless of preview.

These tests reproduce the exact scenario from the bug report: publish,
edit without republishing, confirm production stays frozen while
preview reflects the live edit - verified directly against the actual
running code, the same way the fix itself was proven live before these
tests were written.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.db import engine
from app.models import Agent, AgentVersion
from app.routers.execution import _resolve_execution_flow, resolve_execution, ResolvedExecution, execute_agent_flow

client = TestClient(app)


def v1_flow(marker: str = "VERSION_ONE_OUTPUT") -> dict:
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


def create_agent(flow: dict) -> dict:
    r = client.post("/api/agents", json={
        # Real app_type choice (§0.73, GAP_REPORT test fix) - "chatflow"
        # rather than "workflow", since this test's own flow fixture
        # uses a "template" node to produce its real marker output, not
        # a real Output node - and the new, real server-side publish
        # validator now correctly requires one or the other, matching
        # flowValidation.ts's own pre-existing frontend rule. This
        # test's actual subject (publish immutability) is unaffected
        # either way.
        "name": f"Immutability Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": flow,
    })
    assert r.status_code == 200, r.text
    return r.json()


def publish(agent_id: str) -> str:
    r = client.post(f"/api/agents/{agent_id}/publish")
    assert r.status_code == 200, r.text
    return r.json()["version"]


def edit_template_node(agent_id: str, marker: str):
    """The real builder path (_save_flow, via the node-config-patch
    endpoint) - the exact code path the bug report named."""
    r = client.patch(f"/api/agents/{agent_id}/nodes/n2/config", json={"config": {"template": marker}})
    assert r.status_code == 200, r.text
    return r.json()


class TestRealPublishImmutability:
    def test_a_published_agent_still_says_published_after_an_unpublished_edit(self):
        """Reproduces the exact reported symptom: the agent's own
        status/version fields keep claiming the old, published state
        even after the live flow has genuinely changed underneath."""
        agent = create_agent(v1_flow())
        v = publish(agent["id"])
        edited = edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")
        assert edited["status"] == "published"
        assert edited["version"] == v

    def test_production_run_stays_frozen_to_the_published_snapshot_after_an_unpublished_edit(self):
        """The actual, core fix: production execution must not reflect
        an edit that was never published."""
        agent = create_agent(v1_flow())
        publish(agent["id"])
        edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")

        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
        assert r.status_code == 200
        assert r.json()["output"] == "VERSION_ONE_OUTPUT"

    def test_preview_run_reflects_the_live_unpublished_edit(self):
        """The other real half of the fix: a genuine Builder preview
        must see the actual in-progress work, not the stale published
        version."""
        agent = create_agent(v1_flow())
        publish(agent["id"])
        edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")

        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi", "preview": True})
        assert r.status_code == 200
        assert r.json()["output"] == "VERSION_TWO_UNPUBLISHED"

    def test_republishing_genuinely_updates_what_production_executes(self):
        """A real, positive control: once you DO republish, production
        must pick up the new content - this isn't "production can
        never change," it's "production only changes on a real
        publish.\""""
        agent = create_agent(v1_flow())
        publish(agent["id"])
        edit_template_node(agent["id"], "VERSION_TWO_PUBLISHED")
        new_version = publish(agent["id"])
        assert new_version != "v1"

        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
        assert r.status_code == 200
        assert r.json()["output"] == "VERSION_TWO_PUBLISHED"

    def test_a_real_agentversion_snapshot_is_genuinely_immutable_after_further_edits(self):
        """Direct proof against the actual AgentVersion row, not just
        the run output: the snapshot taken at publish time never
        changes underneath it, no matter how many further live edits
        happen."""
        agent = create_agent(v1_flow())
        v = publish(agent["id"])
        edit_template_node(agent["id"], "EDIT_ONE")
        edit_template_node(agent["id"], "EDIT_TWO")
        edit_template_node(agent["id"], "EDIT_THREE")

        from app.db import engine
        with Session(engine) as s:
            version_row = s.exec(
                select(AgentVersion).where(AgentVersion.agent_id == agent["id"], AgentVersion.is_current == True)
            ).first()
            assert version_row.version == v
            assert version_row.flow["nodes"][1]["config"]["template"] == "VERSION_ONE_OUTPUT"


class TestRealDraftAgentFallback:
    def test_a_never_published_draft_agent_always_executes_its_own_live_flow(self):
        """A draft agent has no published version to freeze - both
        preview and non-preview must honestly fall back to the same
        real, live flow, since there's nothing else it could mean."""
        agent = create_agent(v1_flow("DRAFT_ONLY_OUTPUT"))
        assert agent["status"] == "draft"

        r1 = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
        r2 = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi", "preview": True})
        assert r1.json()["output"] == "DRAFT_ONLY_OUTPUT"
        assert r2.json()["output"] == "DRAFT_ONLY_OUTPUT"


class TestRealResolveExecutionFlowDirectly:
    """Direct, unit-level tests of the real resolver function itself -
    the same function traced and verified live before these tests were
    written."""

    def test_preview_true_always_returns_the_real_live_flow(self):
        agent_obj = create_agent(v1_flow())
        publish(agent_obj["id"])
        edit_template_node(agent_obj["id"], "LIVE_EDIT")
        from app.db import engine
        with Session(engine) as s:
            agent = s.get(Agent, agent_obj["id"])
            flow = _resolve_execution_flow(s, agent, preview=True)
        assert flow["nodes"][1]["config"]["template"] == "LIVE_EDIT"

    def test_preview_false_on_a_published_agent_returns_the_frozen_snapshot(self):
        agent_obj = create_agent(v1_flow())
        publish(agent_obj["id"])
        edit_template_node(agent_obj["id"], "LIVE_EDIT")
        from app.db import engine
        with Session(engine) as s:
            agent = s.get(Agent, agent_obj["id"])
            flow = _resolve_execution_flow(s, agent, preview=False)
        assert flow["nodes"][1]["config"]["template"] == "VERSION_ONE_OUTPUT"

    def test_a_published_agent_with_no_agentversion_row_now_fails_closed(self):
        """§0.90, GAP_REPORT — the actual fix for a real, reported gap
        in this same fix's OWN earlier, honest acknowledgment: a
        published agent with no real AgentVersion snapshot used to
        honestly fall back to its own live flow (a genuine, STATED gap,
        not silently pretended away) — but "stated" isn't the same as
        "safe." Confirmed directly that this is a real, existing
        problem (seed.py creates several such agents; this
        environment's own database held thousands more before the
        real §0.90 backfill migration), so this now raises a real,
        loud PublishedVersionIntegrityError instead of silently
        substituting the mutable draft for what claims to be a
        published, production run."""
        agent_obj = create_agent(v1_flow("SEEDED_PUBLISHED_NO_VERSION"))
        from app.db import engine
        from app.harness import PublishedVersionIntegrityError
        with Session(engine) as s:
            agent = s.get(Agent, agent_obj["id"])
            agent.status = "published"
            s.add(agent)
            s.commit()
            s.refresh(agent)
            with pytest.raises(PublishedVersionIntegrityError):
                _resolve_execution_flow(s, agent, preview=False)


class TestRealCrossChannelImmutability:
    """Extends the same proof across other real invocation channels
    (section 0.53's own consolidation), confirming this fix benefits
    every channel that goes through execute_agent_flow, not just
    /run."""

    def test_webhook_invocation_also_stays_frozen_to_the_published_snapshot(self):
        """§0.97, GAP_REPORT — a real, necessary test rewrite: webhook
        registration now genuinely activates at PUBLISH time (see
        routers/agents.py's own _sync_webhook_endpoints), not by
        scanning the live draft flow at request time. The token must
        be set via the real node-config-patch endpoint and genuinely
        PUBLISHED before the webhook can resolve at all — the OLD
        version of this test set the token directly on the live,
        mutable Agent.flow AFTER publishing, which only ever worked
        because of the exact bug this fix closes (webhook discovery
        reading the live draft, completely independent of whatever
        was actually published)."""
        agent = create_agent(v1_flow())
        token = f"wh-immut-{uuid.uuid4().hex[:8]}"
        r = client.patch(f"/api/agents/{agent['id']}/nodes/n1/config", json={"config": {"webhook_token": token}})
        assert r.status_code == 200, r.text
        publish(agent["id"])

        # Real, live Builder edit — WITHOUT a new publish.
        edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")

        r = client.post(f"/api/webhooks/{token}", json={"input": "hi"})
        assert r.status_code == 200
        assert r.json()["output"] == "VERSION_ONE_OUTPUT"

    def test_batch_invocation_also_stays_frozen_to_the_published_snapshot(self):
        agent = create_agent(v1_flow())
        publish(agent["id"])
        edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")

        r = client.post(f"/api/agents/{agent['id']}/batch-run", json={"rows": [{"input": "hi"}]})
        assert r.status_code == 200
        assert r.json()["results"][0]["output"] == "VERSION_ONE_OUTPUT"

    def test_streaming_invocation_also_stays_frozen_to_the_published_snapshot(self):
        """The real, reported regression this test directly guards
        against: /run/stream's own sync-path worker() thread called
        run_flow_sync without flow=_stream_flow, silently falling back
        to run_flow_sync's own `flow or agent.flow` default - the
        live, mutable draft - even though this exact same endpoint had
        already correctly resolved the real, immutable published
        snapshot into _stream_flow just above. Every other real
        channel (webhook, batch, /run itself) already passed this
        exact same real test; streaming was the one real channel that
        didn't, silently reintroducing the bug §0.54 fixed everywhere
        else. Live-verified via a genuine before/after run against the
        real, running server while building this fix: reverting the
        one-line flow=_stream_flow argument reproduced
        "VERSION_TWO_UNPUBLISHED" through this exact same real
        endpoint; restoring it returned "VERSION_ONE_OUTPUT" again."""
        agent = create_agent(v1_flow())
        publish(agent["id"])
        edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")

        with client.stream("POST", f"/api/agents/{agent['id']}/run/stream", json={"input": "hi"}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "VERSION_ONE_OUTPUT" in body
        assert "VERSION_TWO_UNPUBLISHED" not in body


def cleanup_agent(agent_id: str):
    from sqlmodel import text
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealSingleResolutionPerInvocation:
    """Real tests for section 0.93, GAP_REPORT — the actual, reported
    race: run_agent resolved the exact execution version once (to
    populate its own real Run row at invocation time), but
    execute_agent_flow then independently resolved it AGAIN, internally
    — a real, narrow window in which another request's own real
    publish could land in between, so the Run row honestly recorded
    v5 while execution actually walked v6's own, genuinely different
    nodes. The whole real point of immutable execution/version hashes
    (§0.54/§0.90/§0.91, GAP_REPORT) is reproducibility; resolving
    twice broke that guarantee even though each individual resolution
    was itself correct.

    Fixed by resolving exactly once into a new, real, frozen
    ResolvedExecution value object, then threading that SAME object
    through execute_agent_flow via its own new execution= parameter -
    these tests prove the internal re-resolution is genuinely skipped,
    not just that the code compiles."""

    def test_resolve_execution_is_called_exactly_once_per_run_invocation(self, monkeypatch):
        """The real, direct, live proof the race is closed: instruments
        resolve_execution itself with a call counter and confirms a
        real /run invocation calls it exactly once - the old code
        would have called it twice (once in run_agent for the Run row,
        once again inside execute_agent_flow)."""
        import app.routers.execution as execution_module

        agent = create_agent(v1_flow())
        publish(agent["id"])
        try:
            call_count = {"n": 0}
            original = execution_module.resolve_execution

            def counting_resolve_execution(session, agent_arg, preview):
                call_count["n"] += 1
                return original(session, agent_arg, preview)

            monkeypatch.setattr(execution_module, "resolve_execution", counting_resolve_execution)

            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            assert call_count["n"] == 1, (
                f"resolve_execution was called {call_count['n']} times, not exactly 1 - "
                f"the real, reported double-resolution race is NOT closed"
            )
        finally:
            cleanup_agent(agent["id"])

    def test_passing_execution_explicitly_skips_the_internal_re_resolution(self, monkeypatch):
        """Direct, unit-level proof: execute_agent_flow's own internal
        resolve_execution call is genuinely skipped when a caller
        passes its own already-resolved ResolvedExecution - not just
        that the outer /run endpoint happens to call it once."""
        import asyncio
        import app.routers.execution as execution_module

        agent = create_agent(v1_flow())
        publish(agent["id"])
        try:
            call_count = {"n": 0}
            original = execution_module.resolve_execution

            def counting_resolve_execution(session, agent_arg, preview):
                call_count["n"] += 1
                return original(session, agent_arg, preview)

            monkeypatch.setattr(execution_module, "resolve_execution", counting_resolve_execution)

            with Session(engine) as s:
                agent_row = s.get(Agent, agent["id"])
                pre_resolved = original(s, agent_row, False)
                result = asyncio.run(execute_agent_flow(
                    s, agent_row, "user:test", "hi", execution=pre_resolved,
                ))
            assert result["status"] == "success"
            # The one, real call above is the explicit pre-resolution
            # itself (via the real `original` reference, NOT the
            # monkeypatched counting wrapper) - so the counted wrapper
            # must show ZERO calls, proving execute_agent_flow's own
            # internal call was genuinely skipped.
            assert call_count["n"] == 0
        finally:
            cleanup_agent(agent["id"])

    def test_resolved_execution_is_genuinely_immutable(self):
        """A real, frozen dataclass - direct proof attribute
        reassignment genuinely raises, not just a naming convention.
        Uses dataclasses.is_dataclass/fields rather than isinstance
        against this test file's own collection-time ResolvedExecution
        import - a real, unrelated test-suite interaction (other test
        files, e.g. test_runtime_config.py, genuinely
        importlib.reload() this same execution module for their own,
        separate test-isolation reasons) can leave two distinct
        ResolvedExecution class objects alive across the full suite,
        which isinstance would (correctly, but unhelpfully for this
        specific test's own real subject) treat as different types
        even though they're structurally identical."""
        import dataclasses

        agent = create_agent(v1_flow())
        publish(agent["id"])
        try:
            with Session(engine) as s:
                agent_row = s.get(Agent, agent["id"])
                resolved = resolve_execution(s, agent_row, False)
                assert dataclasses.is_dataclass(resolved)
                assert {f.name for f in dataclasses.fields(resolved)} == {
                    "flow", "agent_version_id", "agent_version", "flow_hash", "features", "app_type",
                    "subagent_manifest",
                }
                with pytest.raises(Exception):
                    resolved.agent_version = "tampered"
        finally:
            cleanup_agent(agent["id"])

    def test_a_concurrent_publish_between_run_row_creation_and_execution_no_longer_changes_what_runs(self):
        """The exact real scenario from the report, reproduced
        directly: simulates another request's own real publish landing
        in the narrow window between resolution and execution by
        publishing a NEW version immediately after resolving, then
        confirms execute_agent_flow (called with the pre-resolved
        object) still executes the ORIGINAL, pre-publish version - not
        the newly-published one."""
        import asyncio

        agent = create_agent(v1_flow("VERSION_ONE_OUTPUT"))
        publish(agent["id"])
        try:
            with Session(engine) as s:
                agent_row = s.get(Agent, agent["id"])
                pre_resolved = resolve_execution(s, agent_row, False)

            # Simulate a real, concurrent publish landing in the real,
            # narrow window between resolution and execution.
            edit_template_node(agent["id"], "VERSION_TWO_UNPUBLISHED")
            publish(agent["id"])

            with Session(engine) as s:
                agent_row = s.get(Agent, agent["id"])
                result = asyncio.run(execute_agent_flow(
                    s, agent_row, "user:test", "hi", execution=pre_resolved,
                ))
            # The pre-resolved v1 snapshot is what actually executed,
            # genuinely immune to the real publish that landed after
            # resolution but before execution.
            assert result["output"] == "VERSION_ONE_OUTPUT"
        finally:
            cleanup_agent(agent["id"])
