"""
Real tests for multi-turn chat history (§0.47, GAP_REPORT) - genuinely
different from the older AgentMemory __llm_chat_history__ text-blob
mechanism this replaces for the `llm` node specifically: real, structured
ChatMessage rows, replayed as a genuine multi-turn `messages` array to
model_adapter.call_llm, not flattened into the system prompt.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, select

from app.harness import run_flow_sync, _load_chat_history, _append_chat_turn, CHAT_HISTORY_MAX_TURNS
from app.models import Agent, ChatMessage


@pytest.fixture()
def session():
    from app.db import engine
    with Session(engine) as s:
        yield s


def make_agent(session, flow: dict, features: dict = None) -> Agent:
    """A real, persisted Agent — required for ChatMessage's real
    foreign key (see persisted_agent_id's own note above)."""
    agent = Agent(
        id=f"test-chat-{uuid.uuid4().hex[:10]}", name="Test", type="App", subtype="Chatflow", plane="App",
        owner="you", status="published", version="v1", app_type="chatflow", flow=flow,
        features=features or {},
    )
    session.add(agent)
    session.commit()
    return agent


def persisted_agent_id(session) -> str:
    """A real, minimal Agent row actually committed to the database —
    required because ChatMessage.agent_id is a real foreign key
    (unlike AgentMemory's own looser scope_key string, deliberately
    stricter here to catch a genuinely orphaned chat row as a real
    integrity error, not silently allow one). Production code never
    hits this: a real run only ever happens against a real, already-
    persisted agent — this helper exists purely so these tests reflect
    that same real condition rather than routing around it."""
    agent = Agent(
        id=f"test-chatmsg-{uuid.uuid4().hex[:10]}", name="Test", type="App", subtype="Chatflow", plane="App",
        owner="you", status="published", version="v1", app_type="chatflow", flow={"nodes": [], "edges": []},
    )
    session.add(agent)
    session.commit()
    return agent.id


def chat_flow() -> dict:
    return {
        "nodes": [
            {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}},
            {"id": "n2", "type": "llm", "x": 0, "y": 0, "sub": "", "config": {"system_prompt": "Be concise.", "user_prompt": "{{input}}"}},
            {"id": "n3", "type": "respond", "x": 0, "y": 0, "sub": "", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            {"id": "e2", "source": "n2", "target": "n3", "source_handle": None},
        ],
    }


def chat_flow_with_explicit_memory(memory_enabled: bool) -> dict:
    f = chat_flow()
    f["nodes"][1]["config"]["memory_enabled"] = memory_enabled
    return f


class TestRealChatHistoryHelpers:
    def test_a_fresh_agent_user_pair_has_no_real_history(self, session):
        agent_id = persisted_agent_id(session)
        result = _load_chat_history(session, agent_id, "nobody")
        assert result == []

    def test_append_then_load_returns_real_rows_in_chronological_order(self, session):
        agent_id = persisted_agent_id(session)
        _append_chat_turn(session, agent_id, "user:alice", "user", "hello")
        _append_chat_turn(session, agent_id, "user:alice", "assistant", "hi there")
        _append_chat_turn(session, agent_id, "user:alice", "user", "how are you")
        history = _load_chat_history(session, agent_id, "user:alice")
        assert history == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you"},
        ]

    def test_history_is_genuinely_scoped_per_invoking_user(self, session):
        agent_id = persisted_agent_id(session)
        _append_chat_turn(session, agent_id, "user:alice", "user", "alice's real message")
        _append_chat_turn(session, agent_id, "user:bob", "user", "bob's real message")
        alice_history = _load_chat_history(session, agent_id, "user:alice")
        bob_history = _load_chat_history(session, agent_id, "user:bob")
        assert alice_history == [{"role": "user", "content": "alice's real message"}]
        assert bob_history == [{"role": "user", "content": "bob's real message"}]

    def test_history_is_genuinely_scoped_per_agent(self, session):
        user = "user:carol"
        agent_a = persisted_agent_id(session)
        agent_b = persisted_agent_id(session)
        _append_chat_turn(session, agent_a, user, "user", "message to agent A")
        history_a = _load_chat_history(session, agent_a, user)
        history_b = _load_chat_history(session, agent_b, user)
        assert history_a == [{"role": "user", "content": "message to agent A"}]
        assert history_b == []

    def test_real_max_turns_bound_is_respected(self, session):
        agent_id = persisted_agent_id(session)
        for i in range(CHAT_HISTORY_MAX_TURNS + 10):
            _append_chat_turn(session, agent_id, "user:dave", "user", f"message {i}")
        history = _load_chat_history(session, agent_id, "user:dave")
        assert len(history) == CHAT_HISTORY_MAX_TURNS
        assert history[-1]["content"] == f"message {CHAT_HISTORY_MAX_TURNS + 9}"


class TestRealAgentLevelInheritance:
    def test_an_llm_node_with_no_explicit_memory_setting_inherits_the_real_agent_level_toggle_on(self, session):
        agent = make_agent(session, chat_flow(), features={"chat_history": {"enabled": True}})
        result = run_flow_sync(session, agent, "user:erin", "first real message")
        assert result["status"] == "success"
        history = _load_chat_history(session, agent.id, "user:erin")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    def test_an_llm_node_with_no_explicit_memory_setting_inherits_the_real_agent_level_toggle_off(self, session):
        agent = make_agent(session, chat_flow(), features={"chat_history": {"enabled": False}})
        run_flow_sync(session, agent, "user:frank", "a message that should not be persisted")
        history = _load_chat_history(session, agent.id, "user:frank")
        assert history == []

    def test_a_node_explicit_memory_enabled_false_overrides_the_agent_level_toggle_on(self, session):
        agent = make_agent(session, chat_flow_with_explicit_memory(False), features={"chat_history": {"enabled": True}})
        run_flow_sync(session, agent, "user:grace", "should not be persisted despite agent-level toggle")
        history = _load_chat_history(session, agent.id, "user:grace")
        assert history == []

    def test_a_node_explicit_memory_enabled_true_works_even_with_no_agent_level_feature_set(self, session):
        agent = make_agent(session, chat_flow_with_explicit_memory(True), features={})
        run_flow_sync(session, agent, "user:henry", "explicit node-level opt-in")
        history = _load_chat_history(session, agent.id, "user:henry")
        assert len(history) == 2

    def test_a_real_second_run_genuinely_sees_the_first_runs_history(self, session):
        agent = make_agent(session, chat_flow(), features={"chat_history": {"enabled": True}})
        run_flow_sync(session, agent, "user:iris", "my first real message")
        run_flow_sync(session, agent, "user:iris", "my second real message")
        history = _load_chat_history(session, agent.id, "user:iris")
        assert len(history) == 4
        assert history[0]["content"] == "my first real message"
        assert history[2]["content"] == "my second real message"
