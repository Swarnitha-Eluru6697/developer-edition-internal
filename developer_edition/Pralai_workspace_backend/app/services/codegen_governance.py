"""Governance gate for codegen source-context egress (WS-AI-011).

Whether the current file's content — potentially proprietary source,
secrets or personal data — is sent to the model is decided here, and only
here, by a single fail-closed chain that applies to every deployment:

  1. the user explicitly opted in for this turn (`include_current_file`) —
     the client no longer sends the open file by default;
  2. the org allows source context to be sent to the model at all
     (CodegenOrgSettings.allow_external_context, admin-set, audited);
  3. the model provider is on the org's allowlist;
  4. if the gateway is NOT inside the customer perimeter, UCS allows this
     principal `codegen.send_external_context` on the governed model object
     (settings.codegen_ucs_model_object) — the content leaves the boundary;
  5. secret redaction and personal-data classification/redaction.

An in-perimeter gateway used to skip 1-3 entirely. The decision returned
names the categories sent / redacted and why, for the audit record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.config import get_settings
from app.models import CodegenOrgSettings
from app.services.ucs_client import UcsAuthorizationError, authorize as ucs_authorize

# The default model provider codegen calls: the self-hosted, in-perimeter model
# gateway (app/codegen_agent/model_client.py -> CODEGEN_GATEWAY_URL). Named
# explicitly rather than assumed — an admin must allowlist "codegen_gateway"
# for file content to be sent. When an admin switches the agent to OpenAI or
# Anthropic (Settings), THAT provider's name ("openai" / "anthropic", see
# codegen_provider.GOVERNANCE_NAMES) is what the allowlist is checked against,
# and it is always treated as outside the perimeter.
CODEGEN_PROVIDER = "codegen_gateway"
EXTERNAL_PROVIDER_NAMES = {"openai", "anthropic"}

UCS_ACTION_SEND_EXTERNAL_CONTEXT = "codegen.send_external_context"


@dataclass
class ContextDecision:
    """Auditable record of whether source context was included in a turn."""
    include_context: bool
    categories: list[str] = field(default_factory=list)  # e.g. ["file_content"]
    redacted_categories: list[str] = field(default_factory=list)  # secret types stripped, if any
    reason: str = ""  # e.g. "allowed" | "org_policy_disabled" | "ucs_denied:<detail>"


# Best-effort secret patterns. Defense in depth on top of the policy gate
# above, not a substitute for it — a false negative here still can't leak
# more than what the org already authorized sending.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]+?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    )),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("connection_string_credential", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@'\"]+:[^\s:/@'\"]+@")),
    ("generic_secret_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|client[_-]?secret)"
        r"\b\s*[=:]\s*['\"]?[A-Za-z0-9/_\-+=.]{12,}['\"]?"
    )),
]


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


# Personal-data classification. Matching content is redacted and its
# category recorded ("pii_*") — the classifier the review requires next to
# secret redaction. Card numbers are Luhn-checked to keep false positives low.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pii_email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("pii_us_ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    ("pii_iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
]
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def classify_and_redact_pii(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    redacted = text
    for category, pattern in _PII_PATTERNS:
        if pattern.search(redacted):
            found.append(category)
            redacted = pattern.sub("[REDACTED]", redacted)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            if "pii_payment_card" not in found:
                found.append("pii_payment_card")
            return "[REDACTED]"
        return m.group(0)
    redacted = _CARD.sub(card, redacted)
    return redacted, found


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Strip recognizable secret material from `text`.

    Returns (redacted_text, categories_found). Every category whose pattern
    matched anywhere in the text is reported once, regardless of how many
    matches it had, so the audit record names what KIND of thing was
    stripped without echoing the count (which could itself hint at content).
    """
    found: list[str] = []
    redacted = text
    for category, pattern in _SECRET_PATTERNS:
        if pattern.search(redacted):
            found.append(category)
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted, found


async def get_org_settings(db: AsyncSession) -> CodegenOrgSettings:
    """Get-or-create this deployment's codegen governance row.

    Fails closed: a brand-new deployment gets allow_external_context=False
    and an empty provider allowlist until an admin makes the opposite an
    explicit, recorded decision — never an implicit allow because the row
    didn't exist yet.
    """
    settings = get_settings()
    row = (await db.execute(
        select(CodegenOrgSettings).where(CodegenOrgSettings.org_id == settings.org_id)
    )).scalar_one_or_none()
    if row is None:
        row = CodegenOrgSettings(org_id=settings.org_id)
        db.add(row)
        await db.flush()
    return row


async def decide_context_inclusion(
    db: AsyncSession,
    principal: Principal,
    *,
    workspace_id: UUID,
    raw_context: str | None,
    user_opted_in: bool = False,
    provider: str | None = CODEGEN_PROVIDER,
) -> tuple[str | None, ContextDecision]:
    """Decide whether `raw_context` (the current file's content) may be
    included in this turn's prompt. Returns (content_to_send_or_None,
    decision) — callers must send the returned content, never `raw_context`.

    `provider` is the governance name of the model that will receive it ("codegen_gateway", "openai", "anthropic"); None means
    it could not be determined, and nothing is sent.
    """
    if not raw_context:
        return None, ContextDecision(include_context=False, reason="no_context_supplied")
    if not user_opted_in:
        return None, ContextDecision(include_context=False, reason="user_not_opted_in")

    org_settings = await get_org_settings(db)
    if not org_settings.allow_external_context:
        return None, ContextDecision(include_context=False, reason="org_policy_disabled")
    if provider is None:
        return None, ContextDecision(include_context=False, reason="provider_unknown")
    if provider not in (org_settings.allowed_providers or []):
        return None, ContextDecision(include_context=False, reason="provider_not_allowlisted")

    settings = get_settings()
    in_perimeter = settings.codegen_gateway_in_perimeter and provider not in EXTERNAL_PROVIDER_NAMES
    if not in_perimeter:
        model_object = settings.codegen_ucs_model_object
        if not model_object:
            return None, ContextDecision(include_context=False, reason="ucs_object_not_configured")
        try:
            await ucs_authorize(principal, action=UCS_ACTION_SEND_EXTERNAL_CONTEXT, object_name=model_object)
        except UcsAuthorizationError as e:
            return None, ContextDecision(include_context=False, reason=f"ucs_denied:{e.reason}")

    content = raw_context
    redacted_categories: list[str] = []
    if org_settings.redact_secrets:
        content, secret_cats = redact_secrets(content)
        content, pii_cats = classify_and_redact_pii(content)
        redacted_categories = secret_cats + pii_cats

    return content, ContextDecision(
        include_context=True,
        categories=["file_content"],
        redacted_categories=redacted_categories,
        reason="allowed_in_perimeter" if in_perimeter else "allowed_external",
    )
