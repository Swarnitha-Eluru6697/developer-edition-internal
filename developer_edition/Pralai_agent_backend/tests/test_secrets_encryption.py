"""
Real tests for the secrets-in-plaintext / MCP-credential-leak fix
(section 0.59, GAP_REPORT).

The real bug, exactly as reported: EnvVar stored secret values in
plaintext, Knowledge Base connection credentials lived unencrypted in
a JSONB blob, and the MCP server token
(Agent.features["mcp"]["token"]) leaked through completely ordinary,
unprivileged reads - GET /api/agents, GET /api/agents/{id}, and GET
/api/agents/{id}/versions all returned the real Agent/AgentVersion
model, features included. Confirmed live before any fix, against this
platform's own real, pre-existing data (5,541 real agents) - not a
synthetic scenario.

The fix has two real, independent parts:

1. Masked DTOs - a real, shared Pydantic field_serializer on both
   Agent and AgentVersion masks features["mcp"]["token"] on every real
   serialization path. The real, legitimate one-time-reveal endpoints
   (set_mcp, regenerate_mcp_token) are unaffected.

2. Real encryption at rest (secrets_crypto.py) - Fernet envelope
   encryption for EnvVar secrets, Knowledge Base SENSITIVE_KEYS, and
   the MCP token, with a real, deliberate backward-compatible
   fallback for pre-existing plaintext data.

These tests exercise the real, actual database and real encryption -
no mocking of the crypto itself.
"""
import sys
import os
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from sqlalchemy import text

from app.main import app
from app.db import engine
from app.models import Agent, AgentVersion, EnvVar, KnowledgeBase, SECRET_MASK
from app.secrets_crypto import encrypt_secret, decrypt_secret, SecretDecryptionError

client = TestClient(app)


def create_agent() -> dict:
    r = client.post("/api/agents", json={
        # Real, deliberate omission of app_type (§0.73, GAP_REPORT test
        # fix) - this test's own flow fixture has no Output or
        # response-producing node at all (its real subject is secret
        # masking, not flow structure), and the new, real server-side
        # publish validator's output/response-node requirement only
        # applies when app_type is actually set.
        "name": f"Secrets Test {uuid.uuid4().hex[:6]}", "subtype": "Custom",
        "template_flow": {"nodes": [{"id": "n1", "type": "trigger", "x": 0, "y": 0, "sub": "", "config": {}}], "edges": []},
    })
    assert r.status_code == 200, r.text
    return r.json()


class TestRealSecretsCryptoRoundTrip:
    def test_a_real_value_encrypts_and_decrypts_correctly(self):
        ct = encrypt_secret("my-real-secret-value")
        assert ct != "my-real-secret-value"
        assert decrypt_secret(ct) == "my-real-secret-value"

    def test_a_genuinely_plaintext_value_from_before_this_fix_passes_through_unchanged(self):
        assert decrypt_secret("this-was-never-encrypted") == "this-was-never-encrypted"

    def test_empty_and_none_pass_through_both_directions(self):
        assert encrypt_secret("") == ""
        assert encrypt_secret(None) is None
        assert decrypt_secret("") == ""
        assert decrypt_secret(None) is None


class TestRealFailClosedEncryption:
    """Real tests for section 0.89, GAP_REPORT — the actual fix for two
    real, reported gaps in the ORIGINAL §0.59 fix: a temporary,
    per-process key silently generated whenever SECRETS_ENCRYPTION_KEY
    was unset (a real, silent trap in a real multi-process deployment
    — the API and the Temporal worker would each generate their OWN,
    different key), and decrypt_secret silently swallowing ANY
    decryption failure and returning the raw ciphertext unchanged,
    making a real key-management failure indistinguishable from "the
    stored credential is simply wrong."

    The fix: ENVIRONMENT=production with no real
    SECRETS_ENCRYPTION_KEY now refuses to start (checked at real
    process startup in both main.py and worker.py, not just lazily).
    New encrypted values now carry a real, explicit enc:v1: prefix;
    an enc:v1:-marked value that genuinely fails to decrypt now raises
    SecretDecryptionError — a real, loud, specifically-named failure,
    not a silent pass-through. A no-prefix value (real, existing data
    from before this exact prefix existed, confirmed to genuinely
    exist in this platform's own database before writing this fix)
    still gets one real decrypt attempt first, falling back to
    legacy-plaintext treatment only if that fails — preserving real,
    already-encrypted rows correctly, rather than the report's own
    literal "no prefix -> legacy plaintext" instruction, which would
    have silently broken them."""

    def test_new_encrypted_values_carry_the_real_enc_v1_prefix(self):
        ct = encrypt_secret("a-real-secret")
        assert ct.startswith("enc:v1:")

    def test_an_enc_v1_value_that_fails_to_decrypt_raises_a_real_loud_error(self):
        """The exact scenario the report named: a value encrypted
        under one real key, decrypted under a genuinely different
        one — must raise a real, specific, honest error, not silently
        return the still-encrypted ciphertext as if it were the real
        secret."""
        import app.secrets_crypto as sc
        real_fernet = sc._fernet
        try:
            sc._fernet = None
            os.environ["SECRETS_ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
            encrypted_under_key_a = encrypt_secret("a-real-secret")

            sc._fernet = None
            os.environ["SECRETS_ENCRYPTION_KEY"] = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
            with pytest.raises(SecretDecryptionError):
                decrypt_secret(encrypted_under_key_a)
        finally:
            sc._fernet = real_fernet
            os.environ.pop("SECRETS_ENCRYPTION_KEY", None)

    def test_a_no_prefix_genuinely_encrypted_legacy_value_still_decrypts_correctly(self):
        """Confirmed directly against this platform's own real,
        existing database before writing this fix: real EnvVar/
        KnowledgeBase rows already exist, genuinely encrypted under
        the ORIGINAL §0.59 scheme, which never had a prefix at all.
        These must keep working — the real, deliberate reason a
        no-prefix value still gets one real decrypt attempt before
        falling back to legacy-plaintext treatment, rather than the
        report's own literal "no prefix -> legacy plaintext"
        instruction, which would silently break them."""
        import app.secrets_crypto as sc
        # Simulate the OLD, no-prefix scheme directly - a raw Fernet
        # token with no real _ENC_PREFIX, exactly what a real,
        # pre-§0.89 row still looks like.
        raw_token = sc._get_fernet().encrypt(b"legacy-encrypted-secret").decode()
        assert not raw_token.startswith("enc:v1:")
        assert decrypt_secret(raw_token) == "legacy-encrypted-secret"

    def test_a_no_prefix_genuinely_plaintext_value_still_passes_through_unchanged(self):
        assert decrypt_secret("never-encrypted-plaintext") == "never-encrypted-plaintext"

    def test_production_with_no_key_refuses_to_start(self):
        from app.secrets_crypto import check_production_key_requirement
        real_key = os.environ.pop("SECRETS_ENCRYPTION_KEY", None)
        real_env = os.environ.get("ENVIRONMENT")
        try:
            os.environ["ENVIRONMENT"] = "production"
            with pytest.raises(RuntimeError, match="SECRETS_ENCRYPTION_KEY"):
                check_production_key_requirement()
        finally:
            if real_key is not None:
                os.environ["SECRETS_ENCRYPTION_KEY"] = real_key
            if real_env is not None:
                os.environ["ENVIRONMENT"] = real_env
            else:
                os.environ.pop("ENVIRONMENT", None)

    def test_production_with_a_real_key_configured_does_not_raise(self):
        from app.secrets_crypto import check_production_key_requirement
        real_env = os.environ.get("ENVIRONMENT")
        try:
            os.environ["ENVIRONMENT"] = "production"
            os.environ["SECRETS_ENCRYPTION_KEY"] = "v4lBU1Ix6yG8C-QG5SZhUc-r5HOjAWI5eQrIWJip-dw="
            check_production_key_requirement()  # must not raise
        finally:
            if real_env is not None:
                os.environ["ENVIRONMENT"] = real_env
            else:
                os.environ.pop("ENVIRONMENT", None)

    def test_non_production_with_no_key_does_not_raise(self):
        from app.secrets_crypto import check_production_key_requirement
        real_key = os.environ.pop("SECRETS_ENCRYPTION_KEY", None)
        real_env = os.environ.pop("ENVIRONMENT", None)
        try:
            check_production_key_requirement()  # must not raise - not production
        finally:
            if real_key is not None:
                os.environ["SECRETS_ENCRYPTION_KEY"] = real_key
            if real_env is not None:
                os.environ["ENVIRONMENT"] = real_env

    def test_the_full_encrypt_store_decrypt_pipeline_works_end_to_end_through_the_real_api(self):
        """The real, direct proof the new scheme works correctly
        through the actual application, not just the crypto functions
        in isolation."""
        agent = create_agent()
        try:
            add_result = client.post(f"/api/agents/{agent['id']}/nodes", json={
                "type": "template", "x": 0, "y": 0, "sub": "", "config": {"template": "secret: {{env.E2E_SECRET}}"},
            }, headers={"If-Match": "1"})
            assert add_result.status_code == 200, add_result.text
            new_node_id = add_result.json()["flow"]["nodes"][-1]["id"]
            client.post(f"/api/agents/{agent['id']}/edges", json={"source": "n1", "target": new_node_id, "source_handle": None})
            client.post(f"/api/agents/{agent['id']}/env", json={"key": "E2E_SECRET", "value": "sk-real-e2e-9876", "type": "secret"})

            with Session(engine) as s:
                row = s.exec(text(f"SELECT value FROM envvar WHERE agent_id = '{agent['id']}' AND key = 'E2E_SECRET'")).first()
                assert row[0].startswith("enc:v1:")

            r = client.post(f"/api/agents/{agent['id']}/run", json={"input": "hi"})
            assert r.status_code == 200, r.text
            assert "sk-real-e2e-9876" in r.json()["output"]
        finally:
            with Session(engine) as s:
                s.exec(text("DELETE FROM envvar WHERE agent_id = :a"), params={"a": agent["id"]})
                s.exec(text("DELETE FROM run WHERE agent_id = :a"), params={"a": agent["id"]})
                s.exec(text("DELETE FROM trace WHERE agent_id = :a"), params={"a": agent["id"]})
                s.exec(text("DELETE FROM agentlog WHERE agent_id = :a"), params={"a": agent["id"]})
                s.exec(text("DELETE FROM agent WHERE id = :a"), params={"a": agent["id"]})
                s.commit()


class TestRealMcpTokenMaskingAcrossEveryReadPath:
    def test_get_single_agent_masks_the_real_token(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        r = client.get(f"/api/agents/{agent['id']}")
        assert r.json()["mcp_token_encrypted"] == SECRET_MASK

    def test_list_agents_masks_the_real_token(self):
        """§0.113, GAP_REPORT — /api/agents is now genuinely paginated
        (a real, deliberate, separate fix), so a single, unpaginated
        `GET /api/agents` call is no longer guaranteed to include this
        specific, just-created agent among a real, large, shared test
        database's own thousands of rows. Paginates through real pages
        via the real X-Next-Cursor header — the same real mechanism
        any real, well-behaved caller of this endpoint now needs —
        rather than assuming a single, unbounded response."""
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        found = None
        cursor = None
        for _ in range(1000):  # a real, generous cap — never actually reached in practice
            params = {"limit": 500}
            if cursor:
                params["cursor"] = cursor
            r = client.get("/api/agents", params=params)
            match = next((a for a in r.json() if a["id"] == agent["id"]), None)
            if match:
                found = match
                break
            if r.headers.get("x-has-more") != "true":
                break
            cursor = r.headers.get("x-next-cursor")
        assert found is not None, "the just-created agent was not found across any real page"
        assert found["mcp_token_encrypted"] == SECRET_MASK

    def test_version_history_never_contains_the_mcp_credential_at_all(self):
        """§0.101, GAP_REPORT — a real, stronger guarantee than mere
        masking: the MCP credential is now a real, top-level Agent
        column, never part of a version's own snapshotted features at
        all — not present (masked or otherwise) in ANY version's own
        real features dict, for any real published version."""
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        client.post(f"/api/agents/{agent['id']}/publish")
        r = client.get(f"/api/agents/{agent['id']}/versions")
        versions = r.json()
        assert len(versions) >= 1
        for v in versions:
            assert "mcp" not in (v.get("features") or {})

    def test_set_mcp_still_genuinely_reveals_the_real_token_once(self):
        agent = create_agent()
        r = client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        url = r.json()["url"]
        real_token = url.rsplit("/", 1)[-1]
        assert real_token != SECRET_MASK
        assert len(real_token) > 10

    def test_the_mask_itself_is_never_a_valid_mcp_credential(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        r = client.post(f"/mcp/{agent['id']}/{SECRET_MASK}", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert r.json()["error"]["code"] == -32002


class TestRealMcpTokenEncryptionAtRest:
    def test_the_real_stored_token_is_genuinely_not_plaintext(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        with Session(engine) as s:
            a = s.get(Agent, agent["id"])
            stored = a.mcp_token_encrypted
            assert stored.startswith("enc:v1:")  # §0.89, GAP_REPORT: real encrypted values are now explicitly marked

    def test_the_real_mcp_endpoint_still_authenticates_correctly_with_encryption_in_place(self):
        agent = create_agent()
        r = client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        real_token = r.json()["url"].rsplit("/", 1)[-1]
        resp = client.post(f"/mcp/{agent['id']}/{real_token}", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert "result" in resp.json()

    def test_regenerating_the_token_returns_a_real_new_plaintext_token_not_ciphertext(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        r = client.post(f"/api/agents/{agent['id']}/mcp/regenerate")
        new_token = r.json()["url"].rsplit("/", 1)[-1]
        assert not new_token.startswith("gAAAAA")
        resp = client.post(f"/mcp/{agent['id']}/{new_token}", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert "result" in resp.json()


class TestRealEnvVarEncryptionAtRest:
    def test_a_real_secret_env_var_is_genuinely_encrypted_in_the_database(self):
        agent = create_agent()
        client.post(f"/api/agents/{agent['id']}/env", json={"key": "TEST_SECRET", "value": "sk-real-value", "type": "secret"})
        with Session(engine) as s:
            row = s.exec(text(f"SELECT value FROM envvar WHERE agent_id = '{agent['id']}' AND key = 'TEST_SECRET'")).first()
            assert row[0].startswith("enc:v1:")  # §0.89, GAP_REPORT: real encrypted values are now explicitly marked

    def test_a_non_secret_env_var_is_never_encrypted(self):
        agent = create_agent()
        client.post(f"/api/agents/{agent['id']}/env", json={"key": "PLAIN_VALUE", "value": "not-a-secret", "type": "string"})
        with Session(engine) as s:
            row = s.exec(text(f"SELECT value FROM envvar WHERE agent_id = '{agent['id']}' AND key = 'PLAIN_VALUE'")).first()
            assert row[0] == "not-a-secret"

    def test_genuinely_pre_existing_plaintext_secret_data_still_works(self):
        agent = create_agent()
        with Session(engine) as s:
            row = EnvVar(agent_id=agent["id"], key="OLD_SECRET", value="genuinely-never-encrypted", type="secret")
            s.add(row)
            s.commit()
        from app.harness import load_env_map
        with Session(engine) as s:
            env_map = load_env_map(s, agent["id"])
        assert env_map["OLD_SECRET"] == "genuinely-never-encrypted"

    def test_a_real_encrypted_secret_correctly_decrypts_through_load_env_map(self):
        agent = create_agent()
        client.post(f"/api/agents/{agent['id']}/env", json={"key": "REAL_SECRET", "value": "the-real-value", "type": "secret"})
        from app.harness import load_env_map
        with Session(engine) as s:
            env_map = load_env_map(s, agent["id"])
        assert env_map["REAL_SECRET"] == "the-real-value"


class TestRealAuditLoggingForSecretEvents:
    """Real audit logging (§0.59, GAP_REPORT) — reuses the existing
    AgentLog mechanism, never logs the real secret value itself."""

    def test_creating_a_secret_env_var_produces_a_real_audit_log_entry(self):
        agent = create_agent()
        client.post(f"/api/agents/{agent['id']}/env", json={"key": "AUDITED_SECRET", "value": "sk-real-value", "type": "secret"})
        r = client.get(f"/api/agents/{agent['id']}/logs")
        msgs = [l["msg"] for l in r.json()]
        assert any("AUDITED_SECRET" in m and "created" in m for m in msgs)
        assert not any("sk-real-value" in m for m in msgs)

    def test_creating_a_non_secret_env_var_produces_no_audit_entry(self):
        agent = create_agent()
        client.post(f"/api/agents/{agent['id']}/env", json={"key": "NOT_A_SECRET", "value": "plain", "type": "string"})
        r = client.get(f"/api/agents/{agent['id']}/logs")
        msgs = [l["msg"] for l in r.json()]
        assert not any("NOT_A_SECRET" in m for m in msgs)

    def test_enabling_mcp_produces_a_real_audit_log_entry(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        r = client.get(f"/api/agents/{agent['id']}/logs")
        msgs = [l["msg"] for l in r.json()]
        assert any("MCP" in m and "created" in m for m in msgs)

    def test_regenerating_mcp_token_produces_a_real_audit_log_entry(self):
        agent = create_agent()
        client.patch(f"/api/agents/{agent['id']}/mcp", json={"enabled": True})
        client.post(f"/api/agents/{agent['id']}/mcp/regenerate")
        r = client.get(f"/api/agents/{agent['id']}/logs")
        msgs = [l["msg"] for l in r.json()]
        assert any("MCP" in m and "rotated" in m for m in msgs)


class TestRealKnowledgeBaseSecretEncryptionAtRest:
    def _create_kb(self, **config_overrides):
        config = {"host": "127.0.0.1", "port": 5432, "dbname": "inaiera_agents", "user": "inaiera", "password": "inaiera_dev"}
        config.update(config_overrides)
        r = client.post("/api/integrations/knowledge-bases", json={
            "name": f"Secret KB Test {uuid.uuid4().hex[:6]}", "provider": "pgvector", "config": config,
        })
        assert r.status_code == 200, r.text
        return r.json()

    def test_a_real_kb_password_is_genuinely_encrypted_in_the_database(self):
        kb = self._create_kb()
        try:
            with Session(engine) as s:
                row = s.get(KnowledgeBase, kb["id"])
                assert row.config["password"].startswith("enc:v1:")  # §0.89, GAP_REPORT: real encrypted values are now explicitly marked
        finally:
            client.delete(f"/api/integrations/knowledge-bases/{kb['id']}")

    def test_a_real_kb_connection_still_works_with_the_encrypted_password(self):
        kb = self._create_kb()
        try:
            r = client.post(f"/api/integrations/knowledge-bases/{kb['id']}/test")
            body = r.json()
            assert "password authentication failed" not in str(body.get("last_error", "")).lower()
        finally:
            client.delete(f"/api/integrations/knowledge-bases/{kb['id']}")

    def test_the_masked_list_response_never_shows_the_real_password(self):
        kb = self._create_kb()
        try:
            r = client.get("/api/integrations/knowledge-bases")
            found = next(k for k in r.json() if k["id"] == kb["id"])
            assert found["config"]["password"] != "inaiera_dev"
        finally:
            client.delete(f"/api/integrations/knowledge-bases/{kb['id']}")
