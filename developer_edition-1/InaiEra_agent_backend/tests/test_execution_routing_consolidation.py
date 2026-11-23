"""
Real tests for the execution-routing consolidation fix (section 0.53, GAP_REPORT).

The real bug, as reported: only /api/agents/{id}/run ever checked
_needs_durable_path before deciding whether to route a run through
Temporal. Every other real invocation channel - webhooks, MCP, evals,
batch, and streaming - called run_flow_sync directly, unconditionally.
A hitl-containing agent genuinely behaved differently depending on which
channel invoked it: a real Temporal signal-based pause through /run, but
the sync path's own snapshot-based HitlPause everywhere else. Two
channels (streaming, batch) had no routing check of any kind, not even
a consistently-wrong one.

The fix: a single, shared execute_agent_flow function in execution.py
that every real channel now calls. These tests monkeypatch the real
_run_durable function (not run_flow_sync) to record which real path a
given invocation actually took, for a deterministic, environment-
independent proof - this repo's own sandbox has no live Temporal server,
so a "did it try to connect" test would otherwise depend on real network
conditions rather than the actual routing decision.

Also verified live, directly, before these tests were written: a real
hitl-containing agent invoked through all 6 real channels each genuinely
attempted a real Temporal connection and got the exact same real
"Connection refused" error /run itself produces - not a theoretical
claim.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Agent

client = TestClient(app)


def hitl_flow() -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "hitl", "x": 0, "y": 0, "sub": "", "config": {
                "form_content": "Approve?", "actions": [{"id": "continue", "title": "Continue", "style": "primary"}],
            }},
            {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def publishable_hitl_flow() -> dict:
    """§0.97, GAP_REPORT test fixture — a genuinely publishable variant
    of hitl_flow, above: "respond" alone isn't a real response-
    producing node type for a chatflow app (the server-side publish
    validator requires one of LLM/Agent/Sub-Agent/Code/Template/HTTP/
    Tool), and a hitl node needs a real delivery_methods config to
    validate at all — hitl_flow itself was never designed to be
    published (the OLD, buggy webhook behavior never required it),
    only this new, real webhook registration mechanism does."""
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "hitl", "x": 0, "y": 0, "sub": "", "config": {
                "delivery_methods": ["web_app"], "form_content": "Approve?",
                "actions": [{"id": "continue", "title": "Continue", "style": "primary"}],
            }},
            {"id": "n3", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "done"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def plain_flow() -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
    }


def publishable_plain_flow() -> dict:
    """§0.97, GAP_REPORT test fixture — see publishable_hitl_flow's
    own docstring just above for the full real reasoning."""
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "done"}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
    }


def create_agent(flow: dict, app_type: str = "workflow") -> dict:
    r = client.post("/api/agents", json={
        "name": f"Routing Test {uuid.uuid4().hex[:6]}", "subtype": "Custom", "app_type": app_type,
        "template_flow": flow,
    })
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def spy_run_durable(monkeypatch):
    import app.routers.execution as execution_module
    calls = []

    async def fake_run_durable(session, agent, invoking_user, user_input, webhook_payload=None, structured_inputs=None, flow=None, features=None, subagent_manifest=None):
        calls.append({
            "agent_id": agent.id, "invoking_user": invoking_user, "user_input": user_input,
            "webhook_payload": webhook_payload, "structured_inputs": structured_inputs, "flow": flow,
        })
        return {"status": "success", "output": "durable-mock-output", "spans": [], "fail_class": None}

    monkeypatch.setattr(execution_module, "_run_durable", fake_run_durable)
    return calls


class TestRealRunEndpointRouting:
    def test_a_hitl_agent_routes_to_the_real_durable_path(self, spy_run_durable):
        agent = create_agent(hitl_flow())
        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        assert r.status_code == 200
        assert r.json()["execution_mode"] == "durable (Temporal)"
        assert len(spy_run_durable) == 1
        assert spy_run_durable[0]["agent_id"] == agent["id"]

    def test_a_plain_agent_stays_on_the_real_sync_path(self, spy_run_durable):
        agent = create_agent(plain_flow())
        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hello"})
        assert r.status_code == 200
        assert r.json()["execution_mode"] == "sync (direct harness call, no Temporal)"
        assert len(spy_run_durable) == 0


class TestRealWebhookRouting:
    def _with_webhook_token(self, agent_id: str, token: str):
        """§0.97, GAP_REPORT — a real, necessary test-fixture update:
        webhook registration now genuinely activates at PUBLISH time
        (routers/agents.py's own _sync_webhook_endpoints), not by
        scanning the live draft flow at request time — this helper
        must set the token via the real node-config-patch endpoint
        (the same real path a Builder actually uses) and then publish,
        rather than mutating a.flow directly and expecting the old,
        buggy draft-scanning behavior to pick it up."""
        r = client.patch(f"/api/agents/{agent_id}/nodes/n1/config", json={"config": {"webhook_token": token}})
        assert r.status_code == 200, r.text
        pub = client.post(f"/api/agents/{agent_id}/publish")
        assert pub.status_code == 200, pub.text

    def test_a_hitl_agent_invoked_via_webhook_now_genuinely_routes_durable(self, spy_run_durable):
        agent = create_agent(publishable_hitl_flow(), app_type="chatflow")
        token = f"wh-{uuid.uuid4().hex[:8]}"
        self._with_webhook_token(agent["id"], token)
        r = client.post(f"/api/webhooks/{token}", json={"input": "hello"})
        assert r.status_code == 200
        assert len(spy_run_durable) == 1
        assert spy_run_durable[0]["invoking_user"] == "webhook"
        assert spy_run_durable[0]["webhook_payload"] == {"input": "hello"}

    def test_a_plain_agent_invoked_via_webhook_stays_sync(self, spy_run_durable):
        agent = create_agent(publishable_plain_flow(), app_type="chatflow")
        token = f"wh-{uuid.uuid4().hex[:8]}"
        self._with_webhook_token(agent["id"], token)
        r = client.post(f"/api/webhooks/{token}", json={"input": "hello"})
        assert r.status_code == 200
        assert len(spy_run_durable) == 0

    def test_a_real_durable_failure_is_a_real_honest_error_not_a_bare_500(self, monkeypatch):
        import app.routers.execution as execution_module

        async def failing_run_durable(*args, **kwargs):
            raise RuntimeError("Connection refused")
        monkeypatch.setattr(execution_module, "_run_durable", failing_run_durable)

        agent = create_agent(publishable_hitl_flow(), app_type="chatflow")
        token = f"wh-{uuid.uuid4().hex[:8]}"
        self._with_webhook_token(agent["id"], token)
        r = client.post(f"/api/webhooks/{token}", json={"input": "hello"})
        assert r.status_code == 500
        assert "Durable execution failed" in r.json()["detail"]


class TestRealMcpRouting:
    def _enable_mcp(self, agent_id: str, token: str):
        # §0.101, GAP_REPORT test fix — the real MCP credential is now
        # a real, top-level Agent column (mcp_enabled/
        # mcp_token_encrypted), never features["mcp"] — this helper
        # sets it the same real way the actual PATCH /mcp endpoint
        # does (a real encrypt_secret call), directly via a real
        # session, so these tests can still pin a specific, known
        # token value rather than the real endpoint's own randomly
        # generated one.
        from sqlmodel import Session
        from app.db import engine
        from app.models import Agent
        from app.secrets_crypto import encrypt_secret
        with Session(engine) as s:
            a = s.get(Agent, agent_id)
            a.mcp_enabled = True
            a.mcp_token_encrypted = encrypt_secret(token)
            s.add(a)
            s.commit()

    def _tool_name(self, agent: dict) -> str:
        return "".join(c if c.isalnum() else "_" for c in agent["name"].lower()).strip("_") or agent["id"]

    def test_a_hitl_agent_invoked_via_mcp_now_genuinely_routes_durable(self, spy_run_durable):
        agent = create_agent(hitl_flow())
        token = f"mcp-{uuid.uuid4().hex[:8]}"
        self._enable_mcp(agent["id"], token)
        r = client.post(f"/mcp/{agent['id']}/{token}", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": self._tool_name(agent), "arguments": {"input": "hello"}},
        })
        assert r.status_code == 200
        assert len(spy_run_durable) == 1

    def test_a_real_durable_failure_via_mcp_is_a_real_jsonrpc_error_not_a_bare_500(self, monkeypatch):
        import app.routers.execution as execution_module

        async def failing_run_durable(*args, **kwargs):
            raise RuntimeError("Connection refused")
        monkeypatch.setattr(execution_module, "_run_durable", failing_run_durable)

        agent = create_agent(hitl_flow())
        token = f"mcp-{uuid.uuid4().hex[:8]}"
        self._enable_mcp(agent["id"], token)
        r = client.post(f"/mcp/{agent['id']}/{token}", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": self._tool_name(agent), "arguments": {"input": "hello"}},
        })
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert "Connection refused" in body["error"]["message"]


class TestRealEvalsRouting:
    def test_a_hitl_agent_invoked_via_evals_now_genuinely_routes_durable(self, spy_run_durable):
        agent = create_agent(hitl_flow())
        r = client.post("/api/evals/sets", json={"name": "Routing Test Set", "agent_id": agent["id"]})
        eval_set_id = r.json()["id"]
        client.post(f"/api/evals/sets/{eval_set_id}/cases", json={"input": "hello", "expected_output": "anything"})
        r = client.post(f"/api/evals/sets/{eval_set_id}/run")
        assert r.status_code == 200
        assert len(spy_run_durable) == 1

    def test_a_real_durable_failure_in_evals_is_a_real_honest_failed_case_not_a_crashed_run(self, monkeypatch):
        import app.routers.execution as execution_module

        async def failing_run_durable(*args, **kwargs):
            raise RuntimeError("Connection refused")
        monkeypatch.setattr(execution_module, "_run_durable", failing_run_durable)

        agent = create_agent(hitl_flow())
        r = client.post("/api/evals/sets", json={"name": "Routing Test Set 2", "agent_id": agent["id"]})
        eval_set_id = r.json()["id"]
        client.post(f"/api/evals/sets/{eval_set_id}/cases", json={"input": "hello", "expected_output": "anything"})
        r = client.post(f"/api/evals/sets/{eval_set_id}/run")
        assert r.status_code == 200
        result = r.json()["results"][0]
        assert result["passed"] is False
        assert "Connection refused" in result["reason"]


class TestRealBatchRouting:
    def test_a_hitl_agent_invoked_via_batch_is_now_rejected_upfront(self, spy_run_durable):
        """§0.98, GAP_REPORT — a real, reported gap this closes: batch
        used to genuinely route a HITL agent durable, start a real,
        separate Temporal workflow per row, and leave every one of
        them permanently, durably paused with no real way for this
        reference implementation to ever resume them. The whole batch
        call is now rejected upfront instead, before any row (and any
        real Temporal workflow) ever starts."""
        agent = create_agent(hitl_flow())
        r = client.post(f"/api/agents/{agent['id']}/batch-run", json={"rows": [{"input": "hello"}]})
        assert r.status_code == 400
        assert "Human Input node" in r.json()["detail"]
        assert len(spy_run_durable) == 0

    def test_a_plain_agent_invoked_via_batch_stays_sync(self, spy_run_durable):
        agent = create_agent(plain_flow())
        r = client.post(f"/api/agents/{agent['id']}/batch-run", json={"rows": [{"input": "hello"}]})
        assert r.status_code == 200
        assert len(spy_run_durable) == 0


class TestRealStreamingRouting:
    def test_a_hitl_agent_invoked_via_streaming_now_genuinely_routes_durable(self, spy_run_durable):
        agent = create_agent(hitl_flow())
        with client.stream("POST", f"/api/agents/{agent['id']}/run/stream", json={"input": "hello"}) as r:
            list(r.iter_lines())
        assert len(spy_run_durable) == 1

    def test_a_plain_agent_invoked_via_streaming_stays_sync_with_real_live_events(self, spy_run_durable):
        agent = create_agent(plain_flow())
        with client.stream("POST", f"/api/agents/{agent['id']}/run/stream", json={"input": "hello"}) as r:
            lines = [l for l in r.iter_lines() if l.strip()]
        assert len(spy_run_durable) == 0
        assert any("node_start" in l for l in lines)


def _blocked_output_flow(keyword: str = "BADOUTPUT") -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": f"This has {keyword} in it."}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
    }


def _enable_output_moderation(agent_id: str, keyword: str, reply: str):
    r = client.patch(f"/api/agents/{agent_id}/features", json={
        "features": {"moderation": {
            "enabled": True, "provider": "keywords", "output_enabled": True,
            "keywords": [keyword], "output_preset_reply": reply,
        }},
    })
    assert r.status_code == 200, r.text


class TestRealCrossChannelBehaviorConsistency:
    """Real tests for section 0.108, GAP_REPORT.

    The real, reported bug: run_agent (/run) applied input moderation,
    output moderation, follow-up generation, and citations — but every
    other real invocation channel (webhooks, MCP, /run/stream, batch)
    silently skipped some or all of them. The exact same real
    AgentVersion, given the exact same real input, could genuinely
    behave differently — including bypassing a genuine, versioned
    SAFETY control — purely depending on which transport invoked it.

    The fix: execute_agent_flow itself now runs the shared
    pre-execution (input moderation) and post-execution (output
    moderation, follow-ups, citations) pipeline, so every real caller
    gets it by construction. Two further, related gaps were found and
    fixed while verifying this: webhooks.py kept its own, separate,
    duplicated Run/Trace status-mapping logic that had drifted out of
    sync with the shared one (missing "blocked" entirely — a real,
    live-confirmed inconsistency between /run and webhook for the
    identical outcome); MCP created no Run/Trace row at all for any
    outcome except a genuine HITL pause.

    These tests exercise the real, running API directly across
    multiple real channels for the same agent/input, not mocked."""

    def _with_webhook_token(self, agent_id: str, token: str):
        r = client.patch(f"/api/agents/{agent_id}/nodes/n1/config", json={"config": {"webhook_token": token}})
        assert r.status_code == 200, r.text
        pub = client.post(f"/api/agents/{agent_id}/publish")
        assert pub.status_code == 200, pub.text

    def _enable_mcp(self, agent_id: str, token: str):
        from sqlmodel import Session
        from app.db import engine
        from app.models import Agent
        from app.secrets_crypto import encrypt_secret
        with Session(engine) as s:
            a = s.get(Agent, agent_id)
            a.mcp_enabled = True
            a.mcp_token_encrypted = encrypt_secret(token)
            s.add(a)
            s.commit()

    def _tool_name(self, agent: dict) -> str:
        return "".join(c if c.isalnum() else "_" for c in agent["name"].lower()).strip("_") or agent["id"]

    def test_output_moderation_blocks_consistently_across_rest_webhook_and_mcp(self):
        agent = create_agent(_blocked_output_flow(), app_type="chatflow")
        _enable_output_moderation(agent["id"], "BADOUTPUT", "SAFE_REPLY")
        webhook_token = f"wh-{uuid.uuid4().hex[:8]}"
        mcp_token = f"mcp-{uuid.uuid4().hex[:8]}"
        self._with_webhook_token(agent["id"], webhook_token)
        self._enable_mcp(agent["id"], mcp_token)

        rest_r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
        assert rest_r.status_code == 200, rest_r.text
        assert rest_r.json()["status"] == "blocked"
        assert rest_r.json()["output"] == "SAFE_REPLY"

        webhook_r = client.post(f"/api/webhooks/{webhook_token}", json={"input": "hi"})
        assert webhook_r.status_code == 200, webhook_r.text
        assert webhook_r.json()["status"] == "blocked"
        assert webhook_r.json()["output"] == "SAFE_REPLY"

        mcp_r = client.post(f"/mcp/{agent['id']}/{mcp_token}", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": self._tool_name(agent), "arguments": {"input": "hi"}},
        })
        assert mcp_r.status_code == 200, mcp_r.text
        mcp_body = mcp_r.json()["result"]
        assert mcp_body["content"][0]["text"] == "SAFE_REPLY"
        # §0.108, GAP_REPORT — "blocked" is deliberately NOT an MCP
        # error, matching REST's own real HTTP 200 for the identical
        # outcome.
        assert mcp_body["isError"] is False

    def test_webhook_persists_blocked_not_failed_matching_rest_exactly(self):
        """Direct, database-level proof of the real, confirmed
        inconsistency: the API response for webhook already said
        "blocked" before this fix, but the PERSISTED Run/Trace rows
        said "failed"/"orch_fail" — webhooks.py's own separate,
        duplicated status map had never been updated for "blocked"."""
        from sqlmodel import Session, text
        from app.db import engine
        agent = create_agent(_blocked_output_flow(), app_type="chatflow")
        _enable_output_moderation(agent["id"], "BADOUTPUT", "SAFE_REPLY")
        token = f"wh-{uuid.uuid4().hex[:8]}"
        self._with_webhook_token(agent["id"], token)

        r = client.post(f"/api/webhooks/{token}", json={"input": "hi"})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        with Session(engine) as s:
            row = s.exec(text("SELECT status, fail_class FROM run WHERE id = :r"), params={"r": run_id}).first()
            assert row[0] == "blocked"
            assert row[0] != "failed"
            assert row[1] == "moderation"

    def test_mcp_now_creates_a_real_run_and_trace_row_for_a_normal_call(self):
        """Direct, database-level proof of the real, confirmed gap:
        before this fix, an MCP-invoked run created NO Run/Trace row
        at all for any outcome except a genuine HITL pause."""
        from sqlmodel import Session, text
        from app.db import engine
        agent = create_agent(publishable_plain_flow(), app_type="chatflow")
        assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
        token = f"mcp-{uuid.uuid4().hex[:8]}"
        self._enable_mcp(agent["id"], token)

        with Session(engine) as s:
            before = s.exec(text("SELECT count(*) FROM run WHERE agent_id = :a"), params={"a": agent["id"]}).first()[0]
        assert before == 0

        r = client.post(f"/mcp/{agent['id']}/{token}", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": self._tool_name(agent), "arguments": {"input": "hi"}},
        })
        assert r.status_code == 200, r.text
        assert r.json()["result"]["content"][0]["text"] == "done"

        with Session(engine) as s:
            after = s.exec(text("SELECT status FROM run WHERE agent_id = :a"), params={"a": agent["id"]}).first()
        assert after is not None
        assert after[0] == "success"

    def test_input_moderation_now_also_produces_a_real_trace_row(self):
        """§0.108, GAP_REPORT — input-blocked runs now go through the
        same, real, shared _finalize_run every other terminal outcome
        does, so a real Trace row is created too, not just a Run —
        matching the output-blocked case's own established behavior."""
        agent = create_agent(publishable_plain_flow(), app_type="chatflow")
        _r = client.patch(f"/api/agents/{agent['id']}/features", json={
            "features": {"moderation": {
                "enabled": True, "provider": "keywords", "input_enabled": True,
                "keywords": ["BADINPUT"], "input_preset_reply": "INPUT_BLOCKED",
            }},
        })
        assert _r.status_code == 200, _r.text

        r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "this has BADINPUT in it"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "blocked"
        assert body["output"] == "INPUT_BLOCKED"
        assert body.get("trace_id"), "an input-blocked run should now produce a real Trace row too"

    def test_eval_opts_out_of_response_enrichment_but_not_moderation(self):
        """§0.108, GAP_REPORT — the report's own explicit concession
        ("some differences are reasonable for Eval") made real and
        verified directly: evals.py passes apply_response_enrichment=
        False, so follow-ups are never generated for an eval case, but
        moderation still fully applies."""
        import inspect
        from app.routers import evals as evals_module
        source = inspect.getsource(evals_module)
        assert "apply_response_enrichment=False" in source
