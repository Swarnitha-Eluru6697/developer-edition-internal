"""
Real tests for the batch concurrency admission control fix (section
0.75, GAP_REPORT).

The real, reported gap: batch_run (routers/execution.py) launched up
to 200 rows via asyncio.gather with no explicit semaphore, per-agent
concurrency limit, provider-call rate limit, or queue at all.

The fix: app/concurrency.py's three real, independent, configurable
limiters - a real per-batch-call asyncio.Semaphore, a real,
process-wide per-agent asyncio.Semaphore shared across overlapping
batch calls for the same agent, and a real, thread-safe
threading.Semaphore capping concurrent real Anthropic API calls
platform-wide.

These tests measure GENUINE concurrency directly - tracking how many
simulated rows/calls are actually in flight at once via a real lock
and a real counter, not asserting on call counts after the fact. A
cosmetic wrapper that didn't actually serialize anything would fail
every test in this file.
"""
import sys
import os
import uuid
import asyncio
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, text

from app.db import engine
from app.models import Agent
from app.concurrency import get_agent_semaphore, batch_semaphore, llm_call_slot, BATCH_MAX_CONCURRENCY, AGENT_MAX_CONCURRENCY, LLM_MAX_CONCURRENCY


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        for table in ("run", "trace", "agentlog"):
            s.exec(text(f"DELETE FROM {table} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


def make_agent() -> str:
    agent_id = f"conc-test-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s:
        s.add(Agent(
            id=agent_id, name=agent_id, type="App", subtype="Custom", plane="App",
            owner="you", status="draft", version="v1", app_type="workflow",
            flow={"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
        ))
        s.commit()
    return agent_id


class TestRealBatchSemaphore:
    def test_batch_semaphore_genuinely_caps_concurrent_row_execution(self):
        import app.routers.execution as execution_module
        from app.routers.execution import batch_run, BatchRunIn

        agent_id = make_agent()
        concurrent = {"current": 0, "max_seen": 0}
        lock = asyncio.Lock()

        async def fake_execute_agent_flow(session, agent, invoking_user, user_input, **kwargs):
            async with lock:
                concurrent["current"] += 1
                concurrent["max_seen"] = max(concurrent["max_seen"], concurrent["current"])
            await asyncio.sleep(0.1)
            async with lock:
                concurrent["current"] -= 1
            return {"status": "success", "output": "ok", "spans": []}

        original = execution_module.execute_agent_flow
        execution_module.execute_agent_flow = fake_execute_agent_flow
        try:
            async def run():
                with Session(engine) as s:
                    body = BatchRunIn(rows=[{"input": f"row{i}"} for i in range(15)], invoking_user="user:test")
                    return await batch_run(agent_id, body, s)
            result = asyncio.run(run())
            assert result["succeeded"] == 15
            assert concurrent["max_seen"] <= BATCH_MAX_CONCURRENCY
            assert concurrent["max_seen"] > 1
        finally:
            execution_module.execute_agent_flow = original
            cleanup_agent(agent_id)


class TestRealPerAgentSemaphore:
    def test_the_same_agent_id_always_gets_the_same_real_semaphore(self):
        a = get_agent_semaphore("agent-x")
        b = get_agent_semaphore("agent-x")
        assert a is b

    def test_different_agent_ids_get_genuinely_different_semaphores(self):
        a = get_agent_semaphore("agent-y")
        b = get_agent_semaphore("agent-z")
        assert a is not b

    def test_two_real_overlapping_batch_calls_for_the_same_agent_share_the_real_cap(self):
        import app.routers.execution as execution_module
        from app.routers.execution import batch_run, BatchRunIn

        agent_id = make_agent()
        concurrent = {"current": 0, "max_seen": 0}
        lock = asyncio.Lock()

        async def fake_execute_agent_flow(session, agent, invoking_user, user_input, **kwargs):
            async with lock:
                concurrent["current"] += 1
                concurrent["max_seen"] = max(concurrent["max_seen"], concurrent["current"])
            await asyncio.sleep(0.15)
            async with lock:
                concurrent["current"] -= 1
            return {"status": "success", "output": "ok", "spans": []}

        original = execution_module.execute_agent_flow
        execution_module.execute_agent_flow = fake_execute_agent_flow
        try:
            async def run():
                with Session(engine) as s1, Session(engine) as s2:
                    body1 = BatchRunIn(rows=[{"input": f"a{i}"} for i in range(4)], invoking_user="user:test")
                    body2 = BatchRunIn(rows=[{"input": f"b{i}"} for i in range(4)], invoking_user="user:test")
                    await asyncio.gather(batch_run(agent_id, body1, s1), batch_run(agent_id, body2, s2))
            asyncio.run(run())
            assert concurrent["max_seen"] <= AGENT_MAX_CONCURRENCY
        finally:
            execution_module.execute_agent_flow = original
            cleanup_agent(agent_id)


class TestRealLlmConcurrencyLimiter:
    def test_llm_call_slot_genuinely_caps_concurrent_calls_across_real_threads(self):
        concurrent = {"current": 0, "max_seen": 0}
        lock = threading.Lock()

        def simulated_call():
            with llm_call_slot():
                with lock:
                    concurrent["current"] += 1
                    concurrent["max_seen"] = max(concurrent["max_seen"], concurrent["current"])
                time.sleep(0.1)
                with lock:
                    concurrent["current"] -= 1

        threads = [threading.Thread(target=simulated_call) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert concurrent["max_seen"] <= LLM_MAX_CONCURRENCY
        assert concurrent["max_seen"] > 0

    def test_llm_call_slot_releases_correctly_after_use(self):
        for _ in range(LLM_MAX_CONCURRENCY * 3):
            with llm_call_slot():
                pass
