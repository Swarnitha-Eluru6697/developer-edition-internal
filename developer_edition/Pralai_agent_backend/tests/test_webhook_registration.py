"""
Real tests for first-class webhook registration, activated at publish
time (section 0.97, GAP_REPORT).

The real bug, exactly as reported: webhook EXECUTION already correctly
ran a published agent's own frozen AgentVersion.flow, but webhook
DISCOVERY (_find_webhook_node) scanned the live, mutable Agent.flow
directly for a plain string match against node.config["webhook_token"]
— a Builder editing a trigger node's own token on the live draft, with
NO new publish, silently changed which token production actually
responded to, even though the flow that token invoked stayed genuinely
frozen. It was also an unindexed O(agents x nodes) full scan on every
single incoming webhook request.

The fix: a new, first-class WebhookEndpoint table, registered/updated/
revoked as part of the same real, atomic publish transaction that
creates a new AgentVersion — never a JSON flow scan again.

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
from app.routers.webhooks import hash_webhook_token

client = TestClient(app)


def create_agent(marker: str = "V1_OUTPUT") -> dict:
    r = client.post("/api/agents", json={
        "name": f"Webhook Registration Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
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


def set_token(agent_id: str, token: str):
    r = client.patch(f"/api/agents/{agent_id}/nodes/n1/config", json={"config": {"webhook_token": token}})
    assert r.status_code == 200, r.text


def publish(agent_id: str) -> str:
    r = client.post(f"/api/agents/{agent_id}/publish")
    assert r.status_code == 200, r.text
    return r.json()["version"]


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        s.exec(text("DELETE FROM webhookendpoint WHERE agent_id = :a"), params={"a": agent_id})
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealWebhookRegistrationStaysFrozenAtPublishTime:
    """The exact real scenario from the report: publish v5 with token
    A; edit the live draft's token to B with no new publish; token A
    must still resolve, and token B must NOT resolve yet."""

    def test_a_live_token_edit_does_not_change_production_exposure(self):
        agent = create_agent("V1_OUTPUT")
        try:
            set_token(agent["id"], "TOKEN_A")
            publish(agent["id"])

            r1 = client.post(f"/api/webhooks/TOKEN_A", json={"input": "hi"})
            assert r1.status_code == 200, r1.text
            assert r1.json()["output"] == "V1_OUTPUT"

            # Real, live Builder edit — WITHOUT a new publish.
            set_token(agent["id"], "TOKEN_B")

            # The real, direct proof: TOKEN_A must still resolve,
            # genuinely immune to the live, unpublished edit.
            r2 = client.post(f"/api/webhooks/TOKEN_A", json={"input": "hi again"})
            assert r2.status_code == 200, r2.text
            assert r2.json()["output"] == "V1_OUTPUT"

            # TOKEN_B must NOT resolve yet — it was never published.
            r3 = client.post(f"/api/webhooks/TOKEN_B", json={"input": "hi"})
            assert r3.status_code == 404
        finally:
            cleanup_agent(agent["id"])

    def test_republishing_activates_the_new_token_and_revokes_the_old_one(self):
        agent = create_agent("V1_OUTPUT")
        try:
            set_token(agent["id"], "TOKEN_A2")
            publish(agent["id"])

            set_token(agent["id"], "TOKEN_B2")
            r = client.patch(f"/api/agents/{agent['id']}/nodes/n2/config", json={"config": {"template": "V2_OUTPUT"}})
            assert r.status_code == 200, r.text
            publish(agent["id"])

            r_new = client.post(f"/api/webhooks/TOKEN_B2", json={"input": "hi"})
            assert r_new.status_code == 200, r_new.text
            assert r_new.json()["output"] == "V2_OUTPUT"

            r_old = client.post(f"/api/webhooks/TOKEN_A2", json={"input": "hi"})
            assert r_old.status_code == 404
        finally:
            cleanup_agent(agent["id"])

    def test_removing_the_webhook_token_and_republishing_genuinely_revokes_it(self):
        agent = create_agent("V1_OUTPUT")
        try:
            set_token(agent["id"], "TOKEN_TO_REMOVE")
            publish(agent["id"])
            assert client.post(f"/api/webhooks/TOKEN_TO_REMOVE", json={"input": "hi"}).status_code == 200

            r = client.patch(f"/api/agents/{agent['id']}/nodes/n1/config", json={"config": {"webhook_token": None}})
            assert r.status_code == 200, r.text
            publish(agent["id"])

            r2 = client.post(f"/api/webhooks/TOKEN_TO_REMOVE", json={"input": "hi"})
            assert r2.status_code == 404
        finally:
            cleanup_agent(agent["id"])

    def test_republishing_with_an_unchanged_token_keeps_the_same_endpoint_working(self):
        """Real, direct proof the sync logic doesn't spuriously revoke
        a token that never actually changed between two publishes."""
        agent = create_agent("V1_OUTPUT")
        try:
            set_token(agent["id"], "TOKEN_STABLE")
            publish(agent["id"])

            r = client.patch(f"/api/agents/{agent['id']}/nodes/n2/config", json={"config": {"template": "V2_OUTPUT"}})
            assert r.status_code == 200, r.text
            publish(agent["id"])

            r2 = client.post(f"/api/webhooks/TOKEN_STABLE", json={"input": "hi"})
            assert r2.status_code == 200, r2.text
            assert r2.json()["output"] == "V2_OUTPUT"

            with Session(engine) as s:
                rows = s.exec(text(
                    "SELECT enabled FROM webhookendpoint WHERE agent_id = :a AND token_hash = :h"
                ), params={"a": agent["id"], "h": hash_webhook_token("TOKEN_STABLE")}).all()
            assert len(rows) == 1
            assert rows[0][0] is True
        finally:
            cleanup_agent(agent["id"])


class TestRealTokenHashing:
    def test_the_stored_token_is_a_real_hash_never_the_raw_token(self):
        agent = create_agent("V1_OUTPUT")
        try:
            set_token(agent["id"], "SUPER_SECRET_TOKEN_XYZ")
            publish(agent["id"])
            with Session(engine) as s:
                row = s.exec(text(
                    "SELECT token_hash FROM webhookendpoint WHERE agent_id = :a"
                ), params={"a": agent["id"]}).first()
            assert row is not None
            assert row[0] != "SUPER_SECRET_TOKEN_XYZ"
            assert row[0] == hash_webhook_token("SUPER_SECRET_TOKEN_XYZ")
        finally:
            cleanup_agent(agent["id"])

    def test_hashing_is_deterministic(self):
        assert hash_webhook_token("some-token") == hash_webhook_token("some-token")
        assert hash_webhook_token("some-token") != hash_webhook_token("some-other-token")


class TestRealUnpublishedDraftHasNoWorkingWebhook:
    def test_a_webhook_token_set_on_a_never_published_draft_does_not_resolve(self):
        agent = create_agent("V1_OUTPUT")
        try:
            set_token(agent["id"], "NEVER_PUBLISHED_TOKEN")
            r = client.post(f"/api/webhooks/NEVER_PUBLISHED_TOKEN", json={"input": "hi"})
            assert r.status_code == 404
        finally:
            cleanup_agent(agent["id"])
