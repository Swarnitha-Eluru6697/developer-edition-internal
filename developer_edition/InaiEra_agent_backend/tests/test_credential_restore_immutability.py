"""
Real tests for operational credentials surviving a version restore
(section 0.101, GAP_REPORT).

The real bug, exactly as reported: MCP tokens lived inside
Agent.features["mcp"]["token"] — the same real, live dict
AgentVersion.features snapshots wholesale at publish time. Restoring
an OLD version copied that OLD snapshot's own token back into the LIVE
agent, genuinely resurrecting a credential that had already been
rotated/revoked since.

The fix: this credential now lives on real, top-level Agent columns
(mcp_enabled/mcp_token_encrypted/...), never inside features — restore
only ever reads/writes flow/app_type/features, never these columns, so
a version restore genuinely cannot touch this credential at all
anymore.

A related, second bug was found and fixed while proving this live:
restoring an old version's own webhook token and then republishing
could collide with a DIFFERENT, already-revoked WebhookEndpoint row's
own token_hash (a deterministic hash — the same plaintext always
hashes the same), genuinely failing the publish with a real
IntegrityError misreported as an unrelated "concurrent publish"
conflict. Webhook tokens are deliberately handled differently from MCP
tokens here — they're part of a flow's own explicit, versioned
configuration, so a deliberate restore+republish correctly
re-activates the same registration rather than crashing.

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

client = TestClient(app)


def create_agent(marker: str = "V1", webhook_token: str = None) -> dict:
    trigger_config = {"webhook_token": webhook_token} if webhook_token else {}
    r = client.post("/api/agents", json={
        "name": f"Credential Restore Test {uuid.uuid4().hex[:8]}", "subtype": "Custom", "app_type": "chatflow",
        "template_flow": {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": trigger_config},
                {"id": "n2", "type": "template", "x": 100, "y": 0, "sub": "", "config": {"template": marker}},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None}],
        },
    })
    assert r.status_code == 200, r.text
    return r.json()


def publish(agent_id: str) -> str:
    r = client.post(f"/api/agents/{agent_id}/publish")
    assert r.status_code == 200, r.text
    return r.json()["version"]


def get_version_ids(agent_id: str) -> list:
    r = client.get(f"/api/agents/{agent_id}/versions")
    assert r.status_code == 200, r.text
    return [v["id"] for v in sorted(r.json(), key=lambda v: v["id"])]


def cleanup_agent(agent_id: str):
    with Session(engine) as s:
        s.exec(text("DELETE FROM webhookendpoint WHERE agent_id = :a"), params={"a": agent_id})
        for t in ("run", "trace", "agentlog", "agentversion", "hitlrequest"):
            s.exec(text(f"DELETE FROM {t} WHERE agent_id = :a"), params={"a": agent_id})
        s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent_id})
        s.commit()


class TestRealMcpTokenSurvivesNoLongerCanBeRestored:
    """The exact real scenario from the report: v5 contains MCP
    TOKEN_A; rotate the credential (live agent now TOKEN_B, TOKEN_A
    revoked); restore v5; TOKEN_A must NOT become valid again."""

    def test_a_rotated_mcp_token_stays_revoked_after_restoring_the_version_that_had_it(self):
        agent = create_agent("V1")
        try:
            r1 = client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
            token_a = r1.json()["url"].rsplit("/", 1)[-1]
            v1 = publish(agent["id"])

            # Real, direct proof the credential was never snapshotted
            # at all — not just masked, genuinely absent.
            versions = client.get(f"/api/agents/{agent['id']}/versions").json()
            assert all("mcp" not in (v.get("features") or {}) for v in versions)

            check_a = client.post(f"/mcp/{agent['id']}/{token_a}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert "result" in check_a.json()

            # Real credential rotation — TOKEN_A must genuinely revoke.
            r2 = client.post(f"/api/agents/{agent['id']}/mcp/regenerate")
            token_b = r2.json()["url"].rsplit("/", 1)[-1]

            revoked_check = client.post(f"/mcp/{agent['id']}/{token_a}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert revoked_check.json()["error"]["code"] == -32002

            # The real, reported attack: restore v1 (which had TOKEN_A).
            version_ids = get_version_ids(agent["id"])
            restore_r = client.post(f"/api/agents/{agent['id']}/versions/{version_ids[0]}/restore")
            assert restore_r.status_code == 200, restore_r.text

            # The core, direct proof: TOKEN_A must STILL be revoked.
            still_revoked = client.post(f"/mcp/{agent['id']}/{token_a}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert still_revoked.json()["error"]["code"] == -32002

            # TOKEN_B, the current, real credential, must be unaffected.
            still_works = client.post(f"/mcp/{agent['id']}/{token_b}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert "result" in still_works.json()
        finally:
            cleanup_agent(agent["id"])

    def test_restoring_a_version_does_not_change_the_live_mcp_enabled_flag(self):
        """A real, direct proof restore genuinely never touches these
        columns at all — not even to leave them in a plausible-looking
        but coincidentally-correct state."""
        agent = create_agent("V1")
        try:
            client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
            publish(agent["id"])
            version_ids = get_version_ids(agent["id"])

            client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": False})
            restore_r = client.post(f"/api/agents/{agent['id']}/versions/{version_ids[0]}/restore")
            assert restore_r.status_code == 200, restore_r.text
            assert restore_r.json()["mcp_enabled"] is False
        finally:
            cleanup_agent(agent["id"])


class TestRealWebhookTokenRestoreRepublish:
    """The report's own related, distinct finding: a token restored
    from an old version and republished must not crash the publish
    call, and should correctly re-activate rather than silently break."""

    def test_restoring_and_republishing_an_old_webhook_token_does_not_crash(self):
        agent = create_agent("V1_OUTPUT", webhook_token="RESTORE_REPUBLISH_TOKEN_A")
        try:
            publish(agent["id"])
            client.patch(f"/api/agents/{agent['id']}/nodes/n1/config", json={"config": {"webhook_token": "RESTORE_REPUBLISH_TOKEN_B"}})
            publish(agent["id"])

            r_a_revoked = client.post("/api/webhooks/RESTORE_REPUBLISH_TOKEN_A", json={"input": "hi"})
            assert r_a_revoked.status_code == 404

            version_ids = get_version_ids(agent["id"])
            restore_r = client.post(f"/api/agents/{agent['id']}/versions/{version_ids[0]}/restore")
            assert restore_r.status_code == 200, restore_r.text

            # The real, reported crash: this used to fail with a
            # misleading "concurrent publish" 409 from an unhandled
            # IntegrityError on the token_hash unique constraint.
            republish_r = client.post(f"/api/agents/{agent['id']}/publish")
            assert republish_r.status_code == 200, republish_r.text

            r_a_works_again = client.post("/api/webhooks/RESTORE_REPUBLISH_TOKEN_A", json={"input": "hi"})
            assert r_a_works_again.status_code == 200, r_a_works_again.text
            assert r_a_works_again.json()["output"] == "V1_OUTPUT"

            r_b_now_revoked = client.post("/api/webhooks/RESTORE_REPUBLISH_TOKEN_B", json={"input": "hi"})
            assert r_b_now_revoked.status_code == 404

            with Session(engine) as s:
                rows = s.exec(text(
                    "SELECT token_hash, enabled FROM webhookendpoint WHERE agent_id = :a"
                ), params={"a": agent["id"]}).all()
                assert len(rows) == 2
                assert len({r[0] for r in rows}) == 2  # no duplicate token_hash rows
        finally:
            cleanup_agent(agent["id"])
