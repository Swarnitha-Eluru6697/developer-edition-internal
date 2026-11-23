"""WS-AI-011: the current file reaches the model only through one
fail-closed chain — user opt-in → org policy → provider allowlist → (outside
the perimeter) a UCS decision — with secrets and personal data redacted and
every outcome named for the audit trail."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.core.auth import Principal
from app.core.config import get_settings
from app.services import codegen_governance as gov
from app.services.ucs_client import UcsAuthorizationError

PRINCIPAL = Principal(user_id="u", org_id="o", roles=[], email="u@x", issued_at=0, expires_at=0)
FILE = "select * from orders;\n-- password = 'SuperSecretValue123'\n"


@pytest.fixture
def org(monkeypatch):
    """An org that has enabled source context for the gateway."""
    row = SimpleNamespace(allow_external_context=True, allowed_providers=[gov.CODEGEN_PROVIDER], redact_secrets=True)

    async def fake_get_org_settings(db):
        return row
    monkeypatch.setattr(gov, "get_org_settings", fake_get_org_settings)
    return row


@pytest.fixture
def ucs(monkeypatch):
    calls = []
    state = {"allow": True}

    async def fake_authorize(principal, *, action, object_name):
        calls.append((action, object_name))
        if not state["allow"]:
            raise UcsAuthorizationError("no matching grant")
    monkeypatch.setattr(gov, "ucs_authorize", fake_authorize)
    return SimpleNamespace(calls=calls, state=state)


def _decide(raw=FILE, opted_in=True):
    return asyncio.run(gov.decide_context_inclusion(
        None, PRINCIPAL, workspace_id=uuid.uuid4(), raw_context=raw, user_opted_in=opted_in,
    ))


@pytest.fixture
def in_perimeter(monkeypatch):
    monkeypatch.setattr(get_settings(), "codegen_gateway_in_perimeter", True)


@pytest.fixture
def external(monkeypatch):
    monkeypatch.setattr(get_settings(), "codegen_gateway_in_perimeter", False)
    monkeypatch.setattr(get_settings(), "codegen_ucs_model_object", "ml_models.codegen.generation")


# ── Each gate withholds on its own ───────────────────────────────────────

def test_no_context(org, in_perimeter):
    content, d = _decide(raw=None)
    assert content is None and d.reason == "no_context_supplied"


def test_user_must_opt_in(org, in_perimeter):
    content, d = _decide(opted_in=False)
    assert content is None and d.reason == "user_not_opted_in" and not d.include_context


@pytest.mark.parametrize("mode", ["in_perimeter", "external"])
def test_org_policy_applies_in_every_mode(org, request, mode, ucs):
    request.getfixturevalue(mode)
    org.allow_external_context = False
    content, d = _decide()
    assert content is None and d.reason == "org_policy_disabled"
    assert ucs.calls == []


@pytest.mark.parametrize("mode", ["in_perimeter", "external"])
def test_provider_allowlist_applies_in_every_mode(org, request, mode, ucs):
    request.getfixturevalue(mode)
    org.allowed_providers = []
    content, d = _decide()
    assert content is None and d.reason == "provider_not_allowlisted"


def test_external_requires_ucs_decision_on_the_model(org, external, ucs):
    ucs.state["allow"] = False
    content, d = _decide()
    assert content is None and d.reason.startswith("ucs_denied:")
    assert ucs.calls == [("codegen.send_external_context", "ml_models.codegen.generation")]


def test_external_without_a_configured_ucs_object_fails_closed(org, external, ucs, monkeypatch):
    monkeypatch.setattr(get_settings(), "codegen_ucs_model_object", None)
    content, d = _decide()
    assert content is None and d.reason == "ucs_object_not_configured" and ucs.calls == []


# ── Allowed paths redact and report ──────────────────────────────────────

def test_in_perimeter_allowed_without_ucs_but_redacted(org, in_perimeter, ucs):
    content, d = _decide()
    assert d.include_context and d.reason == "allowed_in_perimeter"
    assert "SuperSecretValue123" not in content and "select * from orders" in content
    assert d.redacted_categories == ["generic_secret_assignment"]
    assert ucs.calls == []        # content never leaves the boundary


def test_external_allowed_after_ucs_allow(org, external, ucs):
    content, d = _decide()
    assert d.include_context and d.reason == "allowed_external"
    assert len(ucs.calls) == 1


def test_personal_data_is_classified_and_redacted(org, in_perimeter):
    raw = ("-- owner: jane.doe@acme.com\n-- ssn 123-45-6789\n"
           "-- card 4111 1111 1111 1111, order 1234 5678 9012 3456\nselect 1;")
    content, d = _decide(raw=raw)
    assert "jane.doe@acme.com" not in content and "123-45-6789" not in content
    assert "4111 1111 1111 1111" not in content
    assert "1234 5678 9012 3456" in content       # not Luhn-valid → not a card
    assert {"pii_email", "pii_us_ssn", "pii_payment_card"} <= set(d.redacted_categories)


def test_redaction_can_be_turned_off_by_the_org(org, in_perimeter):
    org.redact_secrets = False
    content, d = _decide()
    assert content == FILE and d.redacted_categories == []
