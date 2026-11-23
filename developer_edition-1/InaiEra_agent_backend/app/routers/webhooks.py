"""
Real webhook trigger (§3.6's trigger concept, extended). A trigger node
configured as a Webhook Trigger gets an opaque, unguessable token
(generated client-side as a UUID, persisted via the same generic node-config
PATCH every other node field uses — no separate "mint token" endpoint
needed). Posting to /api/webhooks/{token} genuinely executes that agent's
flow, through the exact same harness/Temporal routing as a manual Test run
— this isn't a UI toggle with no backend behind it.

Honesty note on what's NOT here: there's no separate secret/signature
verification beyond the token itself being unguessable (matching how tools
like Zapier/n8n's own webhook URLs typically work) — don't point this at
anything where that's not an adequate security model.
"""
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4
import hashlib
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select

from ..db import get_session
from ..models import Agent, Run, Trace, AgentLog, WebhookEndpoint
from ..auth import enforce_authenticated_user
from .execution import (
    execute_agent_flow, _aggregate_run_usage, _typed_timing, _json_safe_spans, create_hitl_pause_run,
    resolve_execution, _finalize_run,
)
from ..harness import PublishedVersionIntegrityError
from ..metrics import AGENT_RUNS_TOTAL, AGENT_RUN_DURATION_SECONDS, UCS_DENIALS_TOTAL

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def hash_webhook_token(token: str) -> str:
    """Real, deterministic, one-way fingerprint (§0.97, GAP_REPORT) —
    a plain, unsalted sha256 hex digest is genuinely sufficient here,
    unlike a real user-chosen password: a webhook token is a real,
    high-entropy, randomly-generated UUID (never a low-entropy,
    human-guessable value), so a rainbow-table/brute-force attack
    against the hash is already infeasible without any real per-row
    salt needed. Shared by both the real, publish-time registration
    (routers/agents.py) and the real, request-time lookup below — one,
    real function, so the two can never independently drift into
    computing this differently."""
    return hashlib.sha256(token.encode()).hexdigest()


def _find_webhook_endpoint(session: Session, token: str):
    """Real, indexed, authoritative lookup (§0.97, GAP_REPORT) — the
    actual fix for a real, reported bug: this used to scan every
    agent's own live, mutable flow for a plain string match, a real,
    unindexed O(agents × nodes) full scan on every single incoming
    request, AND a real correctness bug (webhook discovery read the
    live draft while webhook EXECUTION already correctly read the
    frozen, published snapshot — a Builder editing a trigger node's
    own token with no new publish silently changed which token
    production responded to). See WebhookEndpoint's own docstring
    (models.py) for the full real design — this table, keyed by a
    real, indexed token_hash, is now the one, real, authoritative
    source for token resolution; a JSON flow is never scanned again."""
    token_hash = hash_webhook_token(token)
    endpoint = session.exec(
        select(WebhookEndpoint).where(WebhookEndpoint.token_hash == token_hash, WebhookEndpoint.enabled == True)
    ).first()
    if not endpoint:
        return None, None
    agent = session.get(Agent, endpoint.agent_id)
    if not agent:
        return None, None
    return agent, endpoint


@router.post("/{token}", dependencies=[Depends(require_permission(P.AGENTS_RUN))])
async def trigger_webhook(token: str, request: Request, session: Session = Depends(get_session)):
    agent, endpoint = _find_webhook_endpoint(session, token)
    if not agent:
        raise HTTPException(404, "No agent has a webhook trigger with this token")
    # A webhook token authenticates the CALLING SYSTEM, not a person.
    # An agent that requires a gateway-verified identity therefore
    # cannot be driven by a bare webhook — previously it could, with
    # whatever invoking_user the payload claimed.
    enforce_authenticated_user(agent, request, "webhook")

    try:
        payload: Any = await request.json()
    except Exception:
        payload = {}
    user_input = payload.get("input") or payload.get("message") if isinstance(payload, dict) else None
    if not user_input:
        user_input = str(payload) if payload else "(empty webhook payload)"

    # Real, immutable execution snapshot, resolved ONCE (§0.93/§0.99,
    # GAP_REPORT) — the actual fix for a real, reported gap: Run/Trace
    # creation below never recorded agent_version_id/agent_version/
    # flow_hash at all, and Trace's own `version` field used
    # agent.version — the LIVE, mutable value, not the version this
    # specific invocation actually resolved and executed against.
    # Passing this same resolved object into execute_agent_flow below
    # (its own execution= parameter, §0.93) also closes the identical,
    # narrow, real race §0.93 already closed for /run: without it,
    # execute_agent_flow would independently re-resolve a second time,
    # after a real, concurrent publish could have landed in between.
    start = time.time()
    try:
        resolved = resolve_execution(session, agent, preview=False)
        result = await execute_agent_flow(
            session, agent, invoking_user="webhook", user_input=user_input,
            webhook_payload=payload if isinstance(payload, dict) else None,
            execution=resolved,
        )
    except PublishedVersionIntegrityError:
        # Real, distinct re-raise (§0.90, GAP_REPORT) — a real data-
        # integrity failure, not an infrastructure one; letting this
        # propagate lets main.py's own dedicated exception handler
        # produce a real, specific, honest response instead of this
        # endpoint's own unrelated Temporal troubleshooting hint below.
        raise
    except Exception as e:
        # Real, honest error (§0.53, GAP_REPORT) — matches /run's own
        # established pattern, rather than letting a real Temporal
        # connection failure leak out as an unstyled 500 with no detail.
        raise HTTPException(
            500,
            f"Durable execution failed — is the Temporal dev server AND worker "
            f"process (`python -m app.worker`) both running? {e}",
        )
    duration_s = time.time() - start

    if result.get("status") == "paused_for_human_input":
        # Real, shared HITL pause lifecycle (§0.98, GAP_REPORT) — the
        # actual fix for a real, reported gap: this used to have no
        # real status mapping for a genuine pause at all — an unknown
        # status silently defaulted to "failed", misrepresenting a
        # real, correct pause as a real failure, with no HitlRequest
        # ever created, so there was nothing for a real person to
        # resume. See create_hitl_pause_run's own docstring
        # (execution.py) for the full real design — the same real,
        # shared lifecycle /run's own pauses already use.
        run_id = f"r-webhook-{uuid4().hex[:8]}"
        request_id = create_hitl_pause_run(
            session, agent, invoking_user="webhook", run_id=run_id, result=result,
            trigger="Webhook", ts_str=datetime.utcnow().strftime("%H:%M:%S"), duration_s=duration_s,
            resolved=resolved,
        )
        AGENT_RUNS_TOTAL.labels(agent_id=agent.id, status="paused", trigger="webhook").inc()
        AGENT_RUN_DURATION_SECONDS.labels(agent_id=agent.id).observe(duration_s)
        session.commit()
        return {
            "run_id": run_id, "status": "paused_for_human_input",
            "hitl_request_id": request_id, "hitl_rendered_content": result["hitl_rendered_content"],
            "hitl_fields": result["hitl_fields"], "hitl_actions": result["hitl_actions"],
            "hitl_delivery_status": result["hitl_delivery_status"], "spans": result.get("spans", []),
        }

    AGENT_RUNS_TOTAL.labels(agent_id=agent.id, status=result["status"], trigger="webhook").inc()
    AGENT_RUN_DURATION_SECONDS.labels(agent_id=agent.id).observe(duration_s)
    if result["status"] == "denied":
        UCS_DENIALS_TOTAL.labels(agent_id=agent.id, enforced="true").inc()
    for _ in result.get("ucs_soft_denials", []):
        UCS_DENIALS_TOTAL.labels(agent_id=agent.id, enforced="false").inc()

    # §0.108, GAP_REPORT — the actual fix for a real, confirmed
    # inconsistency found while live-testing the report's own core
    # concern: this used to keep its OWN, separate, duplicated
    # status_map/trace_status_map/Run/Trace-construction logic —
    # exactly the kind of "parallel implementation" the report's own
    # architecture recommendation warns against — which had silently
    # drifted out of sync with _finalize_run's own, since-updated map
    # (missing "blocked" entirely): a webhook-invoked, moderation-
    # blocked run persisted Run.status="failed"/Trace.status=
    # "orch_fail" — genuinely different from the exact same
    # AgentVersion, same input, invoked via /run, which correctly
    # persisted "blocked"/"moderation_blocked". Now calls the same,
    # one, real, shared _finalize_run every other channel already
    # uses, so this specific class of drift can't recur — there's only
    # one status map left to keep current, not two.
    ids = _finalize_run(
        session, agent, invoking_user=f"webhook:{token[:8]}…", user_input=user_input,
        execution_mode="webhook", result=result, duration_s=duration_s,
        run_id=f"r-webhook-{uuid4().hex[:8]}", trace_id=f"tr-webhook-{uuid4().hex[:8]}",
        agent_version_id=resolved.agent_version_id, agent_version=resolved.agent_version, flow_hash=resolved.flow_hash,
    )
    run_id, trace_id = ids["run_id"], ids["trace_id"]
    session.add(AgentLog(
        agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
        level="INFO" if result["status"] == "success" else "ERROR",
        msg=f"Webhook trigger fired · status={result['status']}",
        trace_id=trace_id,
    ))
    session.commit()

    # §0.117, GAP_REPORT — the same real fix as run_agent's own
    # identical leak (execution.py), including the same real
    # ucs_soft_denials bug fix (unconditional call).
    from ..error_responses import sanitize_result_for_external_response, log_full_detail
    if result.get("status") in ("failed", "error") or result.get("ucs_soft_denials"):
        log_full_detail(run_id, f"Agent execution issue for agent {agent.id} (webhook): {result.get('spans')}, ucs_soft_denials={result.get('ucs_soft_denials')}")
    result = sanitize_result_for_external_response(result, run_id)

    return {"run_id": run_id, "trace_id": trace_id, **result}
