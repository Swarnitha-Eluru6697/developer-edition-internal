"""
Real tests for webhook/batch/eval recording the exact resolved
AgentVersion (section 0.99, GAP_REPORT).

The real bug, exactly as reported: webhook's own Run/Trace creation
never populated agent_version_id/agent_version/flow_hash, and Trace's
own `version` field used the LIVE agent.version rather than the
version this specific invocation actually resolved and executed
against. Batch had the identical gap, plus a deeper, real conceptual
issue: each row independently called execute_agent_flow, so a real,
concurrent publish landing mid-batch could in principle mean different
rows inside one logical batch call silently executed against different
flow content. Eval had the same conceptual issue - each case
independently resolved the agent.

The fix: resolve the execution snapshot exactly once at each
channel's own invocation boundary, and pass that SAME resolved object
into every row/case's own execute_agent_flow call and onto every
Run/Trace/EvalRun row - one real, shared, resolved version per logical
invocation, matching the report's own recommended model.

These tests exercise the real, running API directly.
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


def create_agent(marker: str = "V1") -> dict:
    r = client.post("/api/agents", json={
        "name": f"Version Metadata Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                {"id": "n2", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": marker}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
        },
    })
    assert r.status_code == 200, r.text
    return r.json()


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        s.exec(text("DELETE FROM webhookendpoint WHERE agent_id = :a"), params={"a": agent_id})
        eval_set_ids = [row[0] for row in s.exec(text("SELECT id FROM evalset WHERE agent_id = :a"), params={"a": agent_id}).all()]
        for esid in eval_set_ids:
            s.exec(text("DELETE FROM evalresult WHERE eval_run_id IN (SELECT id FROM evalrun WHERE eval_set_id = :e)"), params={"e": esid})
            s.exec(text("DELETE FROM evalrun WHERE eval_set_id = :e"), params={"e": esid})
            s.exec(text("DELETE FROM evalcase WHERE eval_set_id = :e"), params={"e": esid})
        s.exec(text("DELETE FROM evalset WHERE agent_id = :a"), params={"a": agent_id})
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealWebhookVersionMetadata:
    def test_a_webhook_invocation_records_the_exact_resolved_version(self):
        agent = create_agent("V1")
        try:
            token = f"vm-{uuid.uuid4().hex[:8]}"
            set_r = client.patch(f"/api/agents/{agent['id']}/nodes/n1/config", json={"config": {"webhook_token": token}})
            assert set_r.status_code == 200, set_r.text
            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text
            expected_version = pub.json()["version"]

            r = client.post(f"/api/webhooks/{token}", json={"input": "hi"})
            assert r.status_code == 200, r.text
            run_id = r.json()["run_id"]

            with Session(engine) as s:
                run = s.exec(text(
                    "SELECT agent_version_id, agent_version, flow_hash FROM run WHERE id = :r"
                ), params={"r": run_id}).first()
                assert run is not None
                assert run[0] is not None
                assert run[1] == expected_version
                assert run[2] is not None and len(run[2]) == 64  # a real sha256 hex digest

                trace = s.exec(text(
                    "SELECT agent_version_id, agent_version, flow_hash FROM trace WHERE agent_id = :a"
                ), params={"a": agent["id"]}).first()
                assert trace == run
        finally:
            cleanup_agent(agent["id"])


class TestRealBatchVersionMetadata:
    def test_every_row_in_one_batch_call_records_the_same_resolved_version(self):
        """The report's own core conceptual concern: every row inside
        ONE logical batch call must share the SAME resolved version -
        not each independently re-resolving, which a real, concurrent
        publish landing mid-batch could otherwise splinter."""
        agent = create_agent("V1")
        try:
            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text
            expected_version = pub.json()["version"]

            r = client.post(f"/api/agents/{agent['id']}/batch-run", json={
                "rows": [{"input": "row1"}, {"input": "row2"}, {"input": "row3"}],
            })
            assert r.status_code == 200, r.text
            assert r.json()["succeeded"] == 3

            with Session(engine) as s:
                runs = s.exec(text(
                    "SELECT agent_version_id, agent_version, flow_hash FROM run WHERE agent_id = :a"
                ), params={"a": agent["id"]}).all()
                assert len(runs) == 3
                assert all(row[1] == expected_version for row in runs)
                assert len({row[0] for row in runs}) == 1
                assert len({row[2] for row in runs}) == 1

                traces = s.exec(text(
                    "SELECT agent_version, flow_hash FROM trace WHERE agent_id = :a"
                ), params={"a": agent["id"]}).all()
                assert len(traces) == 3
                assert all(row[0] == expected_version for row in traces)
        finally:
            cleanup_agent(agent["id"])

    def test_execute_agent_flow_is_not_called_with_a_stale_execution_after_republish(self, monkeypatch):
        """Direct, unit-level proof of the actual mechanism: batch
        resolves once, up front, and passes that SAME object into
        every row - confirmed here by monkeypatching execute_agent_flow
        to capture what `execution=` it was actually called with for
        every row, then asserting every row received the identical
        object (not each independently re-resolving)."""
        import app.routers.execution as execution_module

        agent = create_agent("V1")
        try:
            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text

            captured = []
            original = execution_module.execute_agent_flow

            async def spy(*args, **kwargs):
                captured.append(kwargs.get("execution"))
                return await original(*args, **kwargs)
            monkeypatch.setattr(execution_module, "execute_agent_flow", spy)

            r = client.post(f"/api/agents/{agent['id']}/batch-run", json={
                "rows": [{"input": "row1"}, {"input": "row2"}],
            })
            assert r.status_code == 200, r.text
            assert len(captured) == 2
            assert captured[0] is not None
            assert captured[0] is captured[1]
        finally:
            cleanup_agent(agent["id"])


class TestRealEvalVersionMetadata:
    def test_an_eval_run_records_the_exact_resolved_version(self):
        agent = create_agent("hello world")
        try:
            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text
            expected_version = pub.json()["version"]

            set_r = client.post("/api/evals/sets", json={"name": "Version Test Set", "agent_id": agent["id"]})
            set_id = set_r.json()["id"]
            client.post(f"/api/evals/sets/{set_id}/cases", json={"input": "hi", "expected_output": "hello"})

            run_r = client.post(f"/api/evals/sets/{set_id}/run")
            assert run_r.status_code == 200, run_r.text
            run_body = run_r.json()["run"]
            assert run_body["agent_version"] == expected_version
            assert run_body["agent_version_id"] is not None
            assert run_body["flow_hash"] is not None and len(run_body["flow_hash"]) == 64
        finally:
            cleanup_agent(agent["id"])

    def test_every_case_in_one_eval_run_shares_the_same_resolved_version(self, monkeypatch):
        """The report's own core conceptual concern for evals: every
        case inside ONE logical eval run must be scored against the
        SAME resolved version - confirmed by capturing what
        `execution=` execute_agent_flow was actually called with for
        every case."""
        import app.routers.evals as evals_module

        agent = create_agent("hello world")
        try:
            pub = client.post(f"/api/agents/{agent['id']}/publish")
            assert pub.status_code == 200, pub.text

            set_r = client.post("/api/evals/sets", json={"name": "Version Test Set 2", "agent_id": agent["id"]})
            set_id = set_r.json()["id"]
            client.post(f"/api/evals/sets/{set_id}/cases", json={"input": "hi", "expected_output": "hello"})
            client.post(f"/api/evals/sets/{set_id}/cases", json={"input": "hey", "expected_output": "hello"})

            captured = []
            original = evals_module.execute_agent_flow

            async def spy(*args, **kwargs):
                captured.append(kwargs.get("execution"))
                return await original(*args, **kwargs)
            monkeypatch.setattr(evals_module, "execute_agent_flow", spy)

            run_r = client.post(f"/api/evals/sets/{set_id}/run")
            assert run_r.status_code == 200, run_r.text
            assert len(captured) == 2
            assert captured[0] is not None
            assert captured[0] is captured[1]
        finally:
            cleanup_agent(agent["id"])
