"""
Real tests for the published-version-fail-closed fix (section 0.90,
GAP_REPORT).

The real bug, exactly as reported: _resolve_execution_flow's own
honest, STATED fallback ("falls back to agent.flow rather than
hard-erroring a real run") for a published agent with no current
AgentVersion row was confirmed to be a real, existing, non-hypothetical
problem - seed.py creates several agents with status="published" and
never creates a matching AgentVersion, and this environment's own real
database was found to hold thousands of real, pre-existing published
agents in exactly this state before this fix (many from this same
session's own accumulated test runs, not just the original seed data).
The identical bug pattern existed a second time in harness.py's own
_resolve_subagent_flow (a nested Sub-Agent call's own target
resolution), added in an earlier session (§0.83) after this exact class
of bug was already fixed once for every other channel - silently
unfixed for this one.

The fix has three real parts:
1. A real, one-time Alembic data migration
   (5a5c062ae225_backfill_missing_agentversion_for_) that creates a
   real AgentVersion snapshot, from each agent's own current flow, for
   every real, pre-existing published agent missing one.
2. Both real resolution points (_resolve_execution_flow,
   _resolve_subagent_flow) now raise PublishedVersionIntegrityError
   instead of silently falling back, when a published agent/target has
   no real, current AgentVersion row.
3. A real, dedicated FastAPI exception handler in main.py surfaces
   this as a clear, specific 500 response rather than a generic one;
   two real call sites (run_agent, the webhook trigger) that used to
   wrap ANY exception in a misleading "is the Temporal dev server
   running?" message now re-raise this one distinctly.

These tests reproduce the exact scenario from the bug report directly
against the real, running API and the real database - not mocked.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.main import app
from app.db import engine
from app.models import Agent, AgentVersion
from app.harness import PublishedVersionIntegrityError
from app.routers.execution import _resolve_execution_flow

client = TestClient(app)


def create_agent(app_type="chatflow") -> dict:
    r = client.post("/api/agents", json={
        "name": f"PubVersion Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": app_type,
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "hello"}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
        },
    })
    assert r.status_code == 200, r.text
    return r.json()


def force_published_no_version(agent_id: str):
    """Directly reproduces the exact bug scenario - an agent marked
    published WITHOUT ever going through the real publish endpoint, so
    no real AgentVersion row is ever created for it. This is exactly
    what seed.py does, and exactly the state thousands of real,
    pre-existing rows in this platform's own database were found in
    before the real §0.90 backfill migration."""
    with Session(engine) as s:
        s.exec(text("UPDATE agent SET status = 'published' WHERE id = :a"), params={"a": agent_id})
        s.commit()


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealFailClosedResolution:
    def test_resolve_execution_flow_raises_for_a_published_agent_with_no_version(self):
        agent = create_agent()
        force_published_no_version(agent["id"])
        try:
            with Session(engine) as s:
                a = s.get(Agent, agent["id"])
                with pytest.raises(PublishedVersionIntegrityError):
                    _resolve_execution_flow(s, a, preview=False)
        finally:
            cleanup_agent(agent["id"])

    def test_preview_mode_is_unaffected_even_for_a_published_agent_with_no_version(self):
        """preview=True always means "the live draft" regardless of
        publish state - this real, honest escape hatch must still
        work even for an agent in this exact broken state, since a
        Builder "Try It" call has nothing to do with published-version
        integrity at all."""
        agent = create_agent()
        force_published_no_version(agent["id"])
        try:
            with Session(engine) as s:
                a = s.get(Agent, agent["id"])
                flow = _resolve_execution_flow(s, a, preview=True)
                assert flow == a.flow
        finally:
            cleanup_agent(agent["id"])

    def test_a_run_against_a_published_agent_with_no_version_fails_with_a_clear_specific_error(self):
        agent = create_agent()
        force_published_no_version(agent["id"])
        try:
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r.status_code == 500
            body = r.json()
            assert body["error"] == "published_version_integrity_error"
            assert body["agent_id"] == agent["id"]
            # The real, specific message - not the unrelated Temporal/
            # HITL troubleshooting hint this used to be wrapped in.
            assert "Temporal" not in body["detail"]
            assert "AgentVersion" in body["detail"]
        finally:
            cleanup_agent(agent["id"])

    def test_republishing_through_the_real_endpoint_resolves_the_problem(self):
        """The real, stated remediation path actually works: a normal
        publish call always creates a real AgentVersion in the same
        real transaction as flipping status, so re-publishing a
        broken agent through the real endpoint fixes it."""
        agent = create_agent()
        force_published_no_version(agent["id"])
        try:
            r1 = client.post(f"/api/agents/{agent['id']}/publish")
            assert r1.status_code == 200, r1.text

            r2 = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r2.status_code == 200, r2.text
            assert r2.json()["status"] == "success"
        finally:
            cleanup_agent(agent["id"])


class TestRealSubagentFailClosed:
    """The identical bug pattern in harness.py's own
    _resolve_subagent_flow (§0.83's own nested Sub-Agent resolution)
    - added after §0.54's own fix already closed this exact class of
    bug for every other channel, silently unfixed for this one."""

    def test_a_subagent_call_to_a_published_target_with_no_version_fails_the_node_honestly(self):
        child = create_agent()
        force_published_no_version(child["id"])
        parent = None
        try:
            r = client.post("/api/agents", json={
                "name": f"Subagent Parent Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": "chatflow",
                "template_flow": {
                    "nodes": [
                        {"id": "t1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                        {"id": "sa1", "type": "subagent", "x": 100, "y": 0, "sub": "", "config": {"target_agent_id": child["id"]}},
                    ],
                    "edges": [{"id": "e1", "source": "t1", "target": "sa1", "source_handle": None}],
                },
            })
            assert r.status_code == 200, r.text
            parent = r.json()

            rr = client.post(f"/api/agents/{parent['id']}/run", json={"input": "hi"})
            assert rr.status_code == 200, rr.text
            body = rr.json()
            # A real, honest node-level failure - the parent run
            # itself doesn't crash, the subagent node's own span
            # reports the real, specific reason.
            assert body["status"] == "failed"
            subagent_span = next(s for s in body["spans"] if s["node"] == "subagent")
            assert subagent_span["status"] == "error"
            assert "AgentVersion" in subagent_span["error"]
        finally:
            if parent:
                cleanup_agent(parent["id"])
            cleanup_agent(child["id"])


class TestRealBackfillMigration:
    """Direct, database-level verification of the real §0.90 backfill
    migration's own logic (not a re-run of the migration itself, which
    already ran once against this environment's real data - see
    GAP_REPORT §0.90 for the real, live count) - confirms the
    underlying SQL pattern the migration uses genuinely closes this
    gap and is genuinely idempotent."""

    def test_the_backfill_sql_pattern_correctly_creates_a_missing_version_from_scratch(self):
        """Self-contained, direct proof the real migration's own SQL
        pattern works correctly - deliberately does NOT assert
        anything about the GLOBAL state of this shared test database
        (a separate, pre-existing, unrelated fixture in
        test_agent_node.py creates its own "published, no version"
        agents with no teardown, for its own different testing needs
        - a real, separate, pre-existing test-hygiene gap, out of this
        fix's own scope to correct). Instead, this creates its own
        agent, forces it into exactly the broken state the report
        described, runs the exact same real SQL the actual migration
        uses against just that one agent, and confirms it's fixed."""
        agent = create_agent()
        force_published_no_version(agent["id"])
        try:
            with Session(engine) as s:
                before = s.exec(text("""
                    SELECT count(*) FROM agent a WHERE a.id = :a AND a.status = 'published'
                    AND NOT EXISTS (SELECT 1 FROM agentversion av WHERE av.agent_id = a.id AND av.is_current = true)
                """), params={"a": agent["id"]}).first()[0]
                assert before == 1  # confirms the broken state was genuinely reproduced

                s.exec(text("""
                    INSERT INTO agentversion (agent_id, version, flow, app_type, features, entitlement_plan, published_by, created_at, is_current)
                    SELECT a.id, a.version, a.flow, a.app_type, COALESCE(a.features, '{}'::jsonb), '{}'::jsonb,
                           'system-backfill-0.90', replace(COALESCE(a.updated_at, now())::text, ' ', 'T'), true
                    FROM agent a WHERE a.id = :a
                    AND NOT EXISTS (SELECT 1 FROM agentversion av WHERE av.agent_id = a.id AND av.is_current = true)
                    ON CONFLICT (agent_id, version) DO NOTHING
                """), params={"a": agent["id"]})
                s.commit()

                after = s.exec(text("""
                    SELECT count(*) FROM agent a WHERE a.id = :a AND a.status = 'published'
                    AND NOT EXISTS (SELECT 1 FROM agentversion av WHERE av.agent_id = a.id AND av.is_current = true)
                """), params={"a": agent["id"]}).first()[0]
                assert after == 0

            # And the real, downstream effect: the agent now actually runs.
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "success"
        finally:
            cleanup_agent(agent["id"])

    def test_the_backfill_pattern_is_idempotent(self):
        """Running the same real INSERT ... WHERE NOT EXISTS pattern
        again against an agent that already has a real, current
        AgentVersion must be a genuine no-op, not a duplicate or an
        error."""
        agent = create_agent()
        try:
            r = client.post(f"/api/agents/{agent['id']}/publish")
            assert r.status_code == 200, r.text

            with Session(engine) as s:
                before = s.exec(text("SELECT count(*) FROM agentversion WHERE agent_id = :a"), params={"a": agent["id"]}).first()[0]
                s.exec(text("""
                    INSERT INTO agentversion (agent_id, version, flow, app_type, features, entitlement_plan, published_by, created_at, is_current)
                    SELECT a.id, a.version, a.flow, a.app_type, COALESCE(a.features, '{}'::jsonb), '{}'::jsonb,
                           'system-backfill-0.90', replace(COALESCE(a.updated_at, now())::text, ' ', 'T'), true
                    FROM agent a WHERE a.id = :a
                    AND NOT EXISTS (SELECT 1 FROM agentversion av WHERE av.agent_id = a.id AND av.is_current = true)
                    ON CONFLICT (agent_id, version) DO NOTHING
                """), params={"a": agent["id"]})
                s.commit()
                after = s.exec(text("SELECT count(*) FROM agentversion WHERE agent_id = :a"), params={"a": agent["id"]}).first()[0]
            assert after == before
        finally:
            cleanup_agent(agent["id"])
