"""Parallel graph nodes must not share one database Session.

The bug: `_execute_flow` closed over a single `Session` and
`exec_node` handed that same object to every handler. When a wave
contained more than one plain node, `run_graph_concurrent` ran them on
a ThreadPoolExecutor — so two threads used one SQLAlchemy Session at
the same time. Sessions are explicitly not thread-safe, and every
handler that touches persistence was exposed: memory, retrieval, llm
with chat history, agent, sub-agent, parameter extraction, variable
assignment.

What that costs is not a clean error. It is one node's pending rows
flushed inside another's transaction, a rollback discarding work
neither node knew about, or `IllegalStateChangeError` — intermittently,
under concurrency, and rarely in a test that runs one node at a time.

The iteration handler already opened a Session per parallel item and
closed it afterwards; the top-level scheduler now does the same, so a
node executed concurrently gets its own session and the run's own
session is used only where nothing runs beside it.

These tests use the real Postgres this platform runs against — the
point is the concurrency, and an in-memory stand-in with different
threading behaviour would prove nothing about it.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import harness
from app.db import engine
from app.main import app
from app.models import Agent, AgentMemory

client = TestClient(app)

# Trigger fans out to two memory nodes, which then join on respond —
# exactly the shape from the report. Two DB-writing nodes with no
# dependency on each other is the minimum that makes the scheduler run
# them at the same time.
PARALLEL_MEMORY_FLOW = {
    "nodes": [
        {"id": "trigger", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
        {"id": "memA", "type": "memory", "x": 0, "y": 0, "sub": "", "config": {
            "key": "parallel_a", "mode": "write", "scope": "agent", "write_value": "value-A"}},
        {"id": "memB", "type": "memory", "x": 0, "y": 0, "sub": "", "config": {
            "key": "parallel_b", "mode": "write", "scope": "agent", "write_value": "value-B"}},
        {"id": "respond", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source": "trigger", "target": "memA"},
        {"id": "e2", "source": "trigger", "target": "memB"},
        {"id": "e3", "source": "memA", "target": "respond"},
        {"id": "e4", "source": "memB", "target": "respond"},
    ],
}


@pytest.fixture
def parallel_agent():
    r = client.post("/api/agents", json={"name": "Parallel session test", "subtype": "Workflow",
                                         "app_type": "workflow"})
    assert r.status_code == 200, r.text
    agent_id = r.json()["id"]
    from sqlalchemy.orm.attributes import flag_modified
    with Session(engine) as s:
        row = s.get(Agent, agent_id)
        row.flow = PARALLEL_MEMORY_FLOW
        flag_modified(row, "flow")
        s.add(row)
        s.commit()
    yield agent_id
    # Leave nothing behind in the live database this runs against.
    with Session(engine) as s:
        for mem in s.exec(select(AgentMemory).where(AgentMemory.scope_key == agent_id)).all():
            s.delete(mem)
        row = s.get(Agent, agent_id)
        if row:
            row.status = "draft"
            s.add(row)
            s.commit()
        s.commit()
    from app.routers.agents import delete_agent
    with Session(engine) as s:
        try:
            delete_agent(agent_id, s)
        except Exception:
            pass


def _run(agent_id: str, text: str = "go") -> dict:
    r = client.post(f"/api/agents/{agent_id}/run", json={"input": text})
    assert r.status_code == 200, r.text
    return r.json()


class TestEachConcurrentNodeGetsItsOwnSession:
    """The regression guard. Running the flow and seeing it pass proves
    little on its own — the race is intermittent, and the broken version
    succeeds most of the time. What is asserted here is the property
    that makes it safe: two nodes running at once never hold the same
    Session."""

    def test_two_parallel_nodes_receive_different_sessions(self, parallel_agent, monkeypatch):
        seen = {}
        lock = threading.Lock()
        original = harness.call_node_handler

        def recording(node_type, cfg, ctx, session):
            with lock:
                seen.setdefault(node_type, []).append(id(session))
            return original(node_type, cfg, ctx, session)

        monkeypatch.setattr(harness, "call_node_handler", recording)
        _run(parallel_agent)

        memory_sessions = seen.get("memory", [])
        assert len(memory_sessions) == 2, f"expected both memory nodes to run, saw {seen}"
        assert memory_sessions[0] != memory_sessions[1], (
            "both parallel memory nodes were handed the same Session object — "
            "this is the bug, and SQLAlchemy sessions are not thread-safe"
        )

    def test_a_lone_node_still_uses_the_runs_own_session(self, parallel_agent, monkeypatch):
        """Nothing runs beside a solo node, so there is nothing to share
        with — opening a second session for it would be pure cost. The
        trigger and respond nodes are alone in their waves."""
        seen = {}
        original = harness.call_node_handler

        def recording(node_type, cfg, ctx, session):
            seen.setdefault(node_type, []).append(id(session))
            return original(node_type, cfg, ctx, session)

        monkeypatch.setattr(harness, "call_node_handler", recording)
        _run(parallel_agent)

        # trigger and respond each run alone, so they share the run's
        # session with each other and with nobody else.
        assert seen["trigger"] == seen["respond"], seen
        # And that session is not one of the per-node ones.
        assert seen["trigger"][0] not in seen["memory"]

    def test_the_per_node_session_is_closed_when_the_node_finishes(self, parallel_agent, monkeypatch):
        """Ten parallel nodes must not each hold a pooled connection for
        the length of the whole wave."""
        closed = []
        real_close = Session.close

        def tracking_close(self):
            closed.append(id(self))
            return real_close(self)

        monkeypatch.setattr(Session, "close", tracking_close)

        opened = []
        original = harness.call_node_handler

        def recording(node_type, cfg, ctx, session):
            if node_type == "memory":
                opened.append(id(session))
            return original(node_type, cfg, ctx, session)

        monkeypatch.setattr(harness, "call_node_handler", recording)
        _run(parallel_agent)

        assert len(opened) == 2
        for session_id in opened:
            assert session_id in closed, "a per-node session was left open"


class TestTheWritesActuallyLand:
    def test_both_parallel_writes_persist(self, parallel_agent):
        _run(parallel_agent)
        with Session(engine) as s:
            rows = {m.key: m.value for m in s.exec(
                select(AgentMemory).where(AgentMemory.scope_key == parallel_agent)).all()}
        assert rows.get("parallel_a") == "value-A"
        assert rows.get("parallel_b") == "value-B"

    def test_repeated_runs_never_lose_a_write_or_raise(self, parallel_agent):
        """The report's own reproduction: run it repeatedly against a
        real Postgres with the thread pool active. A shared session
        shows up here as an occasional failed run or a memory row that
        silently did not update — so every iteration is checked, not
        just the last."""
        runs = int(os.getenv("PARALLEL_SESSION_TEST_RUNS", "15"))
        for attempt in range(runs):
            result = _run(parallel_agent, text=f"run-{attempt}")
            assert result.get("status") in ("ok", "success"), (
                f"run {attempt} failed: {result.get('spans') or result}"
            )
            spans = {s.get("node_id"): s for s in (result.get("spans") or []) if s.get("node_id")}
            for node_id in ("memA", "memB"):
                assert spans.get(node_id, {}).get("status") == "ok", (
                    f"run {attempt}: {node_id} -> {spans.get(node_id)}"
                )
            with Session(engine) as s:
                rows = {m.key: m.value for m in s.exec(
                    select(AgentMemory).where(AgentMemory.scope_key == parallel_agent)).all()}
            assert rows.get("parallel_a") == "value-A", f"run {attempt}: {rows}"
            assert rows.get("parallel_b") == "value-B", f"run {attempt}: {rows}"


class TestTheSchedulerContract:
    def test_the_scheduler_takes_a_session_factory(self):
        """A factory, not a session: one instance handed to the
        scheduler would be the same object every thread received, which
        is the bug this fixes."""
        import inspect
        params = inspect.signature(harness.run_graph_concurrent).parameters
        assert "session_factory" in params
        assert params["session_factory"].default is None

    def test_without_a_factory_nothing_is_shared_because_nothing_is_passed(self):
        """A caller with no database session (a validation or preview
        run) keeps working unchanged — the node simply runs without
        one."""
        handled = []

        def run_one(node, node_ctx, node_session=None):
            handled.append((node["id"], node_session))

        ctx = {"vars": {}, "input": "", "output": None}
        harness.run_graph_concurrent(
            "trigger",
            {n["id"]: n for n in PARALLEL_MEMORY_FLOW["nodes"]},
            PARALLEL_MEMORY_FLOW["edges"],
            run_one, ctx, {"count": 0},
        )
        assert [node_id for node_id, _ in handled] == ["trigger", "memA", "memB", "respond"] or \
               sorted(node_id for node_id, _ in handled) == ["memA", "memB", "respond", "trigger"]
        assert all(session is None for _n, session in handled)

    def test_with_a_factory_only_the_concurrent_nodes_get_one(self):
        made = []

        def factory():
            session = Session(engine)
            made.append(session)
            return session

        handed = {}

        def run_one(node, node_ctx, node_session=None):
            handed[node["id"]] = node_session

        ctx = {"vars": {}, "input": "", "output": None}
        harness.run_graph_concurrent(
            "trigger",
            {n["id"]: n for n in PARALLEL_MEMORY_FLOW["nodes"]},
            PARALLEL_MEMORY_FLOW["edges"],
            run_one, ctx, {"count": 0}, None, factory,
        )
        # memA and memB are the only wave with more than one plain node.
        assert handed["memA"] is not None and handed["memB"] is not None
        assert handed["memA"] is not handed["memB"]
        assert handed["trigger"] is None and handed["respond"] is None
        assert len(made) == 2


# The report's release-verification criterion names three node types, not
# one: "parallel Memory/Retrieval/LLM-history nodes". They are named
# individually because they reach persistence in three different ways, and
# a shared Session corrupts each differently:
#
#   memory    — writes AgentMemory rows (INSERT/UPDATE inside the wave)
#   retrieval — reads KnowledgeBase to resolve the node's KB reference
#   llm       — with memory_enabled, SELECTs prior ChatMessage rows and
#               INSERTs new ones for this turn
#
# A test covering only memory proves the scheduler hands out separate
# sessions; it does not prove that the three handlers which actually
# touch different tables survive running at once. This wave runs all
# three concurrently, repeatedly, against the real Postgres.
MIXED_PARALLEL_FLOW = {
    "nodes": [
        {"id": "trigger", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
        {"id": "memA", "type": "memory", "x": 0, "y": 0, "sub": "", "config": {
            "key": "mixed_mem", "mode": "write", "scope": "agent", "write_value": "mem-value"}},
        {"id": "ret", "type": "retrieval", "x": 0, "y": 0, "sub": "", "config": {
            "knowledge_bases": ["Parallel Session Probe KB"], "top_k": 3}},
        {"id": "llm", "type": "llm", "x": 0, "y": 0, "sub": "", "config": {
            "prompt": "Summarise: {{trigger_input}}", "memory_enabled": True}},
        {"id": "respond", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source": "trigger", "target": "memA"},
        {"id": "e2", "source": "trigger", "target": "ret"},
        {"id": "e3", "source": "trigger", "target": "llm"},
        {"id": "e4", "source": "memA", "target": "respond"},
        {"id": "e5", "source": "ret", "target": "respond"},
        {"id": "e6", "source": "llm", "target": "respond"},
    ],
}

PROBE_KB_NAME = "Parallel Session Probe KB"
PROBE_KB_ID = "parallel_session_probe_kb"


@pytest.fixture
def mixed_agent():
    """A registered KB makes the retrieval node do a real KnowledgeBase
    read on its own session. Left deliberately non-pgvector: the point
    here is concurrent session use, and standing up a real vector table
    would add a second failure mode without testing anything more about
    thread safety."""
    from sqlalchemy.orm.attributes import flag_modified
    from app.models import ChatMessage, KnowledgeBase

    with Session(engine) as s:
        if not s.get(KnowledgeBase, PROBE_KB_ID):
            s.add(KnowledgeBase(id=PROBE_KB_ID, name=PROBE_KB_NAME, provider="pgvector",
                                config={}, status="connected"))
            s.commit()

    r = client.post("/api/agents", json={"name": "Mixed parallel session test",
                                         "subtype": "Workflow", "app_type": "workflow"})
    assert r.status_code == 200, r.text
    agent_id = r.json()["id"]
    with Session(engine) as s:
        row = s.get(Agent, agent_id)
        row.flow = MIXED_PARALLEL_FLOW
        flag_modified(row, "flow")
        s.add(row)
        s.commit()
    yield agent_id

    with Session(engine) as s:
        for mem in s.exec(select(AgentMemory).where(AgentMemory.scope_key == agent_id)).all():
            s.delete(mem)
        for msg in s.exec(select(ChatMessage).where(ChatMessage.agent_id == agent_id)).all():
            s.delete(msg)
        kb = s.get(KnowledgeBase, PROBE_KB_ID)
        if kb:
            s.delete(kb)
        row = s.get(Agent, agent_id)
        if row:
            row.status = "draft"
            s.add(row)
        s.commit()
    from app.routers.agents import delete_agent
    with Session(engine) as s:
        try:
            delete_agent(agent_id, s)
        except Exception:
            pass


class TestTheThreeDbTouchingNodeTypesInOneWave:
    """The report's named release criterion, as an actual test."""

    def test_memory_retrieval_and_llm_history_each_get_their_own_session(self, mixed_agent, monkeypatch):
        seen = {}
        lock = threading.Lock()
        original = harness.call_node_handler

        def recording(node_type, cfg, ctx, session):
            with lock:
                seen.setdefault(node_type, []).append(id(session))
            return original(node_type, cfg, ctx, session)

        monkeypatch.setattr(harness, "call_node_handler", recording)
        _run(mixed_agent)

        for node_type in ("memory", "retrieval", "llm"):
            assert node_type in seen, f"{node_type} never ran: {list(seen)}"
        concurrent_ids = [seen[t][0] for t in ("memory", "retrieval", "llm")]
        assert len(set(concurrent_ids)) == 3, (
            "the three concurrent DB-touching nodes did not each get their own "
            f"Session: {dict(zip(('memory', 'retrieval', 'llm'), concurrent_ids))}"
        )
        # And none of them borrowed the run's own session, which the
        # solo trigger/respond nodes use.
        assert not set(concurrent_ids) & set(seen.get("trigger", []))

    def test_repeated_mixed_runs_never_raise_or_lose_a_write(self, mixed_agent):
        """The stress criterion: repeated, real-Postgres, real thread
        concurrency. A shared Session shows up here as an intermittent
        failed run, an IllegalStateChangeError, or a write that silently
        did not land — so every iteration is asserted, not just the last."""
        from app.models import ChatMessage

        runs = int(os.getenv("PARALLEL_SESSION_TEST_RUNS", "15"))
        for attempt in range(runs):
            result = _run(mixed_agent, text=f"mixed-{attempt}")
            assert result.get("status") in ("ok", "success"), (
                f"run {attempt} failed: {result.get('spans') or result}"
            )
            spans = {s.get("node_id"): s for s in (result.get("spans") or []) if s.get("node_id")}
            for node_id in ("memA", "ret", "llm"):
                assert spans.get(node_id, {}).get("status") == "ok", (
                    f"run {attempt}: {node_id} -> {spans.get(node_id)}"
                )
            with Session(engine) as s:
                rows = {m.key: m.value for m in s.exec(
                    select(AgentMemory).where(AgentMemory.scope_key == mixed_agent)).all()}
                assert rows.get("mixed_mem") == "mem-value", f"run {attempt}: {rows}"
                # The LLM node's history rows must accumulate — a rolled-back
                # transaction from a neighbouring node would silently eat them.
                history = s.exec(select(ChatMessage).where(
                    ChatMessage.agent_id == mixed_agent)).all()
            assert len(history) >= (attempt + 1), (
                f"run {attempt}: only {len(history)} chat rows persisted across "
                f"{attempt + 1} runs — a write was lost"
            )

    def test_every_per_node_session_is_closed(self, mixed_agent, monkeypatch):
        """Three concurrent nodes per run must not each keep a pooled
        connection past their own node."""
        closed = []
        real_close = Session.close

        def tracking_close(self):
            closed.append(id(self))
            return real_close(self)

        monkeypatch.setattr(Session, "close", tracking_close)

        opened = []
        original = harness.call_node_handler

        def recording(node_type, cfg, ctx, session):
            if node_type in ("memory", "retrieval", "llm"):
                opened.append(id(session))
            return original(node_type, cfg, ctx, session)

        monkeypatch.setattr(harness, "call_node_handler", recording)
        _run(mixed_agent)

        assert len(opened) == 3
        for session_id in opened:
            assert session_id in closed, "a per-node session was left open"
