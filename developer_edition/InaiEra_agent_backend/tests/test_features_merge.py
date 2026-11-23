"""
PATCH /api/agents/{id}/features merges; it does not replace.

The reported bug: features enabled in Manage Features came back
disabled after closing the panel and navigating away. Every individual
save was real — the NEXT one undid it.

Two ways the replace semantics bit:

  * a partial call deleted every key it left out. Sending
    `{"features": {"require_authenticated_user": true}}` wiped
    moderation, citations, chat history, the conversation opener and
    follow-ups from an agent that had them. Confirmed against a live
    agent, whose five enabled features were reduced to one key.
  * the Features panel builds its payload from the agent object it was
    given. Try It seeds that from a snapshot taken when its page
    loaded, so reopening the panel sent a STALE base: enable one
    feature, close, reopen, enable a second, and the first was gone.

A feature is switched off with `enabled: false`, never by omitting its
key, so merging costs no ability to disable anything.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlmodel import Session, text

from app.db import engine
from app.main import app
from app.models import Agent

client = TestClient(app)

PREFIX = f"feat{uuid.uuid4().hex[:8]}"


def _mk_agent(features=None):
    with Session(engine) as s:
        a = Agent(
            id=PREFIX, name=f"{PREFIX} agent", type="App", subtype="Workflow",
            plane="control", owner="tests", status="draft", version="v1",
            health_status="—", flow={"nodes": [], "edges": []}, metrics={},
            features=features or {},
        )
        s.add(a)
        s.commit()
    return PREFIX


def _cleanup():
    with Session(engine) as s:
        s.exec(text("DELETE FROM agent WHERE id LIKE :p"), params={"p": f"{PREFIX}%"})
        s.commit()


def _patch(agent_id, features):
    r = client.patch(f"/api/agents/{agent_id}/features", json={"features": features})
    assert r.status_code == 200, r.text
    return r.json()["features"]


def test_a_partial_payload_keeps_the_keys_it_omits():
    """The core regression. One key in must not mean five keys out."""
    aid = _mk_agent({
        "chat_history": {"enabled": True},
        "citations": {"enabled": True},
        "moderation": {"enabled": True},
        "conversation_opener": {"enabled": True, "text": "Hi there"},
        "follow_up": {"enabled": True},
    })
    try:
        out = _patch(aid, {"require_authenticated_user": True})
        assert out["require_authenticated_user"] is True
        for key in ("chat_history", "citations", "moderation", "follow_up"):
            assert out[key]["enabled"] is True, f"{key} was wiped"
        # Nested detail survives too, not just the enabled flag.
        assert out["conversation_opener"]["text"] == "Hi there"
    finally:
        _cleanup()


def test_two_sequential_partial_saves_accumulate():
    """Exactly the reported sequence: enable one, close, reopen with a
    stale base, enable another."""
    aid = _mk_agent({})
    try:
        _patch(aid, {"chat_history": {"enabled": True}})
        # A second panel session that never saw the first save.
        out = _patch(aid, {"citations": {"enabled": True}})
        assert out["chat_history"]["enabled"] is True, "the first save was undone"
        assert out["citations"]["enabled"] is True
    finally:
        _cleanup()


def test_a_feature_can_still_be_switched_off():
    """Merging must not make disabling impossible — that is done with
    enabled:false, not by omission."""
    aid = _mk_agent({"moderation": {"enabled": True}, "citations": {"enabled": True}})
    try:
        out = _patch(aid, {"moderation": {"enabled": False}})
        assert out["moderation"]["enabled"] is False
        assert out["citations"]["enabled"] is True
    finally:
        _cleanup()


def test_a_later_value_for_the_same_key_wins():
    aid = _mk_agent({"conversation_opener": {"enabled": True, "text": "old"}})
    try:
        out = _patch(aid, {"conversation_opener": {"enabled": True, "text": "new"}})
        assert out["conversation_opener"]["text"] == "new"
    finally:
        _cleanup()


def test_an_empty_payload_changes_nothing():
    aid = _mk_agent({"citations": {"enabled": True}})
    try:
        out = _patch(aid, {})
        assert out["citations"]["enabled"] is True
    finally:
        _cleanup()


def test_the_write_survives_a_reread():
    """A page reload re-reads from the server; that is where the user
    saw the loss."""
    aid = _mk_agent({})
    try:
        _patch(aid, {"follow_up": {"enabled": True}})
        again = client.get(f"/api/agents/{aid}").json()["features"]
        assert again["follow_up"]["enabled"] is True
    finally:
        _cleanup()


def test_the_draft_revision_still_moves():
    """§0.96: a features edit is versioned behaviour, so it must keep
    signalling that the draft changed."""
    aid = _mk_agent({})
    try:
        before = client.get(f"/api/agents/{aid}").json()["draft_revision"]
        _patch(aid, {"citations": {"enabled": True}})
        after = client.get(f"/api/agents/{aid}").json()["draft_revision"]
        assert after > before
    finally:
        _cleanup()
