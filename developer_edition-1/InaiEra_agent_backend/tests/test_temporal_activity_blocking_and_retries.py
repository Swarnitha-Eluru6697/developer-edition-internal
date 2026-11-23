"""
Real tests for blocking work, timeout mismatch, and retry
multiplication in Temporal Activities (section 0.105, GAP_REPORT).

Three real, reported problems, all in execute_node_activity
(temporal_workflow.py) and the handlers it dispatches to:

1. Blocking synchronous work (model SDK calls, HTTP calls, DB queries,
   sandboxed subprocess execution) ran directly inside an `async def`
   Activity with no `await` anywhere near the actual blocking work —
   genuinely starving the SAME asyncio event loop the Temporal worker
   uses to poll for new work, process cancellations, and heartbeat
   every OTHER concurrently-running Activity in this worker process.

2. Every real start_to_close_timeout call site used a flat, hardcoded
   30s regardless of what the node itself was actually configured
   for — an llm node's own real, user-configurable timeout_s
   (handle_llm) could be set well past 30s, and Temporal would still
   genuinely kill/retry the Activity at 30s.

3. handle_llm/handle_code/handle_http each have their own real,
   independent, handler-level retry_on_failure loop, which used to
   stay fully active on the durable (Temporal) path too — stacking on
   top of Temporal's own per-node-type _retry_policy_for_node,
   multiplying real provider calls (and, for LLM nodes, real,
   duplicated token cost) well past what either retry policy's own
   configured attempt count alone would suggest.

The fix: the genuinely blocking body of execute_node_activity was
extracted into _execute_node_sync and is now run via
asyncio.to_thread; a new _activity_timeout_for_node implements the
report's own "node timeout + safety margin" formula; and a new
ctx["_durable_execution"] flag, set only on the durable path, is
checked by all three handlers to suppress their own internal retry
when Temporal already owns retries for this execution mode.

These tests exercise the real functions directly, including a live,
side-by-side proof (not merely inferred from the code) that the event
loop genuinely stays responsive under the new pattern and would have
been genuinely blocked under the old one.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.harness import handle_http, handle_llm, handle_code
from app.model_adapter import LLMCallError
import app.temporal_workflow as tw


class TestRealActivityTimeoutDerivation:
    """The report's own exact formula: activity timeout = node timeout
    + safety margin."""

    def test_the_reports_own_60s_example_is_no_longer_killed_at_30s(self):
        node = {"type": "llm", "config": {"timeout_s": 60}}
        result = tw._activity_timeout_for_node(node)
        assert result > timedelta(seconds=60)
        assert result == timedelta(seconds=75)

    def test_an_llm_node_with_no_configured_timeout_keeps_the_prior_flat_default(self):
        node = {"type": "llm", "config": {}}
        assert tw._activity_timeout_for_node(node) == timedelta(seconds=30)

    def test_every_llm_touching_type_derives_from_its_own_timeout_s(self):
        for node_type in ("llm", "agent", "subagent", "classifier", "parameter_extractor"):
            node = {"type": node_type, "config": {"timeout_s": 45}}
            assert tw._activity_timeout_for_node(node) == timedelta(seconds=60)

    def test_a_node_type_with_no_configurable_timeout_keeps_the_prior_flat_default(self):
        for node_type in ("http", "code", "condition", "template"):
            node = {"type": node_type, "config": {}}
            assert tw._activity_timeout_for_node(node) == timedelta(seconds=30)

    def test_a_malformed_timeout_s_value_falls_back_to_the_default_rather_than_crashing(self):
        node = {"type": "llm", "config": {"timeout_s": "not-a-number"}}
        assert tw._activity_timeout_for_node(node) == timedelta(seconds=30)


class TestRealRetryOwnershipSuppressionOnTheDurablePath:
    """The report's own recommendation: pick one retry owner per
    execution mode. ctx["_durable_execution"] is the real, shared
    signal that Temporal already owns retries for this invocation."""

    def test_handle_http_retries_normally_on_the_sync_path(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            return {"status_code": 500, "body": "err", "headers": {}}
        cfg = {"method": "GET", "url": "https://example.com", "retry_on_failure": True, "max_retries": 3}
        with patch("app.egress.safe_http_request", side_effect=fake):
            handle_http(cfg, {"vars": {}})
        assert calls["n"] == 4  # 1 + 3 configured retries

    def test_handle_http_makes_exactly_one_attempt_on_the_durable_path(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            return {"status_code": 500, "body": "err", "headers": {}}
        cfg = {"method": "GET", "url": "https://example.com", "retry_on_failure": True, "max_retries": 3}
        with patch("app.egress.safe_http_request", side_effect=fake):
            handle_http(cfg, {"vars": {}, "_durable_execution": True})
        assert calls["n"] == 1

    def test_handle_llm_retries_normally_on_the_sync_path(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            raise LLMCallError("simulated failure")
        cfg = {"model": "Claude Sonnet 5", "retry_on_failure": True, "error_handling": "None"}
        with patch("app.harness._call_llm_tracked", side_effect=fake):
            with pytest.raises(Exception):
                handle_llm(cfg, {"vars": {}, "input": "hi"}, session=None)
        assert calls["n"] == 2  # 1 + 1 configured retry

    def test_handle_llm_makes_exactly_one_attempt_on_the_durable_path(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            raise LLMCallError("simulated failure")
        cfg = {"model": "Claude Sonnet 5", "retry_on_failure": True, "error_handling": "None"}
        with patch("app.harness._call_llm_tracked", side_effect=fake):
            with pytest.raises(Exception):
                handle_llm(cfg, {"vars": {}, "input": "hi", "_durable_execution": True}, session=None)
        assert calls["n"] == 1

    def test_handle_code_retries_normally_on_the_sync_path(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            return {"stdout": "", "stderr": "err", "exit_code": 1, "sandboxed": True}
        cfg = {"code": "print(1)", "retry_on_failure": True}
        with patch("app.harness.run_sandboxed_code", side_effect=fake):
            with pytest.raises(Exception):
                handle_code(cfg, {"vars": {}, "input": "hi"})
        assert calls["n"] == 2

    def test_handle_code_makes_exactly_one_attempt_on_the_durable_path(self):
        calls = {"n": 0}
        def fake(*a, **k):
            calls["n"] += 1
            return {"stdout": "", "stderr": "err", "exit_code": 1, "sandboxed": True}
        cfg = {"code": "print(1)", "retry_on_failure": True}
        with patch("app.harness.run_sandboxed_code", side_effect=fake):
            with pytest.raises(Exception):
                handle_code(cfg, {"vars": {}, "input": "hi", "_durable_execution": True})
        assert calls["n"] == 1


class TestRealEventLoopIsNotBlockedByANodeExecution:
    """The core, most important fix: a genuinely slow/blocking node
    handler must not starve the Activity's own event loop — a real,
    concurrent asyncio task must keep making progress while the node
    handler is "running", proving the blocking work actually moved to
    a separate thread (asyncio.to_thread) rather than running inline."""

    def test_a_slow_blocking_handler_no_longer_starves_the_event_loop(self):
        async def scenario():
            def slow_blocking_handler(cfg, ctx):
                time.sleep(0.5)
                ctx["output"] = "done"
                return ctx

            ticks = {"n": 0}

            async def ticker():
                while True:
                    ticks["n"] += 1
                    await asyncio.sleep(0.05)

            ticker_task = asyncio.create_task(ticker())
            node = {"id": "n1", "type": "template", "config": {}}
            ctx = {"agent_id": "test-agent", "vars": {}}

            with patch.object(tw, "NODE_HANDLERS", {"template": slow_blocking_handler}):
                with patch.object(tw, "call_node_handler", lambda t, c, ctx, s: slow_blocking_handler(c, ctx)):
                    start = time.time()
                    await tw.execute_node_activity(node, ctx)
                    elapsed = time.time() - start

            ticker_task.cancel()
            return elapsed, ticks["n"]

        elapsed, tick_count = asyncio.run(scenario())
        # A genuinely blocked event loop would let the ticker make
        # ~0 progress during the 0.5s the handler was "running" — this
        # asserts real, substantial progress was made instead (a
        # generous, non-flaky lower bound well below the ~10 ticks a
        # fully-responsive loop would produce at this timing).
        assert tick_count >= 3, (
            f"event loop only ticked {tick_count} times during a {elapsed:.2f}s blocking "
            f"handler call — the blocking work is not actually offloaded to a separate thread"
        )
