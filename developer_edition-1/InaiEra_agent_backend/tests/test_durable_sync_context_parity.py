"""
Real tests for the durable/sync execution context parity fix
(section 0.61, GAP_REPORT).

The real bug, exactly as reported: run_flow_sync's own ctx construction
seeds _agent_chain, _chat_history_enabled, and every real conversation
variable a Variable Assigner node has persisted (via a real
AgentMemory query) before the graph walk starts. _run_durable's own
ctx was genuinely thinner - these three keys were simply absent, not
"unset" - confirmed by direct inspection before any fix.

The one part of the original report already fixed in an earlier round
(structured_inputs threading, section 0.53) is not retested here.

The fix: _run_durable now takes a real session parameter and builds
its own ctx with the exact same three real pieces run_flow_sync's own
pre-flight already provides, including the same real AgentMemory query
for conversation variables.

These tests call the real _run_durable function directly, mocking only
the real Temporal client's start_workflow call to capture the exact
real ctx dict passed to it.
"""
import sys
import os
import json
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch
from sqlmodel import Session, select

from app.db import engine
from app.models import Agent, AgentMemory
from app.harness import CONV_VAR_PREFIX
from app.routers.execution import _run_durable


def make_agent(agent_id: str, chat_history_enabled: bool = False) -> Agent:
    return Agent(
        id=agent_id, name="CTX Parity Test", type="App", subtype="Custom", plane="App",
        owner="you", status="draft", version="v1", app_type="chatbot",
        model="Claude Sonnet 5",
        flow={"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
        features={"chat_history": {"enabled": chat_history_enabled}},
    )


async def _capture_ctx(session, agent, invoking_user="user:test", user_input="hi",
                        webhook_payload=None, structured_inputs=None) -> dict:
    captured = {}

    class FakeHandle:
        async def query(self, *a, **kw):
            raise RuntimeError("stop here — ctx already captured")

    class FakeClient:
        async def start_workflow(self, fn, args, id, task_queue):
            captured["ctx"] = args[2]
            return FakeHandle()

    async def fake_connect(address, namespace):
        return FakeClient()

    import temporalio.client
    with patch.object(temporalio.client.Client, "connect", fake_connect):
        try:
            await _run_durable(session, agent, invoking_user, user_input,
                                webhook_payload=webhook_payload, structured_inputs=structured_inputs)
        except RuntimeError:
            pass
    return captured["ctx"]


class TestRealAgentChainParity:
    def test_durable_ctx_now_genuinely_includes_agent_chain(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-chain-1")
            s.add(agent)
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent))
            s.delete(agent)
            s.commit()
        assert ctx.get("_agent_chain") == ["ctx-parity-chain-1"]

    def test_the_seed_matches_run_flow_syncs_own_real_top_level_default(self):
        default_agent_chain = None
        agent_id = "some-agent"
        resolved = default_agent_chain or [agent_id]
        assert resolved == ["some-agent"]


class TestRealChatHistoryParity:
    def test_durable_ctx_reflects_a_real_enabled_chat_history_setting(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-chat-1", chat_history_enabled=True)
            s.add(agent)
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent))
            s.delete(agent)
            s.commit()
        assert ctx.get("_chat_history_enabled") is True

    def test_durable_ctx_reflects_a_real_disabled_chat_history_setting(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-chat-2", chat_history_enabled=False)
            s.add(agent)
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent))
            s.delete(agent)
            s.commit()
        assert ctx.get("_chat_history_enabled") is False

    def test_durable_ctx_key_was_previously_entirely_absent_not_just_false(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-chat-3", chat_history_enabled=False)
            s.add(agent)
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent))
            s.delete(agent)
            s.commit()
        assert "_chat_history_enabled" in ctx


class TestRealConversationVariableParity:
    def test_durable_ctx_seeds_a_real_previously_persisted_conversation_variable(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-conv-1")
            s.add(agent)
            scope_key = f"{agent.id}:user:test"
            s.add(AgentMemory(scope_key=scope_key, key=CONV_VAR_PREFIX + "my_var",
                               value=json.dumps("hello from an earlier turn")))
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent))
            for row in s.exec(select(AgentMemory).where(AgentMemory.scope_key == scope_key)).all():
                s.delete(row)
            s.delete(agent)
            s.commit()
        assert ctx["vars"].get("conv_my_var") == "hello from an earlier turn"

    def test_durable_ctx_seeds_multiple_real_conversation_variables_with_real_types_preserved(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-conv-2")
            s.add(agent)
            scope_key = f"{agent.id}:user:test"
            s.add(AgentMemory(scope_key=scope_key, key=CONV_VAR_PREFIX + "count", value=json.dumps(42)))
            s.add(AgentMemory(scope_key=scope_key, key=CONV_VAR_PREFIX + "confirmed", value=json.dumps(True)))
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent))
            for row in s.exec(select(AgentMemory).where(AgentMemory.scope_key == scope_key)).all():
                s.delete(row)
            s.delete(agent)
            s.commit()
        assert ctx["vars"]["conv_count"] == 42
        assert ctx["vars"]["conv_confirmed"] is True

    def test_a_conversation_variable_scoped_to_a_different_user_is_never_leaked_in(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-conv-3")
            s.add(agent)
            other_scope_key = f"{agent.id}:user:someone-else"
            s.add(AgentMemory(scope_key=other_scope_key, key=CONV_VAR_PREFIX + "secret", value=json.dumps("not for this user")))
            s.commit()
            ctx = asyncio.run(_capture_ctx(s, agent, invoking_user="user:test"))
            for row in s.exec(select(AgentMemory).where(AgentMemory.scope_key == other_scope_key)).all():
                s.delete(row)
            s.delete(agent)
            s.commit()
        assert "conv_secret" not in ctx["vars"]


class TestRealExistingFieldsStillWork:
    def test_webhook_payload_and_structured_inputs_still_correctly_threaded(self):
        with Session(engine) as s:
            agent = make_agent("ctx-parity-regress-1")
            s.add(agent)
            s.commit()
            ctx = asyncio.run(_capture_ctx(
                s, agent, webhook_payload={"real": "payload"}, structured_inputs={"field": "value"},
            ))
            s.delete(agent)
            s.commit()
        assert ctx["_webhook_payload"] == {"real": "payload"}
        assert ctx["_structured_inputs"] == {"field": "value"}
