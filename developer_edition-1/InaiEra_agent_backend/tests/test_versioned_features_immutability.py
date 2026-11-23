"""
Real tests for versioned runtime Features being frozen at publish time
(section 0.96, GAP_REPORT).

The real bug, exactly as reported: AgentVersion correctly stores flow,
app_type, AND features — but _resolve_execution_context (now
resolve_execution) only ever returned flow/agent_version_id/
agent_version/flow_hash. Runtime behavior kept reading the live,
mutable agent.features directly for moderation, follow-ups, chat
history, and citations — so a published version's own flow could stay
genuinely frozen while its actual runtime BEHAVIOR silently changed the
moment someone edited live Features without a new publish. Separately,
PATCH /{agent_id}/features directly mutated a.features in place with
no real signal that anything had changed (draft_revision never moved).

The fix: every VERSIONED features read (moderation, follow-ups, chat
history, citations) now goes through the same frozen
ResolvedExecution.features every other §0.93 caller already uses;
require_authenticated_user is deliberately categorized as
OPERATIONAL/deployment configuration and stays reading the live value,
per the report's own recommended split. PATCH /features now routes
through the same real, atomic _atomic_save_agent every other
"edit the draft" endpoint uses, so draft_revision genuinely increments.

These tests exercise the real, running API directly — not mocked.
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

client = TestClient(app)


def create_agent() -> dict:
    r = client.post("/api/agents", json={
        "name": f"Frozen Features Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": "response text"}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
        },
    })
    assert r.status_code == 200, r.text
    return r.json()


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        # §0.107, GAP_REPORT test fix — hitlrequest.run_id is a real FK
        # against run.id, so it must be deleted BEFORE run, not after.
        for t in ("hitlrequest", "run", "trace", "agentlog", "agentversion"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealVersionedFeaturesStayFrozenAfterPublish:
    """The exact real scenario from the report: publish v5 with
    moderation enabled; edit live Features without publishing;
    production must keep executing the frozen, published behavior."""

    def test_a_live_moderation_edit_does_not_change_an_already_published_versions_behavior(self):
        agent = create_agent()
        try:
            fr = client.patch(f"/api/agents/{agent['id']}/features", json={
                "features": {"moderation": {"enabled": True, "input_enabled": True, "provider": "keywords",
                                             "keywords": ["badword"], "input_preset_reply": "Blocked: badword detected"}},
            })
            assert fr.status_code == 200, fr.text

            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text

            r1 = client.post(f"/api/agents/{agent['id']}/run", json={"input": "this has a badword in it"})
            assert r1.status_code == 200, r1.text
            assert r1.json()["status"] == "blocked"
            assert r1.json()["fail_class"] == "moderation"

            # Real, live edit — moderation disabled — WITHOUT a new publish.
            fr2 = client.patch(f"/api/agents/{agent['id']}/features", json={"features": {"moderation": {"enabled": False}}})
            assert fr2.status_code == 200, fr2.text

            # The real, direct proof: the ALREADY-PUBLISHED version must
            # still block the exact same message, genuinely immune to
            # the live edit that just happened.
            r2 = client.post(f"/api/agents/{agent['id']}/run", json={"input": "this has a badword in it"})
            assert r2.status_code == 200, r2.text
            assert r2.json()["status"] == "blocked"
            assert r2.json()["fail_class"] == "moderation"
        finally:
            cleanup_agent(agent["id"])

    def test_a_preview_run_reflects_the_live_edited_features_not_the_frozen_ones(self):
        """The real, honest counterpart - a genuine Builder preview
        always sees the live draft's own features, exactly as it
        always sees the live draft's own flow."""
        agent = create_agent()
        try:
            client.patch(f"/api/agents/{agent['id']}/features", json={
                "features": {"moderation": {"enabled": True, "input_enabled": True, "provider": "keywords",
                                             "keywords": ["badword"], "input_preset_reply": "Blocked"}},
            })
            assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200

            client.patch(f"/api/agents/{agent['id']}/features", json={"features": {"moderation": {"enabled": False}}})

            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "this has a badword in it", "preview": True})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "success"
        finally:
            cleanup_agent(agent["id"])

    def test_republishing_makes_the_new_features_the_new_frozen_production_behavior(self):
        agent = create_agent()
        try:
            client.patch(f"/api/agents/{agent['id']}/features", json={
                "features": {"moderation": {"enabled": True, "input_enabled": True, "provider": "keywords",
                                             "keywords": ["badword"], "input_preset_reply": "Blocked"}},
            })
            assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200

            client.patch(f"/api/agents/{agent['id']}/features", json={"features": {"moderation": {"enabled": False}}})
            r2 = client.post(f"/api/agents/{agent['id']}/publish")
            assert r2.status_code == 200, r2.text

            r3 = client.post(f"/api/agents/{agent['id']}/run", json={"input": "this has a badword in it"})
            assert r3.status_code == 200, r3.text
            assert r3.json()["status"] == "success"
        finally:
            cleanup_agent(agent["id"])


class TestRealPatchFeaturesBumpsDraftRevision:
    """The report's own second, separate finding: PATCH /features
    directly mutated a.features with no real signal that anything had
    changed."""

    def test_patching_features_genuinely_increments_draft_revision(self):
        agent = create_agent()
        try:
            before = agent["draft_revision"]
            r = client.patch(f"/api/agents/{agent['id']}/features", json={"features": {"moderation": {"enabled": True}}})
            assert r.status_code == 200, r.text
            assert r.json()["draft_revision"] == before + 1
        finally:
            cleanup_agent(agent["id"])

    def test_patching_features_does_not_change_the_agents_published_status(self):
        agent = create_agent()
        try:
            assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
            r = client.patch(f"/api/agents/{agent['id']}/features", json={"features": {"moderation": {"enabled": True}}})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "published"
        finally:
            cleanup_agent(agent["id"])


class TestRealOperationalFeaturesStayLive:
    """require_authenticated_user is deliberately categorized as
    OPERATIONAL/deployment configuration, not versioned behavior - it
    must stay reading the LIVE features, so an operator can toggle it
    immediately without a re-publish."""

    def test_require_authenticated_user_takes_effect_immediately_without_a_republish(self):
        agent = create_agent()
        try:
            assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200

            fr = client.patch(f"/api/agents/{agent['id']}/features", json={"features": {"require_authenticated_user": True}})
            assert fr.status_code == 200, fr.text

            # No re-publish - the real, live toggle must still apply
            # immediately to a production run, since this is
            # operational, not versioned.
            # Every request now carries a gateway identity (deny by
            # default), so "takes effect" means: the verified identity
            # replaces the claimed invoking_user on the very next run.
            from conftest import gateway_headers
            r = client.post(f"/api/agents/{agent['id']}/run",
                            json={"input": "hi", "invoking_user": "user:claimed-x"},
                            headers=gateway_headers(user="verified-x"))
            assert r.status_code == 200, r.text
            tr = client.get(f"/api/traces/{r.json()['trace_id']}").json()
            assert "verified-x" in tr["principal"] and "claimed-x" not in tr["principal"]
        finally:
            cleanup_agent(agent["id"])


def create_moderated_agent(template_text: str = "This output contains BADWORD in it.") -> dict:
    """Real, dedicated fixture for section 0.107, GAP_REPORT — a
    template node whose own output contains a real, configured blocked
    keyword, so a run against it genuinely triggers output moderation
    without needing any real LLM call at all."""
    r = client.post("/api/agents", json={
        "name": f"Output Moderation Ordering Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": template_text}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
        },
    })
    assert r.status_code == 200, r.text
    agent = r.json()
    fr = client.patch(f"/api/agents/{agent['id']}/features", json={
        "features": {"moderation": {
            "enabled": True, "provider": "keywords", "output_enabled": True,
            "keywords": ["BADWORD"], "output_preset_reply": "SAFE_REPLACEMENT_MESSAGE",
        }},
    })
    assert fr.status_code == 200, fr.text
    return agent


class TestRealOutputModerationHappensBeforeTerminalPersistence:
    """Real tests for section 0.107, GAP_REPORT.

    The real, reported bug: output moderation used to run AFTER
    _finalize_run had already persisted this run's own Run/Trace rows
    as status="success" — a real caller-visible "blocked" response
    could sit right alongside a Run/Trace pair honestly claiming
    success, with the ORIGINAL, blocked output content still in
    Trace.final_output and the last node's own span-level
    output_preview.

    The fix: moderation now runs BEFORE any terminal persistence, on
    every real path that finalizes a run (the main /run endpoint,
    the durable-run reconciler, both branches of the streaming
    endpoint, and the HITL-resume endpoint) — result is fully rewritten
    first, so what gets persisted is always the same real outcome the
    API response itself reports. A real, explicit "discard" policy
    (_redact_blocked_output_spans) also strips the raw, blocked content
    out of the last node's own span before it ever reaches Trace."""

    def test_a_blocked_run_persists_blocked_not_success(self):
        agent = create_moderated_agent()
        try:
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "blocked"
            assert body["output"] == "SAFE_REPLACEMENT_MESSAGE"
            assert body["fail_class"] == "moderation"

            with Session(engine) as s:
                run_row = s.exec(text("SELECT status, fail_class FROM run WHERE id = :r"), params={"r": body["run_id"]}).first()
                assert run_row[0] == "blocked"
                assert run_row[0] != "success"
                assert run_row[1] == "moderation"

                trace_row = s.exec(text(
                    "SELECT status, final_output, spans_json FROM trace WHERE id = :t"
                ), params={"t": body["trace_id"]}).first()
                assert trace_row[0] == "moderation_blocked"
                assert trace_row[1] == "SAFE_REPLACEMENT_MESSAGE"
                # The real, second reported concern: no accidental
                # retention of the raw, blocked content anywhere in
                # normal Trace data — not final_output (checked just
                # above) and not a span's own output_preview.
                for span in trace_row[2]:
                    if "output_preview" in span and span.get("node") != "trigger":
                        assert "BADWORD" not in span["output_preview"]
                        assert span["output_preview"] == "[redacted — blocked by output moderation]"
        finally:
            cleanup_agent(agent["id"])

    def test_an_unblocked_run_still_persists_success_normally(self):
        """Regression check — moderation being checked at all must not
        change the honest outcome for a genuinely clean output."""
        agent = create_moderated_agent(template_text="This is a clean, safe output.")
        try:
            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "success"
            assert body["output"] == "This is a clean, safe output."

            with Session(engine) as s:
                run_row = s.exec(text("SELECT status FROM run WHERE id = :r"), params={"r": body["run_id"]}).first()
                assert run_row[0] == "success"
        finally:
            cleanup_agent(agent["id"])

    def test_the_hitl_resume_path_also_moderates_before_persisting(self):
        """The report's own related gap found while fixing the primary
        one: respond_to_hitl had NO output moderation at all — not
        merely mis-ordered, entirely absent."""
        r = client.post("/api/agents", json={
            "name": f"HITL Moderation Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                        "delivery_methods": ["web_app"], "form_content": "Approve?",
                        "actions": [{"id": "approve", "title": "Approve"}],
                    }},
                    {"id": "n3", "type": "template", "x": 200, "y": 0, "sub": "", "config": {"template": "This output contains BADWORD in it."}},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": "approve"},
                ],
            },
        })
        agent = r.json()
        try:
            fr = client.patch(f"/api/agents/{agent['id']}/features", json={
                "features": {"moderation": {
                    "enabled": True, "provider": "keywords", "output_enabled": True,
                    "keywords": ["BADWORD"], "output_preset_reply": "SAFE_REPLACEMENT_MESSAGE",
                }},
            })
            assert fr.status_code == 200, fr.text

            from unittest.mock import patch
            import app.routers.execution as execution_module
            with patch.object(execution_module, "_needs_durable_path", return_value=False):
                run_r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi", "invoking_user": "user:you"})
            assert run_r.status_code == 200, run_r.text
            request_id = run_r.json()["hitl_request_id"]

            resp_r = client.post(f"/api/agents/{agent['id']}/hitl/{request_id}/respond", json={"action_id": "approve"})
            assert resp_r.status_code == 200, resp_r.text
            body = resp_r.json()
            assert body["status"] == "blocked"
            assert body["output"] == "SAFE_REPLACEMENT_MESSAGE"

            with Session(engine) as s:
                run_row = s.exec(text("SELECT status FROM run WHERE id = :r"), params={"r": body["run_id"]}).first()
                assert run_row[0] == "blocked"
        finally:
            cleanup_agent(agent["id"])

    def test_redact_blocked_output_spans_directly(self):
        """Direct, unit-level proof of the real redaction mechanism."""
        from app.routers.execution import _redact_blocked_output_spans
        spans = [
            {"node": "trigger", "status": "ok"},
            {"node": "template", "status": "ok", "output_preview": "raw blocked content here"},
            {"node": "respond", "status": "ok", "output_preview": "raw blocked content here too"},
        ]
        redacted = _redact_blocked_output_spans(spans)
        assert redacted[0] == spans[0]  # no output_preview at all - untouched
        assert redacted[1]["output_preview"] == "[redacted — blocked by output moderation]"
        assert redacted[2]["output_preview"] == "[redacted — blocked by output moderation]"
        # Real, defensive proof this doesn't mutate the caller's own
        # original list/dicts in place.
        assert spans[1]["output_preview"] == "raw blocked content here"
