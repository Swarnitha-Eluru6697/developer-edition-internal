"""
Real tests for a unified HITL pause lifecycle across every invocation
channel (section 0.98, GAP_REPORT).

The real bug, exactly as reported: /run and /run/stream already
correctly represent a genuine pause as a real Run(status="paused")
plus a resumable HitlRequest — but webhook, MCP, and evals each either
silently misrepresented a pause as a failure (webhook's own unknown-
status-defaults-to-"failed" mapping; MCP's own `is_error = status !=
"success"`), or immediately, incorrectly scored a still-empty, paused
output as if it were the real, final answer (evals), with no real
HitlRequest ever created for any of them - so there was nothing for a
real person to ever resume. Batch's own honest, existing "batch runs
don't support resuming individual rows" message arrived only AFTER a
real, separate, durable Temporal workflow had already been started and
left genuinely, permanently paused.

The fix: a new, shared create_hitl_pause_run (routers/execution.py),
modeled directly on /run's own established, working lifecycle, is now
called by webhook and MCP and evals; batch instead rejects the whole
call upfront if the agent's own resolved flow has a Human Input node,
before any row (and any real Temporal workflow) ever starts.

These tests exercise the real, running API directly, monkeypatching
execute_agent_flow itself to return a genuine paused_for_human_input
result (there's no reachable Temporal test server in this sandbox to
produce a REAL pause end-to-end) - the same, established pattern this
codebase's own existing tests already use for this exact situation.
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

FAKE_PAUSE_RESULT = {
    "status": "paused_for_human_input", "hitl_node_id": "n1",
    "hitl_rendered_content": "Please review", "hitl_fields": [],
    "hitl_actions": [{"id": "approve", "title": "Approve"}],
    "hitl_delivery_status": {}, "spans": [{"node": "trigger", "status": "ok"}],
    "execution_mode": "durable (Temporal)",
}


def create_agent(name_prefix: str) -> dict:
    r = client.post("/api/agents", json={
        "name": f"{name_prefix} {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": "done"}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
        },
    })
    assert r.status_code == 200, r.text
    return r.json()


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        s.exec(text("DELETE FROM hitlrequest WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM webhookendpoint WHERE agent_id = :a"), params={"a": agent_id})
        eval_set_ids = [r[0] for r in s.exec(text("SELECT id FROM evalset WHERE agent_id = :a"), params={"a": agent_id}).all()]
        for esid in eval_set_ids:
            s.exec(text("DELETE FROM evalresult WHERE eval_run_id IN (SELECT id FROM evalrun WHERE eval_set_id = :e)"), params={"e": esid})
            s.exec(text("DELETE FROM evalrun WHERE eval_set_id = :e"), params={"e": esid})
            s.exec(text("DELETE FROM evalcase WHERE eval_set_id = :e"), params={"e": esid})
        s.exec(text("DELETE FROM evalset WHERE agent_id = :a"), params={"a": agent_id})
        for t in ("run", "trace", "agentlog", "agentversion"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealWebhookHitlLifecycle:
    def test_a_genuine_pause_creates_a_real_paused_run_and_hitlrequest(self, monkeypatch):
        agent = create_agent("Webhook HITL Test")
        try:
            set_r = client.patch(f"/api/agents/{agent['id']}/nodes/n1/config", json={"config": {"webhook_token": "WHT"}})
            assert set_r.status_code == 200, set_r.text
            assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200

            import app.routers.webhooks as webhooks_module
            async def fake(*args, **kwargs):
                return dict(FAKE_PAUSE_RESULT)
            monkeypatch.setattr(webhooks_module, "execute_agent_flow", fake)

            r = client.post("/api/webhooks/WHT", json={"input": "hi"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "paused_for_human_input"
            assert body["status"] != "failed"
            assert "hitl_request_id" in body

            with Session(engine) as s:
                run = s.exec(text("SELECT status FROM run WHERE id = :r"), params={"r": body["run_id"]}).first()
                assert run[0] == "paused"
                hitl = s.exec(text("SELECT id FROM hitlrequest WHERE run_id = :r"), params={"r": body["run_id"]}).first()
                assert hitl is not None
                assert hitl[0] == body["hitl_request_id"]
        finally:
            cleanup_agent(agent["id"])


class TestRealMcpHitlLifecycle:
    def test_a_genuine_pause_is_reported_honestly_not_as_an_error(self, monkeypatch):
        agent = create_agent("MCP HITL Test")
        try:
            mcp_r = client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
            assert mcp_r.status_code == 200, mcp_r.text
            mcp_url = mcp_r.json()["url"]
            tool_name = "".join(c if c.isalnum() else "_" for c in agent["name"].lower()).strip("_")

            import app.routers.mcp as mcp_module
            async def fake(*args, **kwargs):
                return dict(FAKE_PAUSE_RESULT)
            monkeypatch.setattr(mcp_module, "execute_agent_flow", fake)

            rpc = client.post(mcp_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool_name, "arguments": {"input": "hi"}},
            })
            assert rpc.status_code == 200, rpc.text
            result = rpc.json()["result"]
            assert result["isError"] is False
            assert "paused" in result["content"][0]["text"].lower()

            with Session(engine) as s:
                hitl = s.exec(text("SELECT id, run_id FROM hitlrequest WHERE agent_id = :a"), params={"a": agent["id"]}).first()
                assert hitl is not None
                run = s.exec(text("SELECT status FROM run WHERE id = :r"), params={"r": hitl[1]}).first()
                assert run[0] == "paused"
        finally:
            cleanup_agent(agent["id"])


class TestRealEvalHitlLifecycle:
    def test_a_genuine_pause_is_honestly_unscored_not_mis_scored(self, monkeypatch):
        agent = create_agent("Eval HITL Test")
        try:
            set_r = client.post("/api/evals/sets", json={"name": "HITL Test Set", "agent_id": agent["id"]})
            set_id = set_r.json()["id"]
            client.post(f"/api/evals/sets/{set_id}/cases", json={"input": "test input", "expected_output": "expected"})

            import app.routers.evals as evals_module
            async def fake(*args, **kwargs):
                return dict(FAKE_PAUSE_RESULT)
            monkeypatch.setattr(evals_module, "execute_agent_flow", fake)

            run_r = client.post(f"/api/evals/sets/{set_id}/run")
            assert run_r.status_code == 200, run_r.text
            result = run_r.json()["results"][0]
            assert result["passed"] is False
            assert result["actual_output"] == ""
            assert "paused" in result["reason"].lower()

            with Session(engine) as s:
                hitl = s.exec(text("SELECT id, run_id FROM hitlrequest WHERE agent_id = :a"), params={"a": agent["id"]}).first()
                assert hitl is not None
                run = s.exec(text("SELECT status FROM run WHERE id = :r"), params={"r": hitl[1]}).first()
                assert run[0] == "paused"
        finally:
            cleanup_agent(agent["id"])


class TestRealBatchHitlPreflightRejection:
    def test_an_agent_with_a_hitl_node_is_rejected_before_any_row_starts(self):
        r = client.post("/api/agents", json={
            "name": f"Batch HITL Preflight Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "workflow",
            "template_flow": {
                "nodes": [
                    {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                    {"id": "n2", "type": "hitl", "x": 100, "y": 0, "sub": "", "config": {
                        "delivery_methods": ["web_app"], "form_content": "Approve?",
                        "actions": [{"id": "approve", "title": "Approve"}],
                    }},
                    {"id": "n3", "type": "respond", "x": 200, "y": 0, "sub": "", "config": {}},
                ],
                "edges": [
                    {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                    {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
                ],
            },
        })
        agent = r.json()
        try:
            batch_r = client.post(f"/api/agents/{agent['id']}/batch-run", json={"rows": [{"input": "hi"}]})
            assert batch_r.status_code == 400
            assert "Human Input node" in batch_r.json()["detail"]

            with Session(engine) as s:
                count = s.exec(text("SELECT count(*) FROM run WHERE agent_id = :a"), params={"a": agent["id"]}).first()
                assert count[0] == 0
        finally:
            cleanup_agent(agent["id"])

    def test_an_agent_without_a_hitl_node_still_runs_batch_normally(self):
        agent = create_agent("Batch No HITL Test")
        try:
            r = client.post(f"/api/agents/{agent['id']}/batch-run", json={"rows": [{"input": "hi"}]})
            assert r.status_code == 200, r.text
            assert r.json()["results"][0]["status"] == "success"
        finally:
            cleanup_agent(agent["id"])
