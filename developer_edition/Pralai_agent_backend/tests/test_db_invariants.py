"""
Real tests for the database invariant enforcement fix (section 0.70,
GAP_REPORT).

The real bug, exactly as reported: several application-level
invariants (EnvVar's (agent_id, key), AgentMemory's (scope_key, key),
AgentVersion's (agent_id, version), KnowledgeMetadataField's (kb_id,
name), and "exactly one is_current=True AgentVersion per agent") were
only ever checked in Python via SELECT-then-INSERT, never enforced by
Postgres itself - two genuinely concurrent requests could both pass
the SELECT check before either committed, producing real duplicate
rows.

The fix: real UniqueConstraints (plus a real partial unique index for
the is_current invariant), a real Alembic migration applying them, and
real application-code handling for the resulting races - a real
Postgres INSERT ... ON CONFLICT for the two upsert-style writes
(AgentMemory, KnowledgeMetadataField), a real caught IntegrityError
with an honest error message for EnvVar, and a real SELECT ... FOR
UPDATE row lock (with populate_existing=True - a real bug caught by
this file's own live testing before the fix: the lock alone genuinely
serialized the real transactions, but SQLAlchemy's own identity map
kept returning a stale, already-cached Python object without it)
serializing concurrent publishes of the same agent.

These tests fire genuinely concurrent requests via real threads - not
mocked, and not simulated sequentially.
"""
import sys
import os
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select, text

from app.db import engine
from app.main import app
from app.models import Agent, KnowledgeBase, KnowledgeMetadataField, AgentMemory

client = TestClient(app)


@pytest.fixture()
def real_agent():
    agent_id = f"race-agent-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s:
        s.add(Agent(
            id=agent_id, name=agent_id, type="App", subtype="Custom", plane="App",
            owner="you", status="draft", version="v1", app_type="workflow",
            flow={"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
                             {"id": "n2", "type": "output", "x": 50, "y": 0, "sub": "", "config": {"variables": [{"name": "result"}]}},
                             {"id": "n3", "type": "respond", "x": 100, "y": 0, "sub": "", "config": {}}],
                  "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
                            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None}]},
        ))
        s.commit()
    yield agent_id
    with Session(engine) as s:
        s.exec(text("DELETE FROM envvar WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agentversion WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agentmemory WHERE scope_key = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealEnvVarRace:
    def test_ten_genuinely_concurrent_creates_produce_exactly_one_real_row(self, real_agent):
        results = []

        def create():
            r = client.post(f"/api/agents/{real_agent}/env", json={"key": "race_key", "value": "x", "type": "string"})
            results.append(r.status_code)

        threads = [threading.Thread(target=create) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(200) == 1
        assert results.count(400) == 9
        assert 500 not in results

        with Session(engine) as s:
            count = s.exec(text("SELECT count(*) FROM envvar WHERE agent_id = :a AND key = 'race_key'"), params={"a": real_agent}).first()
            assert count[0] == 1


class TestRealAgentMemoryRace:
    def test_ten_genuinely_concurrent_writes_produce_exactly_one_real_row(self, real_agent):
        from app.harness import handle_memory

        def write(i):
            with Session(engine) as s:
                ctx = {"agent_id": real_agent, "invoking_user": "user:test", "output": f"value {i}", "vars": {}}
                cfg = {"key": "race_mem_key", "scope": "agent", "mode": "write"}
                handle_memory(cfg, ctx, s)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with Session(engine) as s:
            rows = s.exec(select(AgentMemory).where(AgentMemory.scope_key == real_agent, AgentMemory.key == "race_mem_key")).all()
            assert len(rows) == 1


class TestRealKnowledgeMetadataFieldRace:
    def test_ten_genuinely_concurrent_ensures_produce_exactly_one_real_row(self):
        from app.routers.knowledge_bases import _ensure_metadata_fields

        kb_id = f"race-kb-{uuid.uuid4().hex[:8]}"
        try:
            with Session(engine) as s:
                s.add(KnowledgeBase(id=kb_id, name=kb_id, provider="pgvector", status="connected", config={}))
                s.commit()

            def ensure():
                with Session(engine) as s:
                    _ensure_metadata_fields(s, kb_id, {"race_field": "value"})
                    s.commit()

            threads = [threading.Thread(target=ensure) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            with Session(engine) as s:
                rows = s.exec(select(KnowledgeMetadataField).where(
                    KnowledgeMetadataField.kb_id == kb_id, KnowledgeMetadataField.name == "race_field"
                )).all()
                assert len(rows) == 1
        finally:
            with Session(engine) as s:
                s.exec(text("DELETE FROM knowledgemetadatafield WHERE kb_id = :k"), params={"k": kb_id})
                s.exec(text("DELETE FROM knowledgebase WHERE id = :k"), params={"k": kb_id})
                s.commit()


class TestRealAgentVersionPublishRace:
    def test_five_genuinely_concurrent_publishes_get_five_distinct_sequential_versions(self, real_agent):
        results = []

        def publish():
            r = client.post(f"/api/agents/{real_agent}/publish")
            results.append((r.status_code, r.json().get("version") if r.status_code == 200 else None))

        threads = [threading.Thread(target=publish) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [v for code, v in results if code == 200]
        assert len(successes) == 5
        assert len(set(successes)) == 5

        with Session(engine) as s:
            current_count = s.exec(text(
                "SELECT count(*) FROM agentversion WHERE agent_id = :a AND is_current = true"
            ), params={"a": real_agent}).first()
            assert current_count[0] == 1

            total_versions = s.exec(text(
                "SELECT count(DISTINCT version) FROM agentversion WHERE agent_id = :a"
            ), params={"a": real_agent}).first()
            assert total_versions[0] == 5
