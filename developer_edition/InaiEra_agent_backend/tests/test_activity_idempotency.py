"""
Real tests for the Temporal activity idempotency/retry design fix
(section 0.76, GAP_REPORT).

The real, reported gap: Activities perform real side effects (HTTP
calls, memory writes, chat-history writes, logs, sub-agent calls,
future connector calls) but the execution code defined no clear
activity-level idempotency strategy or explicit, per-node-category
retry policy.

The fix: a real ActivityIdempotency ledger, a real idempotent_once()
helper wired into the one concrete existing non-idempotent side effect
this platform has today (chat-history append), a real Idempotency-Key
HTTP header pass-through, a real per-node-category RetryPolicy
classifier matching the report's own worked example, and a real,
stable-across-retries idempotency key computation.

These tests exercise the real functions directly - not mocked, except
where a real Temporal workflow execution context is genuinely required
(workflow.info()) and this environment has no real Temporal test
server available (confirmed directly: the embedded test server's own
download endpoint isn't reachable under this sandbox's network
policy).
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch, MagicMock
from sqlmodel import Session, text, select

from app.db import engine
from app.harness import idempotent_once, upsert_agent_memory, AgentMemory, handle_http
from app.temporal_workflow import _retry_policy_for_node, _idempotency_key_for


class TestRealRetryPolicyClassifier:
    def test_llm_touching_types_are_retryable(self):
        for t in ("llm", "agent", "subagent", "classifier", "parameter_extractor"):
            p = _retry_policy_for_node({"type": t, "config": {}})
            assert p.maximum_attempts > 1, f"{t} should be retryable"

    def test_http_get_is_possibly_retryable(self):
        p = _retry_policy_for_node({"type": "http", "config": {"method": "GET"}})
        assert p.maximum_attempts > 1
        p2 = _retry_policy_for_node({"type": "http", "config": {"method": "HEAD"}})
        assert p2.maximum_attempts > 1

    def test_http_post_is_not_automatically_retryable(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            p = _retry_policy_for_node({"type": "http", "config": {"method": method}})
            assert p.maximum_attempts == 1, f"{method} must not auto-retry"

    def test_tool_mutation_requires_idempotency_so_no_auto_retry_by_default(self):
        p = _retry_policy_for_node({"type": "tool", "config": {}})
        assert p.maximum_attempts == 1

    def test_memory_append_is_safe_to_retry_since_it_is_now_idempotent(self):
        p = _retry_policy_for_node({"type": "memory", "config": {}})
        assert p.maximum_attempts > 1
        p2 = _retry_policy_for_node({"type": "variable_assigner", "config": {}})
        assert p2.maximum_attempts > 1

    def test_no_real_node_type_ever_gets_unbounded_retries(self):
        for t in ("llm", "http", "tool", "memory", "code", "template", "output", "respond"):
            p = _retry_policy_for_node({"type": t, "config": {"method": "GET"}})
            assert p.maximum_attempts > 0


class TestRealIdempotentOnce:
    def test_a_repeated_key_prevents_real_re_execution_and_replays_the_cached_result(self):
        key = f"test-idem-{uuid.uuid4().hex[:8]}"
        call_count = {"n": 0}

        def side_effect():
            call_count["n"] += 1
            return {"value": f"call {call_count['n']}"}

        with Session(engine) as s:
            r1 = idempotent_once(s, key, side_effect)
            r2 = idempotent_once(s, key, side_effect)
            r3 = idempotent_once(s, key, side_effect)

        assert call_count["n"] == 1
        assert r1 == r2 == r3
        with Session(engine) as s:
            s.exec(text("DELETE FROM activityidempotency WHERE id = :k"), params={"k": key})
            s.commit()

    def test_a_falsy_key_always_calls_through_directly(self):
        call_count = {"n": 0}

        def side_effect():
            call_count["n"] += 1
            return call_count["n"]

        with Session(engine) as s:
            r1 = idempotent_once(s, None, side_effect)
            r2 = idempotent_once(s, None, side_effect)

        assert call_count["n"] == 2
        assert r1 == 1 and r2 == 2

    def test_different_keys_are_genuinely_independent(self):
        key_a = f"test-idem-a-{uuid.uuid4().hex[:8]}"
        key_b = f"test-idem-b-{uuid.uuid4().hex[:8]}"
        call_count = {"n": 0}

        def side_effect():
            call_count["n"] += 1
            return call_count["n"]

        with Session(engine) as s:
            idempotent_once(s, key_a, side_effect)
            idempotent_once(s, key_b, side_effect)

        assert call_count["n"] == 2
        with Session(engine) as s:
            s.exec(text("DELETE FROM activityidempotency WHERE id IN (:a, :b)"), params={"a": key_a, "b": key_b})
            s.commit()


class TestRealChatHistoryAppendIdempotency:
    def test_the_bug_was_real_without_idempotent_once(self):
        scope_key = f"chat-noidem-{uuid.uuid4().hex[:8]}"

        def append_without_fix(session, task, final_text):
            history_row = session.exec(select(AgentMemory).where(
                AgentMemory.scope_key == scope_key, AgentMemory.key == "__llm_chat_history__")).first()
            prior = history_row.value if history_row else ""
            new_history = f"{prior}\nUser: {task}\nAssistant: {final_text}".strip()
            upsert_agent_memory(session, scope_key, "__llm_chat_history__", new_history)
            session.commit()

        with Session(engine) as s:
            append_without_fix(s, "hello", "hi")
        with Session(engine) as s:
            append_without_fix(s, "hello", "hi")

        with Session(engine) as s:
            row = s.exec(text("SELECT value FROM agentmemory WHERE scope_key = :a AND key = '__llm_chat_history__'"), params={"a": scope_key}).first()
            assert row[0].count("User: hello") == 2
            s.exec(text("DELETE FROM agentmemory WHERE scope_key = :a"), params={"a": scope_key})
            s.commit()

    def test_the_fix_genuinely_closes_it_with_idempotent_once(self):
        scope_key = f"chat-idem-{uuid.uuid4().hex[:8]}"
        idem_key = "wf-fake-123:node1:chat_history"

        def append_with_fix(session, task, final_text):
            history_row = session.exec(select(AgentMemory).where(
                AgentMemory.scope_key == scope_key, AgentMemory.key == "__llm_chat_history__")).first()

            def _append():
                prior = history_row.value if history_row else ""
                new_history = f"{prior}\nUser: {task}\nAssistant: {final_text}".strip()
                upsert_agent_memory(session, scope_key, "__llm_chat_history__", new_history)
                return {"appended": True}
            idempotent_once(session, idem_key, _append)
            session.commit()

        with Session(engine) as s:
            append_with_fix(s, "hello", "hi")
        with Session(engine) as s:
            append_with_fix(s, "hello", "hi")

        with Session(engine) as s:
            row = s.exec(text("SELECT value FROM agentmemory WHERE scope_key = :a AND key = '__llm_chat_history__'"), params={"a": scope_key}).first()
            assert row[0].count("User: hello") == 1
            s.exec(text("DELETE FROM agentmemory WHERE scope_key = :a"), params={"a": scope_key})
            s.exec(text("DELETE FROM activityidempotency WHERE id = :k"), params={"k": idem_key})
            s.commit()


class TestRealHttpIdempotencyKeyHeader:
    def _call_with_capture(self, cfg, ctx):
        captured = {}

        def fake_safe_http_request(method, url, **kwargs):
            captured["headers"] = kwargs.get("headers") or {}
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "ok"
            resp.headers = {}
            resp.json.side_effect = ValueError()
            return resp

        with patch("app.egress.safe_http_request", side_effect=fake_safe_http_request):
            handle_http(cfg, ctx)
        return captured["headers"]

    def test_a_mutating_method_with_a_real_idempotency_key_gets_the_real_header(self):
        headers = self._call_with_capture(
            {"method": "POST", "url": "https://example.com/api", "headers_text": ""},
            {"vars": {}, "_idempotency_key": "wf-1:node-1"},
        )
        assert headers.get("Idempotency-Key") == "wf-1:node-1"

    def test_a_safe_method_never_gets_the_header_even_with_a_key(self):
        headers = self._call_with_capture(
            {"method": "GET", "url": "https://example.com/api", "headers_text": ""},
            {"vars": {}, "_idempotency_key": "wf-1:node-2"},
        )
        assert "Idempotency-Key" not in headers

    def test_no_key_present_sync_path_never_attaches_the_header(self):
        headers = self._call_with_capture(
            {"method": "POST", "url": "https://example.com/api", "headers_text": ""},
            {"vars": {}},
        )
        assert "Idempotency-Key" not in headers

    def test_an_explicit_person_configured_header_is_never_overridden(self):
        headers = self._call_with_capture(
            {"method": "POST", "url": "https://example.com/api", "headers_text": "Idempotency-Key: my-own-key"},
            {"vars": {}, "_idempotency_key": "wf-1:node-4"},
        )
        assert headers.get("Idempotency-Key") == "my-own-key"


class TestRealIdempotencyKeyComputation:
    def test_the_same_node_with_no_iteration_is_stable_across_real_retries(self):
        fake_info = MagicMock()
        fake_info.workflow_id = "wf-real-abc123"
        with patch("app.temporal_workflow.workflow.info", return_value=fake_info):
            k1 = _idempotency_key_for("node-5")
            k2 = _idempotency_key_for("node-5")
        assert k1 == k2 == "wf-real-abc123:node-5"

    def test_the_same_iteration_is_stable_across_real_retries(self):
        fake_info = MagicMock()
        fake_info.workflow_id = "wf-real-abc123"
        with patch("app.temporal_workflow.workflow.info", return_value=fake_info):
            k1 = _idempotency_key_for("node-5", iteration=2)
            k2 = _idempotency_key_for("node-5", iteration=2)
        assert k1 == k2 == "wf-real-abc123:node-5:2"

    def test_different_iterations_of_the_same_node_are_genuinely_distinct(self):
        fake_info = MagicMock()
        fake_info.workflow_id = "wf-real-abc123"
        with patch("app.temporal_workflow.workflow.info", return_value=fake_info):
            k1 = _idempotency_key_for("node-5", iteration=2)
            k2 = _idempotency_key_for("node-5", iteration=3)
        assert k1 != k2
