"""A HITL pause must not persist a plaintext copy of a secret.

The reported gap: `_execute_flow` stored its ENTIRE runtime ctx in
`HitlRequest.ctx_snapshot`, a plaintext JSONB column. ctx accumulates
every node's output for the whole run, so a pause wrote prompts,
retrieved documents, tool results and — concretely — the HTTP node's own
`headers_sent`, which is exactly where `Authorization: Bearer
{{env.API_KEY}}` lands once interpolate_config has substituted the real
value. That is a second, unencrypted copy of a secret whose whole point
was to be encrypted at rest (EnvVar, §0.59).

The release criterion, run literally: put a known secret in node
configuration, pause for HITL, then assert the persisted snapshot cannot
reveal it — including through the encrypted column, the decoded state,
and the raw JSONB text as the database itself stores it.
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlmodel import Session, delete, select

from app.db import engine
from app.hitl_state import (RESUME_CTX_KEYS, decode_snapshot, encode_snapshot,
                            is_undecryptable, resume_state)

SECRET = "sk-live-DO-NOT-PERSIST-8f3a91c2b7d4e6"
SECOND_SECRET = "pg-password-QZ7m2LkT"


def _runtime_ctx():
    """A ctx shaped like a real paused run: the HTTP node's own
    http_output with headers_sent, a prompt the secret was interpolated
    into, tool output, retrieved documents, and the caller payloads."""
    return {
        "input": "check the vendor invoice",
        "agent_id": "a-hitl-secret", "agent_name": "HITL secret test",
        "invoking_user": "reviewer@example.com",
        "output": "pending approval",
        "structured_output": {"amount": "1200"},
        "_objects_to_check": ["pg_sales.public.orders"],
        "_agent_chain": ["a-hitl-secret"],
        "_subagent_manifest": {},
        "_chat_history_enabled": True,
        # Dropped entirely by the schema — trigger-time only, and the
        # likeliest carrier of a caller's own bearer token.
        "_webhook_payload": {"headers": {"Authorization": f"Bearer {SECRET}"}},
        "_structured_inputs": {"api_key": SECRET},
        "_idempotency_key": "node-42-attempt-1",
        "_current_node_id": "n5",
        "_durable_execution": True,
        "_llm_usage": [{"input_tokens": 10}],
        "vars": {
            "http_output": {
                "status_code": 200,
                "body": '{"ok": true}',
                # The concrete leak named in the report.
                "headers_sent": {"Authorization": f"Bearer {SECRET}",
                                 "X-Api-Key": SECRET,
                                 "Cookie": "session=abc123"},
                "params_sent": {"token": SECRET},
            },
            # A secret already substituted INTO free text, where no key
            # name gives it away.
            "llm_prompt": f"Call the API with key {SECRET} and summarise",
            "tool_output": f"connected using {SECOND_SECRET}",
            "retrieved_chunks": ["invoice 7781 total 1200", "vendor: ACME"],
            "approver": "finance@example.com",
        },
    }


ENV_MAP = {"API_KEY": SECRET, "DB_PASSWORD": SECOND_SECRET, "REGION": "eu-west-1"}


class TestTheReleaseCriterion:
    """A known secret in runtime state must not be recoverable from the
    persisted snapshot — by any of the three routes it could leak."""

    def test_the_decoded_resume_state_contains_neither_secret(self):
        state = resume_state(_runtime_ctx(), ENV_MAP)
        blob = json.dumps(state)
        assert SECRET not in blob
        assert SECOND_SECRET not in blob

    def test_the_stored_column_value_contains_neither_secret(self):
        stored = encode_snapshot(resume_state(_runtime_ctx(), ENV_MAP))
        blob = json.dumps(stored)
        assert SECRET not in blob
        assert SECOND_SECRET not in blob

    def test_the_secret_is_gone_even_from_free_text_it_was_interpolated_into(self):
        """The part a key-name filter cannot do: the secret is inside a
        prompt string, not under a key called "api_key"."""
        state = resume_state(_runtime_ctx(), ENV_MAP)
        prompt = state["vars"]["llm_prompt"]
        assert SECRET not in prompt
        assert "redacted" in prompt
        # The surrounding text survives — this is a redaction, not a drop.
        assert "summarise" in prompt

    def test_credentials_are_removed_by_key_name_too(self):
        """Belt and braces: a credential whose value is NOT a configured
        EnvVar (a cookie minted by the remote server, say) has no known
        value to match, so the key name has to catch it."""
        state = resume_state(_runtime_ctx(), ENV_MAP)
        http = state["vars"]["http_output"]
        assert "abc123" not in json.dumps(http), "a Cookie value survived"
        for key in ("headers_sent", "params_sent"):
            assert http[key] == "[redacted — not persisted in the HITL resume state]"

    def test_the_raw_database_text_reveals_nothing(self):
        """Through the real column, as the database actually stores it —
        the form an operator with read access, a backup or a log would
        see."""
        from app.models import Agent, HitlRequest

        agent_id = f"a-hitl-{uuid.uuid4().hex[:8]}"
        req_id = f"hitl-{uuid.uuid4().hex[:10]}"
        with Session(engine) as s:
            s.add(Agent(id=agent_id, name="HITL secret test", type="Workflow",
                        subtype="Workflow", plane="Control", owner="tester",
                        app_type="workflow", flow={"nodes": [], "edges": []}))
            s.commit()
            s.add(HitlRequest(
                id=req_id, agent_id=agent_id, node_id="n5", invoking_user="reviewer",
                ctx_snapshot=encode_snapshot(resume_state(_runtime_ctx(), ENV_MAP)),
            ))
            s.commit()
        try:
            from sqlalchemy import text as sql_text
            with Session(engine) as s:
                raw = s.exec(sql_text(
                    "select ctx_snapshot::text from hitlrequest where id = :i"
                ), params={"i": req_id}).one()[0]
            assert SECRET not in raw
            assert SECOND_SECRET not in raw
            assert "abc123" not in raw
        finally:
            with Session(engine) as s:
                s.exec(delete(HitlRequest).where(HitlRequest.id == req_id))
                s.exec(delete(Agent).where(Agent.id == agent_id))
                s.commit()


class TestTheResumeStateIsNarrow:
    def test_only_allowlisted_keys_are_persisted(self):
        state = resume_state(_runtime_ctx(), ENV_MAP)
        assert set(state) <= set(RESUME_CTX_KEYS)

    @pytest.mark.parametrize("dropped", [
        "_webhook_payload", "_structured_inputs", "_llm_usage",
        "_idempotency_key", "_current_node_id", "_durable_execution",
    ])
    def test_keys_that_cannot_matter_after_a_pause_are_dropped(self, dropped):
        assert dropped not in resume_state(_runtime_ctx(), ENV_MAP)

    def test_a_stale_idempotency_key_is_never_carried_across_a_pause(self):
        """Not only unnecessary but wrong: it keys idempotency for a node
        that already finished, so carrying it could make a later node's
        first real attempt look like a retry."""
        assert "_idempotency_key" not in resume_state(_runtime_ctx(), ENV_MAP)

    def test_the_state_resume_actually_reads_is_still_there(self):
        """The other half of correctness. Dropping vars would resume the
        flow against silently empty variables — a wrong answer reported
        as a right one, which is worse than what was fixed."""
        state = resume_state(_runtime_ctx(), ENV_MAP)
        for needed in ("input", "vars", "output", "structured_output",
                       "agent_id", "agent_name", "invoking_user",
                       "_objects_to_check", "_agent_chain", "_chat_history_enabled"):
            assert needed in state, f"resume needs {needed} and it was dropped"
        # Non-secret variables downstream nodes reference by name survive.
        assert state["vars"]["retrieved_chunks"] == ["invoice 7781 total 1200", "vendor: ACME"]
        assert state["vars"]["approver"] == "finance@example.com"
        assert state["vars"]["http_output"]["status_code"] == 200
        assert state["output"] == "pending approval"

    def test_vars_is_always_present_even_for_an_empty_ctx(self):
        assert resume_state({}, None) == {"vars": {}}


class TestEncryptionAtRest:
    def test_a_durable_key_seals_the_blob(self, monkeypatch):
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY",
                           "aeER5wskKgtIDSnKqQpPXDDD5GfjPqAV2CSDpegqhSE=")
        import app.secrets_crypto as crypto
        monkeypatch.setattr(crypto, "using_ephemeral_key", lambda: False)

        stored = encode_snapshot({"vars": {"note": "hello"}, "output": "x"})
        assert "__enc__" in stored
        assert "hello" not in json.dumps(stored), "the payload is readable in the column"
        assert decode_snapshot(stored) == {"vars": {"note": "hello"}, "output": "x"}

    def test_a_snapshot_written_before_this_change_still_resumes(self):
        """A plaintext row from an older build must not fail closed —
        someone's paused work is on the other side of it."""
        legacy = {"vars": {"a": 1}, "output": "done"}
        assert decode_snapshot(legacy) == legacy

    def test_an_undecryptable_snapshot_is_reported_not_silently_empty(self):
        """Resuming with {} would run the remaining flow against missing
        variables and report success."""
        state = decode_snapshot({"__enc__": "enc:v1:not-a-real-token"})
        assert is_undecryptable(state)
        assert state != {}

    def test_an_empty_or_missing_snapshot_decodes_to_empty(self):
        assert decode_snapshot(None) == {}
        assert decode_snapshot({}) == {}


class TestItIsDeletedAfterCompletion:
    def test_resolving_a_request_clears_its_resume_state(self):
        import inspect

        from app.routers.execution import respond_to_hitl

        src = inspect.getsource(respond_to_hitl)
        assert "hitl.ctx_snapshot = {}" in src, "the resolved row keeps its resume state"
        # And the delegated carry-forward must read the captured copy,
        # not the cleared field.
        assert "carried_snapshot" in src


class TestEveryWriteSiteIsSealed:
    def test_no_write_site_stores_a_raw_snapshot(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "routers", "execution.py")).read()
        for line in source.splitlines():
            if "ctx_snapshot=" in line and "encode_hitl_snapshot" not in line:
                assert "carried_snapshot" in line, f"unsealed snapshot write: {line.strip()}"

    def test_the_harness_narrows_before_handing_the_snapshot_over(self):
        import inspect

        from app import harness

        src = inspect.getsource(harness._execute_flow)
        assert "hitl_resume_state(ctx, env_map)" in src
        assert '"hitl_ctx_snapshot": ctx,' not in src, "the whole runtime ctx is still persisted"
