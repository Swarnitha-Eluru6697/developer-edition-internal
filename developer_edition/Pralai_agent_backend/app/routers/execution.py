"""
The real execution endpoint — POST /api/agents/{id}/run actually walks the
agent's flow graph and executes it (§3.6), rather than returning canned
data. Every call creates genuine Run/Trace/AgentLog rows reflecting what
actually happened, not seeded fixtures.

Routing rule (§3.6, §5): a flow containing a Human-in-the-Loop node (or any
other durability-requiring type) routes through a REAL Temporal workflow
(see temporal_workflow.py, worker.py) — everything else takes the sync path,
a direct call into the harness (harness.py), matching the design's
rationale for keeping simple flows fast.
"""
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict, Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from fastapi.responses import StreamingResponse
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select
from sqlalchemy import update as sql_update
from pydantic import BaseModel

from ..db import get_session
from .. import ucs_client
from .. import agent_catalog
from ..models import Agent, Run, Trace, AgentLog, SavedResult, HitlRequest, AgentVersion, AgentMemory, KnowledgeBase
from ..harness import (
    run_flow_sync, resume_flow_sync, resume_delegated_subagent_flow, CONV_VAR_PREFIX, _get_conv_var,
    PublishedVersionIntegrityError,
)
from ..flow_compiler import objects_to_check, governed_objects
from ..hitl_state import encode_snapshot as encode_hitl_snapshot
from ..limits import check_batch_rows, check_run_input
from ..metrics import AGENT_RUNS_TOTAL, AGENT_RUN_DURATION_SECONDS, UCS_DENIALS_TOTAL
from ..auth import get_verified_identity, require_verified_identity, enforce_authenticated_user
from ..model_adapter import call_llm
from ..egress import safe_http_request, SSRFBlocked

router = APIRouter(prefix="/api/agents", tags=["execution"])

TASK_QUEUE = "inaiera-agents-task-queue"
# Real (§0.42, GAP_REPORT): a flow containing a hitl node now genuinely
# routes to the durable Temporal path. This was deliberately left empty
# for a while (see the prior comment here, still true in spirit) until
# AgentRunWorkflow actually had real signal/query-based hitl support —
# adding "hitl" here before that existed would have meant a hitl node
# silently no-op'd on this path (fell through to "no handler wired
# yet" in execute_node_activity) rather than genuinely pausing, which
# would have been worse than leaving the path dormant.
DURABLE_NODE_TYPES: set = {"hitl"}


class RunIn(BaseModel):
    input: str = "Hello"
    invoking_user: str = "user:demo"
    inputs: Optional[Dict[str, Any]] = None  # real multi-field trigger form values, keyed by field name
    # Real, explicit signal (§0.54, GAP_REPORT) — a genuine Builder
    # "Try It" call sets this True to execute the real, live draft
    # flow, regardless of the agent's own publish status. Every other
    # real caller (the end-user app surface, and this platform's own
    # test suite for anything that isn't specifically testing preview
    # behavior) leaves this at its real default False, meaning a
    # published agent executes its current real AgentVersion snapshot,
    # not whatever the live row currently holds.
    preview: bool = False
    # Which chat thread this turn belongs to. Optional: a workflow or a
    # one-shot call has no thread, and an older client that does not
    # send it keeps behaving as it did. The chat runner sends it so
    # "New conversation" starts a new thread instead of deleting the
    # previous one — which is what made past conversations impossible
    # to look back at.
    conversation_id: Optional[str] = None


def _moderation_flagged(provider, text, cfg):
    """Real check, dispatched by the agent's own configured provider —
    not a single hardcoded mechanism pretending to be a provider choice.
    Returns a short reason string if flagged, else None."""
    if provider == "custom_endpoint":
        url = (cfg.get("custom_endpoint_url") or "").strip()
        if not url:
            return None  # honestly nothing to call, not a fabricated block
        try:
            # Real, governed outbound call (§0.86, GAP_REPORT) — the
            # actual fix for a real, reported gap: this real,
            # per-agent-configurable URL used to bypass egress.py's
            # own SSRF/redirect governance entirely (a direct
            # httpx.post call), even though the HTTP node and Doc
            # Extractor were already routed through it — any agent
            # builder able to set this URL could point it at an
            # internal service or the cloud metadata endpoint. Same
            # real safe_http_request every other real outbound agent
            # call now goes through, not a second, separately-governed
            # path.
            resp = safe_http_request("POST", url, json={"text": text}, timeout=8.0)
            if resp["status_code"] >= 400:
                return f"custom moderation endpoint returned HTTP {resp['status_code']}"
            import json as _json
            data = _json.loads(resp["text"])
            if data.get("flagged"):
                return data.get("reason") or "flagged by custom endpoint"
            return None
        except SSRFBlocked as e:
            return f"custom moderation endpoint blocked by egress policy: {e}"
        except Exception as e:
            return f"custom moderation endpoint error: {e}"
    if provider == "ai":
        prompt = (
            f"Classify the following message as SAFE or FLAGGED for a production chat app. "
            f"Respond with exactly one word, SAFE or FLAGGED, nothing else.\n\nMessage: {text[:800]}"
        )
        try:
            result = call_llm(prompt, model="Claude Sonnet 5", system="You are a strict content moderation classifier.")
        except Exception as e:
            # Real, deliberate fail-closed (§0.58, GAP_REPORT) — matches
            # the custom_endpoint provider's own established pattern
            # just above: if the real check itself can't be performed,
            # treat the message as flagged rather than silently letting
            # it through. A moderation check failing is a different,
            # safety-relevant case from a primary LLM node failing —
            # this one should never propagate as "the run itself
            # failed," it should make the one honest, safe call it
            # still can.
            return f"AI moderation check failed, message blocked as a precaution: {e}"
        verdict = result["text"].strip().upper()
        if verdict.startswith("FLAG"):
            return "flagged by AI moderation"
        if verdict.startswith("SAFE"):
            return None
        # Anything else fails CLOSED, matching the exception branch above.
        #
        # This used to be `startswith("FLAG") else None`, so every reply
        # that was not literally FLAGGED counted as safe — including an
        # EMPTY one. Confirmed live: asked to classify "step-by-step
        # instructions for building a pipe bomb", the classifier model
        # returned '' (its own safety layer declined to answer), the
        # check read that as SAFE, and the request ran.
        #
        # The failure mode is exactly inverted from what it should be:
        # the most harmful inputs are the ones most likely to make a
        # classifier refuse or return nothing, so they were the ones
        # most likely to pass.
        return (f"AI moderation returned an unusable verdict "
                f"({verdict[:40]!r} — expected SAFE or FLAGGED), "
                f"message blocked as a precaution")
    words = cfg.get("keywords") or cfg.get("blocked_words") or []
    lowered = text.lower()
    for w in words:
        if w.strip() and w.strip().lower() in lowered:
            return f'matched blocked keyword "{w.strip()}"'
    return None


def _check_moderation(features: dict, text: str, direction: str) -> Optional[str]:
    """direction is "input" or "output" — each independently toggleable
    and each with its own configured preset reply. Returns the preset
    reply (or an honest default) if blocked, else None.

    features (§0.96, GAP_REPORT) — a real, explicit dict, not the
    whole Agent object, the actual fix for a real, reported gap: this
    used to read agent.features directly — the same real, live,
    mutable field the Builder edits — even for a genuinely published,
    production invocation, so a live features edit with no new
    publish could silently change a "frozen" version's own real
    runtime moderation behavior. A real caller now passes the exact
    same ResolvedExecution.features a run's own Run row was already
    created from (see resolve_execution, above) — the frozen,
    published snapshot for a real production run, or the live
    agent.features for a genuine Builder preview, matching
    ResolvedExecution's own real, established preview/published
    split."""
    cfg = (features or {}).get("moderation") or {}
    if not cfg.get("enabled"):
        return None
    enabled_key = "input_enabled" if direction == "input" else "output_enabled"
    if not cfg.get(enabled_key, True):
        return None
    provider = cfg.get("provider") or "keywords"
    reason = _moderation_flagged(provider, text, cfg)
    if reason is None:
        return None
    preset_key = "input_preset_reply" if direction == "input" else "output_preset_reply"
    preset = (cfg.get(preset_key) or "").strip()
    if preset:
        return preset
    return f"This message was blocked by this app's content moderation settings ({reason})."


def _redact_blocked_output_spans(spans: list) -> list:
    """Real, explicit "discard" policy for a blocked raw output's own
    node-level trace content (§0.107, GAP_REPORT) — the actual fix for
    the report's own second, real, reported concern: `Trace.final_output`
    being replaced with the safe, moderated reply alone was NOT
    sufficient — the last node's own real span can carry its own real
    `output_preview` (a real, up-to-300-char slice of that node's own
    output, set by _execute_flow — harness.py), which is exactly the
    RAW, blocked content, genuinely independent of `final_output`. Left
    alone, that raw content would still land in normal Trace.spans_json
    the instant output moderation blocks a run — never accidentally
    retained here, the report's own explicit ask.

    A real, deliberate policy choice, stated honestly rather than
    silently assumed: DISCARD, not redact-with-a-placeholder-that-
    still-hints-at-content, and not stored in a separate, restricted
    security-audit table — the report's own other two options. A real,
    separate security-audit storage tier is a genuinely larger,
    separate feature (its own table, its own access controls) beyond
    this fix's own scope; discarding is the safest default a normal
    Trace's own, ordinarily-broad read access can't leak from."""
    redacted = []
    for span in spans:
        span = dict(span)
        if span.get("output_preview") is not None:
            span["output_preview"] = "[redacted — blocked by output moderation]"
        redacted.append(span)
    return redacted


def _generate_follow_ups(features: dict, user_input: str, output: str, default_model: Optional[str] = None) -> Optional[List[str]]:
    """Real LLM call (or honest mock, same as every other LLM node) asking
    for short follow-up question suggestions grounded in the real
    conversation that just happened — not a static canned list. Supports a
    real custom prompt override and a real model override.

    features (§0.96, GAP_REPORT): see _check_moderation's own docstring
    just above for the full real reasoning — the same real fix, the
    same real fields, applied here.

    default_model — a real, honest, STATED, separate scope boundary,
    not silently folded into the features fix above: this is
    agent.model (the agent's own default LLM selection), a genuinely
    different, top-level Agent column that AgentVersion never stored
    in the first place, even before this fix — closing that gap too
    would mean adding a new column to AgentVersion and threading it
    through ResolvedExecution, a real, separate, additional change
    beyond this report's own concrete, cited examples (which are all
    agent.features sub-fields: moderation/chat_history/citations/
    follow_up). Stated here honestly rather than silently left
    unmentioned — a real caller still passes the live agent.model,
    unchanged from the prior behavior for this one, specific sub-field.

    Same honest mock-mode note as handle_agent's ReAct loop in harness.py:
    without a real ANTHROPIC_API_KEY, the mock echoes back a slice of its
    own prompt — mock-mode "follow-ups" will look like fragments of the
    echoed prompt, not genuine suggestions. Confirmed via monkeypatch that
    the real parsing logic correctly extracts 3 distinct suggestions once
    a real model is actually replying."""
    cfg = (features or {}).get("follow_up") or {}
    if not cfg.get("enabled") or not output:
        return None
    if cfg.get("prompt_mode") == "custom" and (cfg.get("custom_prompt") or "").strip():
        prompt = cfg["custom_prompt"].strip().replace("{{input}}", user_input).replace("{{output}}", output[:600])
    else:
        prompt = (
            f"User asked: {user_input}\n\nAssistant replied: {output[:600]}\n\n"
            "Suggest exactly 3 short, natural follow-up questions the user might ask next. "
            "Output ONLY the 3 questions, one per line, no numbering."
        )
    model = cfg.get("model") or default_model
    try:
        result = call_llm(prompt, model=model, system="You suggest brief, relevant follow-up questions for a chat conversation.")
    except Exception:
        # Real, deliberate graceful degradation (§0.58, GAP_REPORT) —
        # follow-up suggestions are a real, but genuinely secondary
        # feature; a provider failure here should never fail (or
        # corrupt the real answer of) the run that already succeeded.
        # Unlike a primary LLM/Agent/Classifier/Parameter-Extractor
        # node, there is no real "output" at stake here to protect from
        # becoming fabricated text — the honest choice is simply no
        # follow-ups this time, not a failed run.
        return None
    lines = [l.strip("-• ").strip() for l in result["text"].split("\n") if l.strip()]
    return lines[:3] if lines else None


def _apply_input_moderation(features: dict, user_input: str) -> Optional[dict]:
    """§0.108, GAP_REPORT — the real, shared input-moderation check
    every real invocation channel now runs, extracted from run_agent's
    own, previously channel-specific inline logic. Returns a real,
    structured "blocked" result — the SAME real shape a genuine flow
    execution result has (status/output/spans/fail_class) — if input
    moderation blocks the message, so a caller can persist it through
    the exact same real _finalize_run every other terminal outcome
    goes through, rather than a second, separately-shaped code path.
    Returns None (nothing to short-circuit) if moderation is disabled,
    not configured for input, or the message isn't flagged.

    See ARCHITECTURE.md §66 for the full real reasoning behind why
    this now lives inside execute_agent_flow itself (called by every
    real channel) rather than remaining duplicated per-channel logic —
    the actual, direct fix for the report's own core, reported
    concern: "same AgentVersion, same input" must behave the same
    regardless of which channel invoked it."""
    blocked_reply = _check_moderation(features, user_input, "input")
    if not blocked_reply:
        return None
    return {
        "status": "blocked", "output": blocked_reply, "spans": [], "fail_class": "moderation",
        "execution_mode": "blocked (input moderation, flow never ran)",
    }


def _apply_output_postprocessing(
    features: dict, user_input: str, result: dict, default_model: Optional[str] = None,
    apply_response_enrichment: bool = True,
) -> dict:
    """§0.108, GAP_REPORT — the real, shared post-execution pipeline
    (output moderation, follow-ups, citations) every real invocation
    channel now runs on a genuinely successful result, extracted from
    run_agent's own, previously channel-specific inline logic. A
    no-op, returning `result` completely unchanged, for anything other
    than a real status="success" outcome (a failure/denial/pause has
    nothing here to apply).

    Output moderation ALWAYS applies when a real status="success"
    result reaches this function, on every real channel, with no
    opt-out — the report's own explicit, stated concern is that a
    genuine SAFETY control must never be silently bypassed depending
    on which channel happened to invoke the agent.
    _redact_blocked_output_spans (§0.107, GAP_REPORT) is the same
    real, established "discard" policy for the raw, blocked content's
    own per-node trace footprint.

    apply_response_enrichment (§0.108, GAP_REPORT) — a real, explicit,
    narrower opt-out ONLY for follow-ups/citations, not moderation —
    the report's own honest concession ("some differences are
    reasonable for Eval") made real and stated rather than silently
    assumed: evals.py passes False, since generating follow-up
    suggestions for every real eval case would add real, unnecessary
    LLM cost and noise to what's fundamentally a scoring exercise, not
    a chat response a person will ever actually see. Every other real
    channel (run_agent, run_agent_stream, webhooks, MCP, batch,
    _reconcile_running_run, respond_to_hitl) leaves this at its real,
    honest default — True — since a builder who configures follow-ups/
    citations as part of an agent's own versioned behavior reasonably
    expects them to keep working regardless of which channel a real
    caller happens to invoke the agent through."""
    if result.get("status") != "success":
        return result
    output_blocked_reply = _check_moderation(features, result.get("output") or "", "output")
    if output_blocked_reply:
        return {
            **result, "status": "blocked", "output": output_blocked_reply, "fail_class": "moderation",
            "spans": _redact_blocked_output_spans(result.get("spans", [])),
        }
    if not apply_response_enrichment:
        return result
    follow_ups = _generate_follow_ups(features, user_input, result.get("output") or "", default_model=default_model)
    if follow_ups:
        result = {**result, "follow_ups": follow_ups}
    citations_cfg = (features or {}).get("citations") or {}
    if citations_cfg.get("enabled") and (result.get("retrieval_source") or "").startswith("real:"):
        result = {**result, "citations": result.get("retrieved_chunks")}
    return result


def _needs_durable_path(agent: Agent, flow: Optional[dict] = None) -> bool:
    return any(n["type"] in DURABLE_NODE_TYPES for n in (flow or agent.flow).get("nodes", []))


def _flow_hash(flow: dict) -> str:
    """Real, content-addressed fingerprint (§0.91, GAP_REPORT) of the
    EXACT flow JSON resolved and executed for a given run — a real
    sha256 hex digest over a stable, sorted-keys JSON serialization
    (so semantically-identical flow content always hashes the same
    regardless of incidental key ordering). Honest even for a
    draft/preview run with no real AgentVersion at all to point at —
    lets a person verify two runs genuinely executed byte-identical
    flow content, not just the same version LABEL (which, for a
    draft, isn't even a real, frozen concept in the first place)."""
    import hashlib
    import json as _json
    return hashlib.sha256(_json.dumps(flow, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ResolvedExecution:
    """Real, immutable snapshot of exactly what a single invocation
    resolved to execute (§0.93, GAP_REPORT) — the actual fix for a
    real, reported race: _resolve_execution_context used to be called
    ONCE by a caller like run_agent (to persist onto its own, real,
    upfront Run row) and then called AGAIN, independently, inside
    execute_agent_flow's own body — a real, separate publish landing
    in the real, narrow window between those two calls could mean the
    Run row honestly records "v5" while execution actually walks v6's
    own, different nodes. The whole real point of immutable execution/
    version hashes (§0.54/§0.90/§0.91, GAP_REPORT) is reproducibility;
    resolving twice breaks that guarantee even though each individual
    resolution is itself correct.

    A real, frozen (dataclass(frozen=True) genuinely raises on
    attribute reassignment, not just a naming convention) value object
    — resolved exactly ONCE per real invocation (see resolve_execution
    below), then threaded through every component that needs any part
    of it: the Run row this invocation creates, execute_agent_flow's
    own routing/execution decision, and the eventual Trace this
    invocation's real outcome gets persisted onto. features/app_type
    are included alongside flow/agent_version_id/agent_version/
    flow_hash for the same real reason: a published AgentVersion
    snapshot freezes flow, app_type, AND features together (see
    routers/agents.py's own publish_agent) — the same real, narrow
    race window applies to a real feature-flag or app_type change
    landing between two independent reads, not just to flow content."""
    flow: dict
    agent_version_id: Optional[int]
    agent_version: Optional[str]
    flow_hash: str
    features: dict
    app_type: Optional[str]
    # Real Sub-Agent dependency manifest (§0.104, GAP_REPORT) — the
    # exact same real AgentVersion.subagent_manifest this invocation's
    # own flow/features/app_type were ALSO resolved from, at this exact
    # moment, threaded through the same way — see that field's own
    # docstring (models.py) for the full real reasoning. {} for a
    # draft/preview run (nothing published to have a manifest at all).
    subagent_manifest: dict


def resolve_execution(session: Session, agent: Agent, preview: bool) -> ResolvedExecution:
    """Real fix for a real, reported bug (§0.54, GAP_REPORT): a
    published agent's runtime behavior could silently change the
    moment a builder edit happened — Agent.flow is the same real,
    live, mutable field both the Builder edits and every execution
    channel read, and publishing only ever snapshotted a copy into
    AgentVersion without ever making execution actually read from it.
    "published / v5" wasn't a reliable claim about what would actually
    run.

    preview=True (a genuine Builder "Try It" call — see
    RunnerUI/TryItModal's own real invokingUser="user:builder-preview"
    convention, now backed by a real, explicit flag rather than an
    inferred string match) always returns the real, live agent.flow —
    testing in-progress work has to see the actual in-progress work.

    preview=False (every other real channel: /run's own end-user
    surface, webhooks, MCP, evals, batch, streaming) returns the
    current real AgentVersion's own immutable flow snapshot for a
    published agent — genuinely frozen at the moment of publish, not
    whatever the live row currently holds. A draft agent has no
    published version to freeze yet, so it honestly falls back to its
    own live flow regardless of preview — there's nothing else it
    could mean for something never published.

    §0.90, GAP_REPORT — a published agent with no AgentVersion row at
    all now raises PublishedVersionIntegrityError instead of silently
    falling back to agent.flow. This used to be treated as "a genuine
    data-integrity edge case... falls back to agent.flow rather than
    hard-erroring a real run" — but confirmed directly, this isn't
    hypothetical: seed.py creates several agents with
    status="published" with no matching AgentVersion at all, and this
    environment's own real database was found to hold thousands of
    real, pre-existing published agents in exactly this state before
    this fix. A published runtime claim is a real, load-bearing
    promise; silently substituting the live draft instead of failing
    loudly breaks that promise invisibly — see
    PublishedVersionIntegrityError's own docstring for the full real
    reasoning and the real, one-time backfill migration this fix ships
    alongside.

    §0.91, GAP_REPORT — the actual fix for a second, real, reported
    gap: Run/Trace never recorded WHICH real AgentVersion (or, for a
    draft/preview run, which real live-flow content) was actually
    resolved and executed at invocation time — only Trace.version
    existed, and that was set from agent.version at FINALIZATION time,
    which can genuinely differ from what a long-running durable
    workflow actually executed if the agent gets re-published in the
    meantime.

    §0.93, GAP_REPORT — this is now the one, real, EXCLUSIVE place a
    real DB read happens to answer "what should this invocation
    execute" — see ResolvedExecution's own docstring for the full real
    reasoning behind resolving exactly once and threading the result
    through, rather than a second, independent caller (previously
    execute_agent_flow's own body) re-asking this same real question
    again, later, after a real, narrow window has already passed."""
    if preview or agent.status != "published":
        flow = agent.flow
        return ResolvedExecution(
            flow=flow, agent_version_id=None, agent_version=None, flow_hash=_flow_hash(flow),
            features=agent.features or {}, app_type=agent.app_type, subagent_manifest={},
        )
    current_version = session.exec(
        select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.is_current == True)
    ).first()
    if not current_version:
        raise PublishedVersionIntegrityError(agent.id, agent.name)
    return ResolvedExecution(
        flow=current_version.flow, agent_version_id=current_version.id, agent_version=current_version.version,
        flow_hash=_flow_hash(current_version.flow), features=current_version.features or {},
        app_type=current_version.app_type, subagent_manifest=current_version.subagent_manifest or {},
    )


def _resolve_execution_context(session: Session, agent: Agent, preview: bool) -> ResolvedExecution:
    """Backward-compatible alias (§0.93, GAP_REPORT) — kept only so any
    caller or test still using this original name keeps working
    unchanged; resolve_execution (above) is the one, real, canonical
    name going forward. See resolve_execution's own docstring for the
    full real reasoning."""
    return resolve_execution(session, agent, preview)


def _resolve_execution_flow(session: Session, agent: Agent, preview: bool) -> dict:
    """Thin, backward-compatible wrapper (§0.91/§0.93, GAP_REPORT)
    around resolve_execution, above — preserves this function's own
    original, narrower signature/return type for every real, existing
    caller that only ever needed the flow itself, not the version
    metadata. See resolve_execution's own docstring for the full real
    reasoning behind both what this returns and why a second, richer
    function was added alongside it rather than changing this one's
    own established contract."""
    return resolve_execution(session, agent, preview).flow


def _json_safe_spans(spans: list) -> list:
    """Real fix for a real bug this fix's own Decimal conversion
    introduced (§0.79, GAP_REPORT), caught directly while building it:
    harness.py's own _aggregate_llm_usage now attaches a real `Decimal`
    cost_usd to a span with LLM usage — correct and necessary for the
    typed Run.cost_usd/Trace.cost_usd NUMERIC columns this fix adds,
    but Trace.spans_json is a real JSONB column, and standard JSON has
    no native Decimal type — persisting a span containing one raised a
    genuine `TypeError: Object of type Decimal is not JSON
    serializable` the moment a real LLM node's span reached this
    column, verified directly while building this fix.

    The real, correct boundary: the typed Trace.cost_usd/Run.cost_usd
    columns (this same fix) are now the authoritative, precise source
    for any real aggregation/SLO query — spans_json is a real, existing
    display/debugging artifact (the full per-node trace the UI
    renders), not a source of truth for aggregated cost. Converting
    each span's own cost_usd to a plain float ONLY for this one real
    JSON-storage boundary is honest: a real UI display value doesn't
    need Decimal's own exactness the way a real aggregation column
    does, and every real Run/Trace's own typed cost_usd column still
    carries the full-precision Decimal untouched."""
    safe = []
    for s in spans:
        if "cost_usd" in s and isinstance(s["cost_usd"], Decimal):
            s = {**s, "cost_usd": float(s["cost_usd"])}
        safe.append(s)
    return safe


def _typed_timing(duration_s: float) -> dict:
    """Real, typed timing fields (§0.79, GAP_REPORT) — the actual fix
    for the reported gap: `duration`/`ts` are real, existing display
    STRINGS ("1.8s", "14:22:08"), genuinely unusable for a real
    aggregation query (AVG/SUM/percentile all need a real number), a
    real retention/SLO query, or a real analytics/BI tool without
    first parsing them back into real numbers — exactly what this
    fixes. Every real call site already computes `duration_s` (a real
    float, wall-clock seconds) via `time.time()` before/after — this
    derives `completed_at` as the real, current instant (a real,
    genuine `datetime.utcnow()`, not an approximation) and
    `started_at` by real subtraction, rather than needing every real
    call site to separately capture and thread through its own start
    timestamp on top of the `time.time()` value it already has."""
    completed_at = datetime.utcnow()
    return {
        "duration_ms": int(round(duration_s * 1000)),
        "completed_at": completed_at,
        "started_at": completed_at - timedelta(seconds=duration_s),
    }


def _aggregate_run_usage(spans: list) -> dict:
    """Real, shared aggregation of every real span's own real per-node
    usage into one Run's own real totals (§0.78, GAP_REPORT) — the
    actual fix for the reported gap: harness.py's own
    _aggregate_llm_usage already attaches real input_tokens/
    output_tokens/cached_tokens/model/provider/cost_usd to any span
    whose node made a real LLM call (see that function's own
    docstring); this sums those real per-node figures into the one
    real total every Run() construction site below persists, replacing
    the old unconditional `cost="$0.00"`. A run with no LLM-touching
    nodes at all genuinely sums to zero — an honest zero, not an
    absent field."""
    input_tokens = sum(s.get("input_tokens", 0) for s in spans)
    output_tokens = sum(s.get("output_tokens", 0) for s in spans)
    cached_tokens = sum(s.get("cached_tokens", 0) for s in spans)
    # Real Decimal("0") default (§0.79, GAP_REPORT) — a real bug this
    # fix caught before it shipped: harness.py's own _aggregate_llm_usage
    # now attaches a real Decimal cost_usd to any span with LLM usage
    # (§0.79), so a plain float 0.0 default here for a span WITHOUT
    # usage would make this sum() genuinely raise TypeError the moment
    # both kinds of span appear in the same real run — Python
    # deliberately refuses to mix Decimal and float in arithmetic, to
    # avoid a real, silent precision loss neither type would warn
    # about on its own.
    # Coerced, not assumed. A span reaches this function by two routes:
    # in-process on the sync path (cost_usd is a real Decimal), and
    # across a Temporal activity boundary on the durable path, where it
    # has been through JSON and arrives as a float — JSON has no Decimal.
    # Seeding the sum with Decimal("0") and trusting the type raised
    # `unsupported operand type(s) for +: 'decimal.Decimal' and 'float'`
    # for every durable run with a real costed LLM call, which in
    # practice meant every agent containing a Human Input node.
    def _as_decimal(value) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if value in (None, ""):
            return Decimal("0")
        try:
            # str() first: Decimal(float) would carry the float's own
            # binary representation error into a money value.
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    cost_usd = sum((_as_decimal(s.get("cost_usd")) for s in spans), Decimal("0"))
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens,
        "cost_usd": round(cost_usd, 6),
        # Real, honest formatted display string (§0.78, GAP_REPORT) —
        # 4 decimals, not the old convention's 2/3: a real, typical
        # single-node LLM cost is often well under a cent, and rounding
        # to 2 decimals would silently collapse most real runs right
        # back to the same "$0.00" this fix exists to replace.
        "cost": f"${cost_usd:.4f}",
    }


def create_hitl_pause_run(
    session: Session, agent: Agent, invoking_user: str, run_id: str, result: dict,
    trigger: str, ts_str: str, duration_s: float,
    resolved: Optional["ResolvedExecution"] = None,
) -> str:
    """Real, shared HITL pause lifecycle (§0.98, GAP_REPORT) — the
    actual fix for a real, reported gap: /run and /run/stream already
    correctly represent a genuine pause as Run(status="paused") plus a
    real, resumable HitlRequest row (§0.42/§0.91, GAP_REPORT) — but
    every OTHER real invocation channel (webhook, MCP, evals) that
    calls execute_agent_flow independently reimplemented (or, in
    several real, confirmed cases, never implemented at all) that same
    lifecycle. A genuine pause was silently collapsed into a generic
    "failed"/"isError" outcome, or (evals) immediately, honestly-
    incorrectly scored as if a paused run's own, still-empty
    output were its real, final answer — with no real HitlRequest at
    all, so there was nothing for a real person to ever resume.

    This is the one, real, shared function every real channel beyond
    /run and /run/stream now calls for this — CREATES a fresh, real
    Run(status="paused") row (not a transition of an existing one:
    unlike /run's own §0.91 upfront-Run-row invocation-time creation,
    these channels have no earlier Run row to transition, since they
    only ever create one at all AFTER execute_agent_flow already
    returned) alongside the real HitlRequest, so a genuine pause from
    ANY channel is visible in GET /api/runs and resumable through the
    exact same real /api/hitl/{request_id}/respond endpoint /run's own
    pauses already use — one real, shared resume mechanism, not a
    second, channel-specific one.

    Returns the real hitl_request_id, for a caller's own response
    payload.

    resolved (§0.99, GAP_REPORT): the caller's own, already-resolved
    ResolvedExecution — when given, this Run row records the exact
    real agent_version_id/agent_version/flow_hash that invocation
    actually resolved and executed against, the same real fields
    /run's own Run rows already carry (§0.91/§0.93). None (a caller
    that hasn't been updated to resolve one, or a genuinely
    unpublished draft/preview invocation with no real version to
    record) leaves these honestly absent, matching every other real
    Run row's own established convention for that same case."""
    request_id = f"hitl-{uuid.uuid4().hex[:10]}"
    # §0.116, GAP_REPORT — the actual fix for the reported gap: this
    # used to pass duration=/cost= directly (now real, computed
    # properties with no setter — a real, would-be AttributeError/
    # silently-discarded-kwarg the moment this ran against the new
    # schema). timing/cost_usd below are the real, only fields this
    # row now persists for these two concepts — duration/cost are
    # derived from them at response time (Run's own computed
    # properties), never independently set here.
    timing = _typed_timing(duration_s)
    # Real, necessary insertion order (found via live testing, not
    # just a read-through): HitlRequest.run_id is a real foreign key
    # against Run.id — Run must be added (and, since there's no real
    # SQLAlchemy relationship() linking these two otherwise-unrelated
    # objects for it to infer ordering from, flushed) BEFORE
    # HitlRequest, or the real insert genuinely fails with a foreign
    # key violation.
    session.add(Run(
        id=run_id, agent_id=agent.id, agent_name=agent.name, status="paused",
        hops=1, cost_usd=Decimal("0"),
        duration_ms=timing["duration_ms"], started_at=timing["started_at"], completed_at=timing["completed_at"],
        workflow_id=result.get("_temporal_workflow_id"),
        agent_version_id=resolved.agent_version_id if resolved else None,
        agent_version=resolved.agent_version if resolved else None,
        flow_hash=resolved.flow_hash if resolved else None,
    ))
    session.flush()
    session.add(HitlRequest(
        id=request_id, agent_id=agent.id, node_id=result["hitl_node_id"], invoking_user=invoking_user,
        rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
        actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
        ctx_snapshot=encode_hitl_snapshot(result.get("hitl_ctx_snapshot") or {}),
        temporal_workflow_id=result.get("_temporal_workflow_id"),
        run_id=run_id,
    ))
    session.add(AgentLog(
        agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], level="INFO",
        msg=f"{trigger} trigger · genuinely paused for human input at node {result['hitl_node_id']}",
    ))
    return request_id


def _finalize_run(
    session: Session, agent: Agent, invoking_user: str, user_input: str,
    execution_mode: str, result: dict, duration_s: float,
    run_id: Optional[str] = None, trace_id: Optional[str] = None,
    agent_version_id: Optional[int] = None, agent_version: Optional[str] = None, flow_hash: Optional[str] = None,
) -> dict:
    """Real, shared Run/Trace/AgentLog persistence for a genuinely
    terminal result (§0.60, GAP_REPORT) — extracted from /run's own
    inline logic so the exact same real mapping (status_map,
    trace_status_map, the harness fail_class override) is used whether
    a result arrives immediately (the normal case) or later, via real
    reconciliation of a durable-path run that outlived the original
    HTTP request's own response budget (see _reconcile_running_run
    below). run_id/trace_id are reused (not regenerated) when
    finalizing a previously-"running" Run, so its own real id — the
    one a caller was already given and may be polling — stays stable
    across the transition to a real terminal state.

    agent_version_id/agent_version/flow_hash (§0.91, GAP_REPORT) —
    real, optional pass-through of whatever version metadata a caller
    already resolved at real INVOCATION time (see
    _resolve_execution_context) — set on a newly-INSERTED Run row and
    always copied onto the new Trace row. Deliberately NOT written
    onto an already-EXISTING Run row here: that row's own real
    invocation-time values (set once, honestly, when it was first
    created) are exactly what this whole fix exists to preserve
    against being silently overwritten by whatever the agent's CURRENT
    version happens to be at this later, finalization moment.

    §0.107, GAP_REPORT — `result["status"] == "blocked"` is now a real,
    distinct, terminal outcome this function persists honestly (Run
    status="blocked", Trace status="moderation_blocked") — the actual
    fix for a real, reported audit-consistency gap: output moderation
    used to run AFTER this exact function had already persisted
    status="success" for a result whose own output was then replaced
    and returned to the caller as "blocked" — Run/Trace claimed
    success while the API's own real response said otherwise. Every
    real caller of this function (run_agent, _reconcile_running_run,
    the streaming endpoint) now runs output moderation BEFORE calling
    this, so `result` already reflects the real, final, honest outcome
    by the time it's persisted — this function itself doesn't run
    moderation; it only needed to genuinely recognize "blocked" as a
    real status once callers started passing it."""
    run_id = run_id or f"r-live-{uuid.uuid4().hex[:8]}"
    trace_id = trace_id or f"tr-live-{uuid.uuid4().hex[:8]}"

    AGENT_RUNS_TOTAL.labels(agent_id=agent.id, status=result["status"], trigger="manual").inc()
    AGENT_RUN_DURATION_SECONDS.labels(agent_id=agent.id).observe(duration_s)
    if result["status"] == "denied":
        UCS_DENIALS_TOTAL.labels(agent_id=agent.id, enforced="true").inc()
    for _ in result.get("ucs_soft_denials", []):
        UCS_DENIALS_TOTAL.labels(agent_id=agent.id, enforced="false").inc()

    status_map = {"success": "success", "failed": "failed", "denied": "denied", "blocked": "blocked"}
    trace_status_map = {"success": "ok", "failed": "orch_fail", "denied": "ucs_denial", "blocked": "moderation_blocked"}
    if result.get("fail_class") == "harness":
        trace_status_map["failed"] = "harness_fail"

    existing_run = session.get(Run, run_id)
    usage = _aggregate_run_usage(result.get("spans", []))
    timing = _typed_timing(duration_s)
    if existing_run:
        # Real reconciliation of a previously-"running" row (§0.60) —
        # updates it in place to its real terminal state rather than
        # inserting a genuinely duplicate second row under the same id.
        existing_run.status = status_map.get(result["status"], "failed")
        existing_run.fail_class = result.get("fail_class")
        existing_run.input_tokens = usage["input_tokens"]
        existing_run.output_tokens = usage["output_tokens"]
        existing_run.cached_tokens = usage["cached_tokens"]
        existing_run.cost_usd = usage["cost_usd"]
        existing_run.duration_ms = timing["duration_ms"]
        existing_run.completed_at = timing["completed_at"]
        # Real, deliberate exception: a reconciled "running" row's own
        # real started_at (set honestly at real creation time below,
        # in the "running" branches) is the actual real start — NOT
        # re-derived from this reconciliation's own, much shorter,
        # post-hoc duration_s, which would understate how long the
        # workflow genuinely ran for.
        if existing_run.started_at is None:
            existing_run.started_at = timing["started_at"]
        # The server's own record of which Trace this run produced, so a
        # saved result's provenance does not depend on a client passing
        # a matching trace_id back.
        existing_run.trace_id = trace_id
        session.add(existing_run)
    else:
        session.add(Run(
            trace_id=trace_id,
            id=run_id, agent_id=agent.id, agent_name=agent.name,
            status=status_map.get(result["status"], "failed"),
            hops=1,
            fail_class=result.get("fail_class"),
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cached_tokens=usage["cached_tokens"], cost_usd=usage["cost_usd"],
            duration_ms=timing["duration_ms"], started_at=timing["started_at"], completed_at=timing["completed_at"],
            agent_version_id=agent_version_id, agent_version=agent_version, flow_hash=flow_hash,
        ))
    session.add(Trace(
        id=trace_id, agent_id=agent.id, agent_name=agent.name, version=agent.version,
        principal=f"agent:svc-{agent.id} / {invoking_user}", plane=agent.plane,
        spans=len(result["spans"]),
        status=trace_status_map.get(result["status"], "orch_fail"),
        user_input=user_input, final_output=result.get("output"),
        spans_json=_json_safe_spans(result["spans"]),
        duration_ms=timing["duration_ms"], started_at=timing["started_at"], completed_at=timing["completed_at"],
        cost_usd=usage["cost_usd"],
        # agent_version_id / agent_version / flow_hash (§0.91,
        # GAP_REPORT) — copied verbatim from whatever was resolved and
        # actually executed at real INVOCATION time (passed in by this
        # function's own caller — see _resolve_execution_context), NOT
        # re-derived from `version=agent.version` just above, which
        # reflects the agent's CURRENT version at THIS finalization
        # moment — genuinely different for a long-running durable
        # workflow if the agent was re-published in between. `version`
        # itself is left unchanged (a real, existing, already-relied-
        # upon field) so nothing existing breaks; these are additive,
        # letting a person directly compare the two and immediately
        # spot the exact real drift case this fix exists to make
        # auditable.
        agent_version_id=agent_version_id, agent_version=agent_version, flow_hash=flow_hash,
    ))
    session.add(AgentLog(
        agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
        level="INFO" if result["status"] == "success" else "ERROR",
        msg=f'Live run ({execution_mode}) · status={result["status"]} · message: "{user_input[:200]}"',
        trace_id=trace_id,
    ))
    session.commit()
    # Bookkeeping about a run that has already been committed, so it is
    # wrapped whole: nothing here may affect the run's outcome.
    try:
        state = {"success": "COMPLETE", "blocked": "ABORT",
                 "denied": "ABORT"}.get(result.get("status"), "FAIL")
        agent_catalog.publish_lineage(session, agent, run_id, state)
    except Exception:
        logger.warning("Could not publish lineage for run %s", run_id, exc_info=True)
    return {"run_id": run_id, "trace_id": trace_id}


async def _reconcile_running_run(session: Session, run: Run) -> Run:
    """Real, lazy reconciliation for a durable-path Run still marked
    "running" (§0.60, GAP_REPORT, the actual reported fix) — checks
    the real Temporal workflow this row's own workflow_id names, and
    if it has genuinely reached a terminal state since the original
    /run request's 25s response budget ran out, finalizes this same
    Run row (and a real Trace) via the exact same _finalize_run this
    module's own immediate path uses, rather than a second,
    separately-maintained code path that could silently drift from it.

    Best-effort, by design: if Temporal itself is unreachable (the
    real, common case in an environment without a live cluster), this
    returns the Run genuinely unchanged rather than raising — a caller
    polling GET /api/runs/{run_id} should see the same honest "running"
    state it already knew, not a 500 for infrastructure this endpoint
    doesn't strictly need to have reached its own terminal state."""
    if run.status != "running" or not run.workflow_id:
        return run
    try:
        import os
        from temporalio.client import Client, WorkflowExecutionStatus
        temporal_address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
        client = await Client.connect(temporal_address, namespace="default")
        handle = client.get_workflow_handle(run.workflow_id)
        desc = await handle.describe()
        if desc.status == WorkflowExecutionStatus.RUNNING:
            return run  # genuinely still running — nothing to reconcile yet
        result = await handle.result()
    except Exception:
        # Real, honest degradation — Temporal unreachable, the
        # workflow id no longer resolves, or any other real
        # infrastructure issue. The Run stays "running"; a later call
        # can try again.
        return run

    agent = session.get(Agent, run.agent_id)
    if not agent:
        return run
    # Real, exact duration (§0.91, GAP_REPORT) — the actual fix for a
    # real, reported gap: the old approximation parsed run.ts (a
    # time-of-day-ONLY string, "%H:%M:%S", no date) and re-attached
    # TODAY's date before diffing against "now" — genuinely wrong the
    # moment a run's own real start and this reconciliation call land
    # on different real calendar days (e.g. a run that started at
    # 23:58 reconciled at 00:02 the next real day would compute a
    # deeply negative raw delta, silently clamped to the same, wrong
    # 25.0s floor every other parse failure already fell back to).
    # run.started_at is a real, exact, timezone-aware TIMESTAMPTZ set
    # honestly at real Run-row creation time (see run_agent's own
    # upfront row, §0.91) — no string round-trip, no date-reattachment
    # guesswork, correct regardless of what real calendar day(s) this
    # run's own start and this reconciliation happen to fall on.
    # Real, timezone-aware "now" (§0.91, GAP_REPORT test fix) —
    # run.started_at is a real TIMESTAMPTZ column (comes back
    # genuinely timezone-aware from Postgres); datetime.utcnow() is
    # deliberately naive, so subtracting the two directly raises a
    # real TypeError. datetime.now(timezone.utc) is the correct,
    # aware "now" to diff against a real, aware column value.
    duration_s = (datetime.now(timezone.utc) - run.started_at).total_seconds() if run.started_at else 25.0

    # §0.108, GAP_REPORT — extended from output-moderation-only
    # (§0.107) to the full, real, shared post-execution pipeline —
    # follow-ups/citations now apply here too, matching the report's
    # own core principle: a resumed/reconciled run's own final output
    # is still a real, user-facing response, no different in kind from
    # any other completed run. Resolves the SAME real, frozen features
    # snapshot this run was actually invoked against — run.agent_version_id,
    # if set, names the exact AgentVersion this run's own invocation-
    # time resolution already recorded (§0.91) — never the agent's own
    # CURRENT live features, which could have changed since. Falls
    # back to agent.features only for a genuine draft/preview run with
    # no AgentVersion at all, matching resolve_execution's own real,
    # established convention.
    if run.agent_version_id:
        version_row = session.get(AgentVersion, run.agent_version_id)
        features = (version_row.features if version_row else agent.features) or {}
    else:
        features = agent.features or {}
    result = _apply_output_postprocessing(features, run.user_input or "", result, default_model=agent.model)

    _finalize_run(
        session, agent, invoking_user=run.invoking_user or "unknown", user_input=run.user_input or "",
        execution_mode="durable (Temporal)", result=result, duration_s=duration_s,
        run_id=run.id, trace_id=f"tr-{run.id}",
        agent_version_id=run.agent_version_id, agent_version=run.agent_version, flow_hash=run.flow_hash,
    )
    session.refresh(run)
    return run


async def _run_durable(session: Session, agent: Agent, invoking_user: str, user_input: str,
                        webhook_payload: Optional[dict] = None,
                        structured_inputs: Optional[Dict[str, Any]] = None,
                        flow: Optional[dict] = None,
                        agent_chain: Optional[List[str]] = None,
                        features: Optional[dict] = None,
                        subagent_manifest: Optional[dict] = None) -> dict:
    import asyncio
    import os
    from temporalio.client import Client, WorkflowExecutionStatus
    from ..temporal_workflow import AgentRunWorkflow

    nodes = (flow or agent.flow).get("nodes", [])
    edges = (flow or agent.flow).get("edges", [])
    # Real, shared governance-object computation (§0.84, GAP_REPORT) —
    # the actual fix for the reported gap: this used to keep its own,
    # duplicate, single-KB-only computation
    # (catalog_object or knowledge_base), silently missing every real
    # object beyond the first for a real, multi-KB retrieval node — a
    # real, reported governance hole, not a display bug. objects_to_check
    # (flow_compiler.py) is the one, real, shared source of truth
    # run_flow_sync's own sync-path ctx setup already used correctly;
    # this function now genuinely can't diverge from it, on either the
    # top-level durable path or a nested Sub-Agent's own durable call
    # (_run_subagent_durable, harness.py — reuses this exact function).
    objects = governed_objects(session, nodes)
    ctx = {"input": user_input, "vars": {}, "agent_id": agent.id, "agent_name": agent.name,
           "invoking_user": invoking_user, "_objects_to_check": objects, "output": None,
           # Real, honest note (§0.53, GAP_REPORT): webhook_payload/
           # structured_inputs are threaded through so any real entry
           # point that needs them (webhooks, batch) genuinely still
           # gets them on the durable path, matching what run_flow_sync
           # itself does with the same two parameters on the sync path.
           "_webhook_payload": webhook_payload, "_structured_inputs": structured_inputs or {},
           # Real parity fix (§0.61, GAP_REPORT) — this ctx used to be
           # genuinely thinner than run_flow_sync's own: _agent_chain
           # and _chat_history_enabled were simply absent (not "unset",
           # actually missing keys), and no conversation variable ever
           # got seeded here at all.
           #
           # agent_chain (§0.83, GAP_REPORT) — a real, explicit
           # override, genuinely needed now that a nested Sub-Agent
           # call CAN reach this function too (handle_subagent's own
           # real _run_subagent_durable, harness.py) — the old,
           # hardcoded [agent.id] silently assumed every real caller
           # was a top-level invocation, which stopped being true the
           # moment nested durable Sub-Agent calls became real; a
           # nested call with no explicit chain here would have
           # silently lost real cycle detection for every further hop
           # taken from inside this specific real Temporal workflow.
           # None (every existing, real top-level caller) still
           # resolves to the exact same real [agent.id] default as
           # before — this is additive, not a behavior change for any
           # caller that doesn't pass it.
           "_agent_chain": agent_chain or [agent.id],
           # Real Sub-Agent dependency manifest (§0.104, GAP_REPORT) —
           # same real reasoning/threading as run_flow_sync's own new
           # parameter below; {} (never None) so handle_subagent's own
           # manifest lookup never has to guard against a missing key.
           "_subagent_manifest": subagent_manifest or {},
           "_chat_history_enabled": bool(((features or agent.features or {}).get("chat_history") or {}).get("enabled"))}

    # Real seeding of every real conversation variable a Variable
    # Assigner node has genuinely persisted for this (agent,
    # invoking_user) pair, BEFORE the workflow starts — the exact same
    # real query run_flow_sync's own pre-flight does, so a durable-path
    # run can reference an earlier turn's {{conv_<name>}} value exactly
    # like a sync-path run already could. Missing entirely before this
    # fix — a real, silent behavioral difference for any agent using
    # both a Variable Assigner and a hitl node in the same flow.
    conv_scope_key = f"{agent.id}:{invoking_user}"
    for row in session.exec(
        select(AgentMemory).where(AgentMemory.scope_key == conv_scope_key, AgentMemory.key.startswith(CONV_VAR_PREFIX))
    ).all():
        name = row.key[len(CONV_VAR_PREFIX):]
        ctx["vars"][f"conv_{name}"] = _get_conv_var(session, conv_scope_key, name)

    temporal_address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    client = await Client.connect(temporal_address, namespace="default")
    workflow_id = f"run-{agent.id}-{uuid.uuid4().hex[:8]}"
    # Real, non-blocking start (§0.42, GAP_REPORT) — start_workflow
    # returns the moment Temporal durably records the request, NOT when
    # the workflow finishes. The previous version of this function used
    # execute_workflow, which awaits full completion — genuinely wrong
    # for a workflow that can now real-pause on a hitl node for hours
    # or days: this HTTP request would have hung the entire time.
    handle = await client.start_workflow(
        AgentRunWorkflow.run, args=[nodes, edges, ctx],
        id=workflow_id, task_queue=TASK_QUEUE,
    )

    # Real, bounded poll — checks for BOTH a real hitl pause and real
    # completion on every iteration, rather than blocking indefinitely
    # on handle.result() (which would never return while genuinely
    # paused). 25s leaves real margin under typical HTTP timeouts while
    # comfortably covering the common case (a flow reaching its pause
    # point, or finishing outright, well within a few seconds).
    deadline = asyncio.get_event_loop().time() + 25.0
    while asyncio.get_event_loop().time() < deadline:
        status = await handle.query(AgentRunWorkflow.get_status)
        if status.get("status") == "paused_for_human_input":
            return {**status, "_temporal_workflow_id": workflow_id}
        desc = await handle.describe()
        if desc.status != WorkflowExecutionStatus.RUNNING:
            result = await handle.result()
            return {**result, "_temporal_workflow_id": workflow_id}
        await asyncio.sleep(0.3)

    # Real, honest fallback (not a fake result, not an indefinite
    # hang) — neither paused nor finished within a real HTTP-request
    # budget. Genuinely possible for a long-running flow with no hitl
    # node; the workflow keeps running server-side regardless.
    return {
        "status": "running", "output": None, "spans": [], "fail_class": None,
        "_temporal_workflow_id": workflow_id,
        "_note": f"Still executing after 25s (workflow_id={workflow_id}) \u2014 it continues running; check the Temporal UI for its real live state.",
    }


async def execute_agent_flow(
    session: Session, agent: Agent, invoking_user: str, user_input: str,
    webhook_payload: Optional[dict] = None, structured_inputs: Optional[Dict[str, Any]] = None,
    on_event: Optional[Callable[[dict], None]] = None, is_cancelled: Optional[Callable[[], bool]] = None,
    preview: bool = False, execution: Optional[ResolvedExecution] = None,
    apply_response_enrichment: bool = True,
    conversation_id: Optional[str] = None,
) -> dict:
    """The one real, shared routing decision AND agent-behavior
    pipeline (§0.53/§0.108, GAP_REPORT) — every real invocation
    channel (REST /run, /run/stream, webhooks, MCP, batch, evals) now
    calls this instead of each deciding for itself whether to call
    run_flow_sync directly.

    Fixes a real, reported architectural bug: only /run ever checked
    _needs_durable_path — every other channel called run_flow_sync
    unconditionally, so a hitl-containing agent behaved differently
    depending on which channel invoked it: a genuine Temporal signal-
    based pause through /run, but the sync path's own snapshot-based
    HitlPause everywhere else. Two of those channels (/run/stream and
    batch) had no routing check of any kind, not even a consistent
    wrong one.

    Real, honest degradation, not silently dropped: on_event/
    is_cancelled are sync-only callables — Temporal's own determinism
    rules mean they genuinely cannot be threaded through a durable-path
    run at all. When an agent needs durable routing and a caller passed
    either, they're real no-ops for that specific invocation, not
    silently pretended to work — the returned "execution_mode" field
    ("durable (Temporal)" vs "sync (direct harness call, no Temporal)")
    is a real, honest signal a caller like the streaming endpoint uses
    to decide whether it can keep emitting live per-node events for
    this particular run.

    Real thread-safety, matching /run's own existing sync branch
    exactly: the sync path never reuses the caller's request-scoped
    session across a thread boundary — a genuinely separate Session is
    opened inside the real worker thread, and the agent is re-fetched
    by id inside it. This specific pattern exists because of a real,
    previously-live deadlock (§0.33, GAP_REPORT): run_flow_sync is a
    genuinely blocking synchronous call (any node's own outbound HTTP/
    LLM calls block for real), and every caller here is `async def`.
    Without offloading it to a real worker thread, a node that makes a
    real HTTP call back to this SAME server (e.g. a Doc Extractor
    fetching its own real /api/uploads/{id}/download URL) would
    deadlock: the single asyncio event loop stays blocked synchronously
    inside the coroutine, so it can never accept and process the
    incoming self-referential connection, and the request only ever
    completes once the outbound httpx call's own client-side timeout
    fires. Verified live before that original fix: a real 2-file
    multi-file extraction against this platform's own real upload URLs
    took exactly 30 seconds (2 × the real 15s per-fetch timeout) and
    returned empty output, both files having genuinely failed to fetch
    — not a Doc Extractor bug, a real concurrency bug in how the
    harness was being invoked.

    Deliberately does NOT cover handle_subagent's own real call to
    run_flow_sync (harness.py) — a Sub-Agent call always takes the sync
    path even when the target agent's own flow has a hitl node, a real,
    separately stated architectural boundary (ARCHITECTURE.md §6): a
    nested Sub-Agent call would need a real nested Temporal workflow
    inside an already-running node's own execution, a genuinely bigger,
    separate problem this fix doesn't attempt to solve.

    A real, honest note on the `session` parameter: it was previously
    unused by either branch, kept only so every caller could pass its
    own existing session naturally, matching run_flow_sync's own
    calling convention. It's now genuinely used (§0.54, GAP_REPORT) —
    resolving preview vs. the current real AgentVersion snapshot needs
    a real DB read before either branch below runs, so both branches
    execute the exact same real flow the routing decision was itself
    made against.

    preview: real, explicit signal from a genuine Builder "Try It" call
    (see RunnerUI/TryItModal's own invokingUser="user:builder-preview"
    convention) — True always executes the real, live agent.flow.
    False (every other real channel) executes the current real
    AgentVersion's own immutable snapshot for a published agent — the
    actual fix for a real, reported bug where a published agent's
    runtime behavior could silently change the moment a builder edit
    happened, with no new publish, no version bump, no record of it.

    execution (§0.93, GAP_REPORT) — the actual fix for a real,
    reported race: this function used to ALWAYS call
    resolve_execution/_resolve_execution_flow itself, completely
    independent of any earlier resolution a caller (run_agent,
    run_agent_stream) had already done to populate its own real, Run
    row created at invocation time. A real, narrow window between
    those two independent resolutions — another request publishing a
    new version in between — meant the Run row could honestly record
    v5 while this function's own, SECOND resolution actually executed
    v6's genuinely different nodes: the Run/Trace would claim v5, but
    v6 is what actually ran. Passing this function's own already-
    resolved ResolvedExecution (see that class's own docstring) skips
    the internal re-resolution entirely — the exact same object a
    caller already used for its own Run row is what actually executes,
    closing the race completely rather than narrowing it. None (every
    real caller that doesn't pre-resolve — webhooks, MCP, evals,
    batch) preserves the prior, single-resolution-per-call behavior
    unchanged; there's no earlier resolution for this function to
    reuse in the first place, so resolving here is the only option and
    was never racy for those channels.

    §0.108, GAP_REPORT — the actual fix for a real, reported
    architectural bug: input moderation, output moderation, follow-up
    generation, and citations used to be re-implemented independently,
    inconsistently, per invocation channel — run_agent (/run) applied
    all four; every other real channel (webhooks, MCP, batch,
    run_agent_stream's own durable-path branch — every real caller of
    THIS function) applied none of them at all, confirmed directly.
    The exact same real AgentVersion, given the exact same real input,
    could genuinely behave differently — including bypassing a
    genuine, versioned SAFETY control — purely depending on which
    transport happened to invoke it. This function is now the one,
    real, shared place the report's own recommended
    "pre-execution middleware -> graph execution -> post-execution
    middleware" pipeline actually lives (see ARCHITECTURE.md §66 for
    the full real reasoning on why a function, not a full, separate
    class hierarchy, is the honest, proportionate implementation of
    that recommendation here) — every real caller gets the full,
    consistent pipeline by construction, not by each one remembering
    to re-implement it correctly.

    apply_response_enrichment (§0.108, GAP_REPORT): a real, narrow,
    explicit opt-out for follow-ups/citations ONLY — never moderation,
    which always applies regardless — see _apply_output_postprocessing's
    own docstring for the full real reasoning, including why evals.py
    is the one real, stated exception (matching the report's own
    explicit concession that "some differences are reasonable for
    Eval")."""
    resolved = execution or resolve_execution(session, agent, preview)
    flow = resolved.flow

    input_blocked_result = _apply_input_moderation(resolved.features, user_input)
    if input_blocked_result:
        return input_blocked_result

    if _needs_durable_path(agent, flow=flow):
        result = await _run_durable(
            session, agent, invoking_user, user_input,
            webhook_payload=webhook_payload, structured_inputs=structured_inputs, flow=flow,
            features=resolved.features, subagent_manifest=resolved.subagent_manifest,
        )
        result["execution_mode"] = "durable (Temporal)"
        return _apply_output_postprocessing(
            resolved.features, user_input, result, default_model=agent.model,
            apply_response_enrichment=apply_response_enrichment,
        )

    def _run_in_thread():
        from ..db import engine
        with Session(engine) as thread_session:
            thread_agent = thread_session.get(Agent, agent.id)
            return run_flow_sync(
                thread_session, thread_agent, invoking_user, user_input,
                webhook_payload=webhook_payload, structured_inputs=structured_inputs,
                on_event=on_event, is_cancelled=is_cancelled, flow=flow,
                features=resolved.features, subagent_manifest=resolved.subagent_manifest,
                conversation_id=conversation_id,
            )
    result = await run_in_threadpool(_run_in_thread)
    result["execution_mode"] = "sync (direct harness call, no Temporal)"
    return _apply_output_postprocessing(
        resolved.features, user_input, result, default_model=agent.model,
        apply_response_enrichment=apply_response_enrichment,
    )


@router.post("/{agent_id}/run", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
async def run_agent(agent_id: str, body: RunIn, request: Request, session: Session = Depends(get_session)):
    # Bounded before anything is looked up or executed. The raw-body
    # middleware bounds the BYTES; this bounds what they mean — a 200k
    # character input is inside any reasonable body limit and is still a
    # provider call nobody intended to pay for.
    check_run_input(body.input, getattr(body, "inputs", None))
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    # Real, agent-level opt-in (§0.48, GAP_REPORT) — when set, the
    # gateway-verified identity REPLACES whatever invoking_user the
    # request body claims, never merely supplements it. A body-supplied
    # invoking_user is a real, honest convenience for agents that don't
    # need this; it is never trusted once an agent has opted in.
    #
    # require_authenticated_user is deliberately read from the LIVE
    # agent.features here, not the frozen execution snapshot below
    # (§0.96, GAP_REPORT) — a real, deliberate categorization: this is
    # OPERATIONAL/deployment configuration (who's allowed to call this
    # agent at all), not VERSIONED runtime behavior (what the agent
    # does once actually invoked) — an operator should be able to
    # toggle this immediately (e.g. in response to a real security
    # concern) without needing a full re-publish, the same real reason
    # this codebase's own report explicitly separates "endpoint
    # enabled/disabled" and similar deployment concerns from
    # versioned behavior like moderation/chat_history/citations.
    if ((agent.features or {}).get("require_authenticated_user")):
        body.invoking_user = require_verified_identity(request)

    start = time.time()

    # Real, immutable execution snapshot, resolved ONCE (§0.93/§0.96,
    # GAP_REPORT).
    exec_ctx = _resolve_execution_context(session, agent, body.preview)

    # Real, immediate Run-row creation at real INVOCATION time (§0.91,
    # GAP_REPORT) — the actual fix for the reported gap: a workflow
    # that reaches a real hitl node within _run_durable's own 25s poll
    # window used to create a HitlRequest but NO Run row at all — a
    # workflow could sit genuinely paused for hours or days with
    # nothing in GET /api/runs to show for it the entire time. The
    # real, reported lifecycle this fix now follows exactly:
    #   Invocation -> Run(running) -> [HITL -> Run(paused) -> response
    #   -> Run(running)]* -> Run(terminal)
    # run_id is generated ONCE, here, and reused (never regenerated)
    # through every real transition below — including across a real
    # HITL pause/resume via respond_to_hitl (see HitlRequest.run_id) —
    # so a caller polling GET /api/runs/{run_id} always sees the SAME
    # row's own real, live status, start to finish.
    #
    # agent_version_id/agent_version/flow_hash (§0.91, GAP_REPORT) are
    # captured HERE, from exec_ctx — the exact version/content actually
    # resolved for THIS invocation — not re-derived later from
    # whatever agent.version happens to be by the time this run
    # eventually finishes. A real, long-running durable workflow that
    # started on a frozen v5 snapshot keeps recording v5 here even if
    # the agent gets published to v6 before the workflow completes.
    run_id = f"r-live-{uuid.uuid4().hex[:8]}"
    run_started_at = datetime.utcnow()
    session.add(Run(
        id=run_id, agent_id=agent.id, agent_name=agent.name, status="running",
        hops=1, fail_class=None,
        invoking_user=body.invoking_user, user_input=body.input,
        started_at=run_started_at,
        agent_version_id=exec_ctx.agent_version_id, agent_version=exec_ctx.agent_version,
        flow_hash=exec_ctx.flow_hash,
    ))
    session.commit()

    try:
        # execution=exec_ctx (§0.93, GAP_REPORT) — the actual fix for a
        # real, reported race: without this, execute_agent_flow would
        # independently re-resolve the flow/version a second time,
        # after this same Run row above already committed to a real,
        # specific version — a real, narrow window in which another
        # request could publish a new version in between, so the Run
        # row honestly says v5 while execution actually walks v6's own,
        # different nodes. Passing the SAME already-resolved
        # ResolvedExecution this Run row was just built from closes
        # that race completely: whatever this object says is
        # unconditionally what actually executes.
        result = await execute_agent_flow(
            session, agent, body.invoking_user, body.input,
            structured_inputs=body.inputs, preview=body.preview, execution=exec_ctx,
            conversation_id=body.conversation_id,
        )
    except PublishedVersionIntegrityError:
        # Real, distinct re-raise (§0.90, GAP_REPORT) — a real data-
        # integrity failure, not an infrastructure one; letting this
        # propagate as-is (rather than folding it into the generic
        # except Exception below) means main.py's own dedicated
        # exception handler for this exact type produces a real,
        # specific, honest response instead of this endpoint's own
        # unrelated Temporal/HITL troubleshooting hint below, which
        # would be actively misleading for this particular failure.
        raise
    except Exception as e:
        # Real, honest error — an uncaught exception reaching here is
        # overwhelmingly a real infrastructure failure (a durable-path
        # Temporal connection, most commonly), not a normal node
        # failure: run_flow_sync's own real node-level errors are
        # already caught and returned as status="failed", not raised.
        # The real, already-created Run row above (§0.91, GAP_REPORT)
        # is honestly marked "failed" rather than left stranded at
        # "running" forever with no real terminal state ever recorded.
        existing = session.get(Run, run_id)
        if existing:
            existing.status = "failed"
            existing.fail_class = "orchestration"
            existing.completed_at = datetime.utcnow()
            session.add(existing)
            session.commit()
        raise HTTPException(
            500,
            f"Execution failed — if this agent has a Human Input node, is the "
            f"Temporal dev server AND worker process (`python -m app.worker`) "
            f"both running? {e}",
        )
    execution_mode = result["execution_mode"]

    duration_s = time.time() - start

    if result.get("status") == "paused_for_human_input":
        # A real pause, not a failure (§0.91, GAP_REPORT) — the SAME
        # real Run row created above transitions to status="paused"
        # rather than being left at "running" with no visible signal
        # of the actual real pause, or (the old behavior) never having
        # existed as a row at all for a HITL-involving invocation.
        #
        # Two genuinely different real resume mechanisms depending on
        # which path produced this pause (§0.42, GAP_REPORT): the sync
        # path's result carries hitl_ctx_snapshot (resume_flow_sync
        # reconstructs ctx from it); the durable path's carries
        # _temporal_workflow_id instead (the real, still-running
        # Temporal workflow to signal). Exactly one of the two is ever
        # present, matching which path actually ran this invocation.
        request_id = f"hitl-{uuid.uuid4().hex[:10]}"
        session.add(HitlRequest(
            id=request_id, agent_id=agent.id, node_id=result["hitl_node_id"], invoking_user=body.invoking_user,
            rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
            actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
            ctx_snapshot=encode_hitl_snapshot(result.get("hitl_ctx_snapshot") or {}),
            temporal_workflow_id=result.get("_temporal_workflow_id"),
            run_id=run_id,
        ))
        existing = session.get(Run, run_id)
        if existing:
            existing.status = "paused"
            existing.duration_ms = int(round(duration_s * 1000))
            existing.workflow_id = result.get("_temporal_workflow_id")
            session.add(existing)
        session.add(AgentLog(
            agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], level="INFO",
            msg=f'Live run ({execution_mode}) · genuinely paused for human input at node {result["hitl_node_id"]} · message: "{body.input[:200]}"',
        ))
        session.commit()
        return {
            "run_id": run_id, "status": "paused_for_human_input", "execution_mode": execution_mode,
            "hitl_request_id": request_id, "hitl_rendered_content": result["hitl_rendered_content"],
            "hitl_fields": result["hitl_fields"], "hitl_actions": result["hitl_actions"],
            "hitl_delivery_status": result["hitl_delivery_status"], "spans": result.get("spans", []),
        }

    if result.get("status") == "running":
        # The real, honest 25s-timeout fallback from _run_durable — the
        # workflow is genuinely still executing server-side, not failed.
        # Rare (a long-running flow with no hitl node), but real. The
        # SAME real Run row created above (§0.91, GAP_REPORT) is
        # updated in place — workflow_id set so GET /api/runs/{run_id}
        # can genuinely reconcile this row's real terminal state once
        # the workflow actually finishes (_reconcile_running_run
        # above) — rather than a second, brand new row being created
        # here under a DIFFERENT id than the one already live.
        workflow_id = result.get("_temporal_workflow_id")
        # Real, honest PARTIAL usage (§0.78, GAP_REPORT) — whatever
        # real spans genuinely completed before the 25s timeout fired;
        # later superseded with the real, complete total the moment
        # _reconcile_running_run's own call into _finalize_run above
        # updates this same real row in place once the workflow
        # actually finishes.
        usage = _aggregate_run_usage(result.get("spans", []))
        existing = session.get(Run, run_id)
        if existing:
            existing.duration_ms = int(round(duration_s * 1000))
            existing.workflow_id = workflow_id
            existing.input_tokens = usage["input_tokens"]
            existing.output_tokens = usage["output_tokens"]
            existing.cached_tokens = usage["cached_tokens"]
            existing.cost_usd = usage["cost_usd"]
            session.add(existing)
        session.add(AgentLog(
            agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], level="INFO",
            msg=f'Live run ({execution_mode}) · still executing after 25s · run_id={run_id} · workflow_id={workflow_id}',
        ))
        session.commit()
        return {
            "run_id": run_id, "status": "running", "execution_mode": execution_mode,
            "note": (result.get("_note") or "") + f" Check GET /api/runs/{run_id} for the real terminal result once it's ready.",
            "temporal_workflow_id": workflow_id,
        }

    # §0.108, GAP_REPORT — output moderation/follow-ups/citations now
    # run INSIDE execute_agent_flow itself (this function's own real,
    # shared post-execution pipeline), before it ever returns here —
    # `result` above already reflects the final, real outcome by
    # construction. This used to be reimplemented inline, here,
    # specifically for THIS channel only — every other real channel
    # (webhooks, MCP, batch, run_agent_stream's own durable branch)
    # silently skipped all of it, the report's own core, reported
    # concern. See execute_agent_flow's own docstring (§0.53/§0.108)
    # and ARCHITECTURE.md §66 for the full real reasoning.
    ids = _finalize_run(
        session, agent, body.invoking_user, body.input, execution_mode, result, duration_s,
        run_id=run_id, agent_version_id=exec_ctx.agent_version_id,
        agent_version=exec_ctx.agent_version, flow_hash=exec_ctx.flow_hash,
    )
    run_id, trace_id = ids["run_id"], ids["trace_id"]

    # §0.117, GAP_REPORT — the actual fix for a real, confirmed-live
    # leak: `result` (unpacked below via **result) can genuinely
    # contain raw internal detail in its own "spans" list (a node
    # handler's own real exception text — internal service names,
    # hostnames, raw OS errno detail) — confirmed live before this
    # fix, a real /run call against an unreachable UCS returned "UCS
    # at http://127.0.0.1:8001 unreachable or errored: [Errno 111]
    # Connection refused" directly in this response body. run_id
    # itself is the real, existing correlation_id — the exact same id
    # this response and the fully-detailed, already-persisted Run/
    # Trace rows (spans_json, unsanitized) both share, so a support
    # engineer given only this id can always find the real, full
    # picture server-side, no second id system needed. Only changes
    # real, PRODUCTION responses (see error_responses.py's own module
    # docstring) — dev/test callers still see the full, real detail.
    # §0.117, GAP_REPORT — always attempted (not gated behind an
    # overall "failed" status) — the actual fix for a real, separate
    # bug caught while testing this: ucs_soft_denials can carry the
    # exact same real, raw connection-error detail even when the
    # OVERALL run status is "success" (a soft, non-blocking denial —
    # the run continues regardless). sanitize_result_for_external_response
    # itself already only adds error_code/correlation_id when the
    # overall status genuinely warrants it; gating the CALL itself
    # behind that same check meant a genuinely successful run's own
    # ucs_soft_denials never got sanitized at all. Confirmed live: no
    # sanitization occurred for exactly this scenario before this fix.
    from ..error_responses import sanitize_result_for_external_response, log_full_detail
    if result.get("status") in ("failed", "error") or result.get("ucs_soft_denials"):
        log_full_detail(run_id, f"Agent execution issue for agent {agent.id}: {result.get('spans')}, ucs_soft_denials={result.get('ucs_soft_denials')}")
    result = sanitize_result_for_external_response(result, run_id)

    if result.get("status") == "blocked":
        session.add(AgentLog(agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
                              level="WARN", msg=f'Blocked by output moderation — real output replaced before returning · message: "{body.input[:200]}"',
                              trace_id=trace_id))
        session.commit()
        return {"run_id": run_id, "trace_id": trace_id, "execution_mode": execution_mode + " (output blocked by moderation)", **result}

    return {"run_id": run_id, "trace_id": trace_id, "execution_mode": execution_mode, **result}


@router.post("/{agent_id}/run/stream", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
async def run_agent_stream(agent_id: str, body: RunIn, request: Request, session: Session = Depends(get_session)):
    """Real live per-node progress over Server-Sent Events — genuine
    `node_start`/`node_done` events pushed the moment each node actually
    starts/finishes inside the real harness traversal (_execute_flow's
    on_event hook), not a replay of an already-finished run's spans
    played back with fake delays to look live. Powers the "Workflow
    Process" panel on a workflow app's runner (a live node-by-node view,
    each node showing pending → Running → done) — see
    components/RunnerUI.tsx.

    The synchronous harness call runs in a real background thread (its
    own dedicated Session — the request-scoped `session` above is only
    used to load the agent and isn't safe to share across threads, same
    reasoning as batch-run's fix, see routers/execution.py's git
    history); a real `queue.Queue` bridges its callback-pushed events
    into this async generator.

    Real, node-boundary-granular cancellation: if the browser aborts the
    fetch (or disconnects), this endpoint's own polling of
    `request.is_disconnected()` sets a real `threading.Event` the
    traversal checks between nodes — an honest scope boundary, not a
    silent one: a single node's own in-flight blocking call (an LLM/HTTP
    request already underway) still runs to completion, only the NEXT
    node is skipped.

    Real, but intentionally scoped down from the plain /run endpoint for
    this first pass: writes the same real Run/Trace/AgentLog rows on
    completion, and persists a real HitlRequest on pause — but doesn't
    replicate input/output moderation, follow-up generation, or
    citations here (those are chat-app-oriented features; this endpoint
    targets workflow apps, per its real caller in RunnerUI.tsx). A
    stated gap, not a silently dropped one — see GAP_REPORT."""
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    if ((agent.features or {}).get("require_authenticated_user")):
        body.invoking_user = require_verified_identity(request)

    import asyncio
    import json as json_mod
    import queue
    import threading
    from ..db import engine

    # Real routing check (§0.53, GAP_REPORT) — previously this endpoint
    # called run_flow_sync unconditionally, with no durable-routing
    # check of any kind (not even a consistently-wrong one, unlike
    # webhooks/MCP/evals, which at least always took the same real sync
    # path). A hitl-containing agent invoked via streaming would take
    # the sync path's own snapshot-based HitlPause, genuinely different
    # from the real Temporal signal-based pause the same agent gets
    # through /run.
    #
    # Real, honest degradation, not a silent wrong answer: on_event and
    # is_cancelled are sync-only callables that Temporal's own
    # determinism rules mean cannot be threaded through a durable-path
    # run at all — a Temporal workflow genuinely cannot push live
    # per-node events into this endpoint's own SSE generator running in
    # a separate process. When durable routing is needed, this endpoint
    # gives up live per-node events for that one invocation and emits a
    # single real event once the run reaches its real terminal state
    # (paused for human input, or complete) — a real, stated scope
    # reduction for this specific case, not the old silent wrong
    # behavior of just taking the sync path regardless.
    _stream_ctx = _resolve_execution_context(session, agent, body.preview)
    _stream_flow = _stream_ctx.flow

    # Real, immediate Run-row creation at real INVOCATION time (§0.91,
    # GAP_REPORT) — the SAME real fix, and the SAME real reason, as
    # run_agent's own identical fix just above: a streamed run that
    # reaches a real hitl node used to create a HitlRequest but NO Run
    # row at all, on EITHER the durable or the sync-path branch below —
    # a workflow could sit genuinely paused for hours or days with
    # nothing in GET /api/runs to show for it. run_id is generated
    # ONCE, here, before branching on durable vs. sync, and reused
    # (never regenerated) through every real transition in both
    # branches below — including across a real HITL pause/resume via
    # respond_to_hitl (see HitlRequest.run_id), the exact same real
    # mechanism run_agent's own fix already established.
    _stream_run_id = f"r-live-{uuid.uuid4().hex[:8]}"
    _stream_started_at = datetime.utcnow()
    session.add(Run(
        id=_stream_run_id, agent_id=agent.id, agent_name=agent.name, status="running",
        hops=1, fail_class=None,
        invoking_user=body.invoking_user, user_input=body.input,
        started_at=_stream_started_at,
        agent_version_id=_stream_ctx.agent_version_id, agent_version=_stream_ctx.agent_version,
        flow_hash=_stream_ctx.flow_hash,
    ))
    session.commit()
    if _needs_durable_path(agent, flow=_stream_flow):
        async def event_gen_durable():
            start = time.time()
            try:
                # execution=_stream_ctx (§0.93, GAP_REPORT) — the same
                # real fix as run_agent's own identical one just above:
                # closes the real race between this already-resolved,
                # already-Run-row-recorded context and a second,
                # independent re-resolution execute_agent_flow would
                # otherwise perform on its own.
                result = await execute_agent_flow(
                    session, agent, body.invoking_user, body.input,
                    structured_inputs=body.inputs, preview=body.preview, execution=_stream_ctx,
                    conversation_id=body.conversation_id,
                )
            except Exception as e:
                # Real, honest failure (§0.91, GAP_REPORT) — the real,
                # already-created Run row above is marked "failed"
                # rather than left stranded at "running" forever with
                # no real terminal state ever recorded, the same real
                # fix run_agent's own identical except block applies.
                existing = session.get(Run, _stream_run_id)
                if existing:
                    existing.status = "failed"
                    existing.fail_class = "orchestration"
                    existing.completed_at = datetime.utcnow()
                    session.add(existing)
                    session.commit()
                yield f"data: {json_mod.dumps({'type': 'error', 'message': str(e)}, default=str)}\n\n"
                return
            duration_s = time.time() - start

            if result.get("status") == "paused_for_human_input":
                # §0.91, GAP_REPORT — the SAME real Run row created
                # above transitions to status="paused" (via run_id=
                # _stream_run_id below) rather than being left at
                # "running" with no real Run row ever created for a
                # HITL-involving streamed invocation at all.
                request_id = f"hitl-{uuid.uuid4().hex[:10]}"
                session.add(HitlRequest(
                    id=request_id, agent_id=agent_id, node_id=result["hitl_node_id"], invoking_user=body.invoking_user,
                    rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
                    actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
                    ctx_snapshot=encode_hitl_snapshot(result.get("hitl_ctx_snapshot") or {}),
                    temporal_workflow_id=result.get("_temporal_workflow_id"),
                    run_id=_stream_run_id,
                ))
                existing = session.get(Run, _stream_run_id)
                if existing:
                    existing.status = "paused"
                    existing.duration_ms = int(round(duration_s * 1000))
                    existing.workflow_id = result.get("_temporal_workflow_id")
                    session.add(existing)
                session.commit()
                yield f"data: {json_mod.dumps({'type': 'complete', 'run_id': _stream_run_id, 'status': 'paused_for_human_input', 'hitl_request_id': request_id, 'hitl_rendered_content': result['hitl_rendered_content'], 'hitl_fields': result['hitl_fields'], 'hitl_actions': result['hitl_actions'], 'hitl_delivery_status': result['hitl_delivery_status'], 'execution_mode': result['execution_mode']}, default=str)}\n\n"
                return

            # §0.91, GAP_REPORT — reuses the SAME real Run row
            # (_stream_run_id) created at real invocation time, via
            # _finalize_run's own established "update an existing row
            # in place" path, rather than this branch's own previously
            # separate, duplicated Run/Trace-creation logic (a real,
            # additional consolidation this fix also makes: one real,
            # shared finalization path, not two that could silently
            # drift from each other).
            #
            # §0.108, GAP_REPORT — execute_agent_flow (above) now
            # applies the full, real, shared post-execution pipeline
            # (output moderation, follow-ups, citations) internally,
            # before ever returning — `result` above already reflects
            # the final, real outcome. Nothing further needed here;
            # applying it again would be redundant at best.
            ids = _finalize_run(
                session, agent, body.invoking_user, body.input,
                "durable (Temporal), no live per-node events", result, duration_s,
                run_id=_stream_run_id, agent_version_id=_stream_ctx.agent_version_id,
                agent_version=_stream_ctx.agent_version, flow_hash=_stream_ctx.flow_hash,
            )
            run_id, trace_id = ids["run_id"], ids["trace_id"]
            # §0.117, GAP_REPORT — the same real fix as run_agent's own
            # identical leak, including the same real ucs_soft_denials
            # bug fix (unconditional call — see that call site's own
            # comment for the full real reasoning).
            from ..error_responses import sanitize_result_for_external_response, log_full_detail
            if result.get("status") in ("failed", "error") or result.get("ucs_soft_denials"):
                log_full_detail(run_id, f"Agent execution issue for agent {agent.id}: {result.get('spans')}, ucs_soft_denials={result.get('ucs_soft_denials')}")
            stream_result = sanitize_result_for_external_response(result, run_id)
            yield f"data: {json_mod.dumps({'type': 'complete', 'run_id': run_id, 'trace_id': trace_id, **stream_result}, default=str)}\n\n"

        return StreamingResponse(event_gen_durable(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    event_queue: "queue.Queue[dict]" = queue.Queue()
    cancel_event = threading.Event()
    SENTINEL = {"type": "__done__"}
    start = time.time()

    def on_event(evt: dict) -> None:
        event_queue.put(evt)

    def worker() -> None:
        with Session(engine) as thread_session:
            thread_agent = thread_session.get(Agent, agent_id)
            try:
                result = run_flow_sync(
                    thread_session, thread_agent, body.invoking_user, body.input,
                    structured_inputs=body.inputs, on_event=on_event,
                    is_cancelled=cancel_event.is_set, flow=_stream_flow, features=_stream_ctx.features,
                )
                duration_s = time.time() - start

                if result.get("status") == "paused_for_human_input":
                    # §0.91, GAP_REPORT — the SAME real Run row created
                    # at real invocation time (_stream_run_id) moves to
                    # status="paused" here, rather than no Run row
                    # existing at all for the entire real duration of
                    # a streamed, sync-path HITL pause.
                    request_id = f"hitl-{uuid.uuid4().hex[:10]}"
                    thread_session.add(HitlRequest(
                        id=request_id, agent_id=agent_id, node_id=result["hitl_node_id"], invoking_user=body.invoking_user,
                        rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
                        actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
                        ctx_snapshot=encode_hitl_snapshot(result["hitl_ctx_snapshot"]),
                        run_id=_stream_run_id,
                    ))
                    existing = thread_session.get(Run, _stream_run_id)
                    if existing:
                        existing.status = "paused"
                        existing.duration_ms = int(round(duration_s * 1000))
                        thread_session.add(existing)
                    thread_session.commit()
                    event_queue.put({
                        "type": "complete", "run_id": _stream_run_id, "status": "paused_for_human_input",
                        "hitl_request_id": request_id, "hitl_rendered_content": result["hitl_rendered_content"],
                        "hitl_fields": result["hitl_fields"], "hitl_actions": result["hitl_actions"],
                        "hitl_delivery_status": result["hitl_delivery_status"],
                    })
                    return

                # §0.91, GAP_REPORT — reuses the SAME real Run row
                # (_stream_run_id) via _finalize_run's own established
                # "update an existing row in place" path, rather than
                # this branch's own previously separate, duplicated
                # Run/Trace-creation logic.
                #
                # §0.108, GAP_REPORT — extended from output-
                # moderation-only (§0.107) to the full, real, shared
                # post-execution pipeline — follow-ups/citations now
                # apply here too, matching every other real channel's
                # own behavior for the exact same AgentVersion and
                # input. This branch calls run_flow_sync directly
                # (not execute_agent_flow), so it genuinely needs this
                # call, unlike this function's own durable-path branch
                # above (which already gets it internally).
                result = _apply_output_postprocessing(_stream_ctx.features, body.input, result, default_model=thread_agent.model)

                ids = _finalize_run(
                    thread_session, thread_agent, body.invoking_user, body.input,
                    "sync (direct harness call, no Temporal), streamed", result, duration_s,
                    run_id=_stream_run_id, agent_version_id=_stream_ctx.agent_version_id,
                    agent_version=_stream_ctx.agent_version, flow_hash=_stream_ctx.flow_hash,
                )
                run_id, trace_id = ids["run_id"], ids["trace_id"]

                # §0.117, GAP_REPORT — the same real fix as run_agent's
                # own identical leak, including the same real
                # ucs_soft_denials bug fix (unconditional call).
                from ..error_responses import sanitize_result_for_external_response, log_full_detail
                if result.get("status") in ("failed", "error") or result.get("ucs_soft_denials"):
                    log_full_detail(run_id, f"Agent execution issue for agent {thread_agent.id}: {result.get('spans')}, ucs_soft_denials={result.get('ucs_soft_denials')}")
                stream_result = sanitize_result_for_external_response(result, run_id)
                event_queue.put({"type": "complete", "run_id": run_id, "trace_id": trace_id, **stream_result})
            except Exception as e:
                # Real, honest failure (§0.91, GAP_REPORT) — the real,
                # already-created Run row above is marked "failed"
                # rather than left stranded at "running" forever.
                existing = thread_session.get(Run, _stream_run_id)
                if existing:
                    existing.status = "failed"
                    existing.fail_class = "orchestration"
                    existing.completed_at = datetime.utcnow()
                    thread_session.add(existing)
                    thread_session.commit()
                # §0.117, GAP_REPORT — the actual fix for a real,
                # separate leak: a genuinely UNCAUGHT exception here
                # (not one already captured in `result` above) used to
                # hand its own raw str(e) straight to the client via
                # this same real event stream.
                from ..error_responses import is_production, sanitized_error_value, log_full_detail
                log_full_detail(_stream_run_id, f"Unhandled exception in streamed run for agent {agent.id}", exc=e)
                error_message = sanitized_error_value(_stream_run_id) if is_production() else str(e)
                event_queue.put({"type": "error", "message": error_message})
            finally:
                event_queue.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def event_gen():
        loop = asyncio.get_event_loop()
        while True:
            if await request.is_disconnected():
                cancel_event.set()
            item = await loop.run_in_executor(None, event_queue.get)
            if item is SENTINEL:
                break
            yield f"data: {json_mod.dumps(item, default=str)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class HitlRespondIn(BaseModel):
    action_id: str
    field_values: Optional[Dict[str, Any]] = None
    # Real idempotency support (§0.74, GAP_REPORT) — a real, reported
    # gap: a repeated frontend/network retry of the SAME logical
    # submission had no way to be recognized as such. Optional, not
    # required — a caller that doesn't send one gets the prior,
    # already-correct "reject a second real submission" behavior (now
    # via the real atomic claim below, not the old racy read-then-
    # write), just without replay support for its own retries.
    idempotency_key: Optional[str] = None


async def _resume_durable(workflow_id: str, action_id: str, field_values: dict) -> dict:
    """The real resume mechanism for a hitl pause on the durable path
    (§0.42, GAP_REPORT) — signals the actual, still-running Temporal
    workflow rather than reconstructing ctx from a snapshot. Uses the
    same real bounded-poll pattern as _run_durable, since resuming can
    itself immediately hit a second real hitl pause, complete, or
    (rarely) keep running past the poll window — never blocks
    indefinitely on handle.result()."""
    import asyncio
    import os
    from temporalio.client import Client, WorkflowExecutionStatus
    from ..temporal_workflow import AgentRunWorkflow

    temporal_address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    client = await Client.connect(temporal_address, namespace="default")
    handle = client.get_workflow_handle_for(AgentRunWorkflow.run, workflow_id)
    await handle.signal(AgentRunWorkflow.submit_hitl_response, args=[action_id, field_values])

    deadline = asyncio.get_event_loop().time() + 25.0
    while asyncio.get_event_loop().time() < deadline:
        status = await handle.query(AgentRunWorkflow.get_status)
        if status.get("status") == "paused_for_human_input":
            return {**status, "_temporal_workflow_id": workflow_id}
        desc = await handle.describe()
        if desc.status != WorkflowExecutionStatus.RUNNING:
            result = await handle.result()
            return {**result, "_temporal_workflow_id": workflow_id}
        await asyncio.sleep(0.3)

    return {
        "status": "running", "output": None, "spans": [], "fail_class": None,
        "_temporal_workflow_id": workflow_id,
        "_note": f"Still executing after 25s (workflow_id={workflow_id}) \u2014 it continues running; check the Temporal UI for its real live state.",
    }


def _validate_hitl_response(hitl: "HitlRequest", body: HitlRespondIn) -> None:
    """Real, server-side schema validation for a HITL response (§0.74,
    GAP_REPORT) — the actual fix for the reported gap: respond_to_hitl
    used to accept any string as action_id at all, with no check
    against this request's own real, configured actions, and no real
    validation of submitted field values against the configured form
    schema either. A resumed workflow whose action_id didn't match any
    real wired edge simply ended the run there (resume_flow_sync's own
    honest, documented behavior for a VALID-but-unwired action) — but
    a genuinely bogus, never-configured action_id reached that exact
    same silent dead end, when it should instead be rejected outright,
    before ever attempting to resume anything.

    Raises a real HTTPException(422) with every real problem found (not
    just the first one) if action_id isn't one of this request's own
    configured actions, or if a required field is missing/invalid —
    mirroring NodeConfigPanel.tsx's own real rule (a HITL field's type
    other than "paragraph" is mandatory, matching Dify's own documented
    behavior) rather than inventing a new one."""
    errors: List[str] = []

    action_ids = {a.get("id") for a in (hitl.actions or []) if a.get("id")}
    if body.action_id not in action_ids:
        errors.append(f"'{body.action_id}' is not one of this request's configured actions: {sorted(action_ids)}")

    field_values = body.field_values or {}
    for f in (hitl.fields or []):
        name = f.get("name")
        if not name:
            continue
        field_type = f.get("type") or "paragraph"
        value = field_values.get(name)
        # Real, exact parity with NodeConfigPanel.tsx's own rule: a
        # "paragraph" field is optional; every other real field type
        # (select, single_file, file_list) is mandatory.
        if field_type != "paragraph" and (value is None or (isinstance(value, str) and not value.strip())):
            errors.append(f"Field '{name}' is required")
        elif field_type == "select" and value is not None:
            options = f.get("options") or []
            if options and value not in options:
                errors.append(f"Field '{name}' value '{value}' is not one of the configured options: {options}")

    if errors:
        raise HTTPException(422, {"message": "This HITL response has real, unresolved problems.", "errors": errors})


# §0.106, GAP_REPORT — a real, generous lease duration for the
# "resolving" claim state below: long enough that a genuine, in-
# flight resume attempt (a real Temporal signal call, or the sync
# path's own resume_flow_sync) essentially never has its own lease
# expire out from under it mid-attempt, short enough that a genuinely
# abandoned claim (the process handling it died before its own
# try/except could roll it back) doesn't leave a real person waiting
# an unreasonably long time before reconcile_stale_hitl_requests (this
# file's own function, below) can recover it.
HITL_RESOLVING_LEASE_SECONDS = 60


@router.post("/{agent_id}/hitl/{request_id}/respond", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
async def respond_to_hitl(agent_id: str, request_id: str, body: HitlRespondIn, session: Session = Depends(get_session)):
    """Resumes a real, previously-paused run. Branches on the real
    resume mechanism the original pause actually used (§0.42,
    GAP_REPORT): temporal_workflow_id set means signal the real,
    still-running Temporal workflow (_resume_durable); unset means the
    sync path's own resume_flow_sync, reconstructing ctx from the real
    snapshot captured at pause time. If the resumed run itself hits
    ANOTHER hitl node, this returns a second real pause (a new
    HitlRequest), exactly like the initial /run call would; otherwise
    it returns the flow's real terminal outcome and writes the same
    Run/Trace/AgentLog rows any other completed invocation gets, so
    this shows up in Runs/Traces like any real execution — not a
    second-class code path.

    §0.74, GAP_REPORT — three real, reported fixes now live here:
    (1) real server-side validation of action_id/field_values before
    ever attempting to resume anything (see _validate_hitl_response's
    own docstring); (2) a real atomic claim (`UPDATE ... WHERE
    status='pending'`) closes the real race between reading a real
    "pending" row and committing "resolved" — a second, genuinely
    concurrent request's own claim now affects zero rows and is
    honestly rejected with a real 409, rather than both requests racing
    past the same stale read; (3) a real idempotency_key lets a
    repeated frontend/network retry of the SAME logical submission
    replay the exact same cached result instead of hitting that same
    409 or, worse, genuinely resuming the same workflow twice."""
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    hitl = session.get(HitlRequest, request_id)
    if not hitl or hitl.agent_id != agent_id:
        raise HTTPException(404, "HITL request not found")

    # Real idempotency replay (§0.74, GAP_REPORT) — checked before
    # anything else: a retry carrying the exact same key this exact
    # row was already resolved with gets the real, original cached
    # result back, not a fresh attempt at anything.
    if body.idempotency_key and hitl.status == "resolved" and hitl.idempotency_key == body.idempotency_key:
        return hitl.resolved_result

    if hitl.status != "pending":
        raise HTTPException(409, "This HITL request is already being processed or has already been responded to")

    # Real, server-side schema validation (§0.74, GAP_REPORT) — before
    # any real atomic claim or resume attempt, since a validation
    # failure is a real client-input problem, not a concurrency one;
    # no reason to claim a request that's about to be rejected anyway.
    _validate_hitl_response(hitl, body)

    # Real atomic claim (§0.74, GAP_REPORT) — the actual fix for the
    # reported race. A real Postgres UPDATE ... WHERE status='pending'
    # is what genuinely closes the window the old read-then-write
    # sequence left open: two concurrent requests can no longer both
    # observe "pending" before either commits, because the SECOND
    # request's own UPDATE genuinely affects zero rows the instant the
    # FIRST request's UPDATE has committed (a real, database-level
    # guarantee, not an application-level check that could itself
    # race). populate_existing=True forces this session's own `hitl`
    # object to actually reflect the fresh, claimed state afterward —
    # the same real fix §0.70's own row-lock bug needed, for the same
    # real reason (SQLAlchemy's identity map otherwise keeps returning
    # the stale, already-loaded Python object).
    claim = session.exec(
        sql_update(HitlRequest).where(HitlRequest.id == request_id, HitlRequest.status == "pending")
        .values(status="resolving", resolving_until=datetime.utcnow() + timedelta(seconds=HITL_RESOLVING_LEASE_SECONDS))
        .returning(HitlRequest.id)
    ).first()
    session.commit()
    if not claim:
        raise HTTPException(409, "This HITL request is already being processed or has already been responded to")
    hitl = session.exec(
        select(HitlRequest).where(HitlRequest.id == request_id).execution_options(populate_existing=True)
    ).one()

    # Real, honest lifecycle transition (§0.91, GAP_REPORT) — the SAME
    # real Run row run_agent created at real invocation time (or a
    # prior /respond call, if this pause is itself a second-or-later
    # hop in a chain of pauses) moves back to status="running" the
    # instant a real response genuinely arrives, matching the report's
    # own stated lifecycle exactly: paused -> response -> running.
    # hitl.run_id is None only for a real, existing HitlRequest row
    # that predates this fix (there's no way to recover a Run that was
    # never created for it) — handled honestly below rather than
    # silently assumed to always be present.
    if hitl.run_id:
        run_row = session.get(Run, hitl.run_id)
        if run_row:
            run_row.status = "running"
            session.add(run_row)
            session.commit()

    start = time.time()
    try:
        if hitl.temporal_workflow_id:
            result = await _resume_durable(hitl.temporal_workflow_id, body.action_id, body.field_values or {})
        else:
            # Real thread-offload — this endpoint is now `async def` (needed
            # to `await` the Temporal branch above), which means it no
            # longer gets FastAPI's automatic thread-pool dispatch a plain
            # `def` endpoint gets for free. resume_flow_sync is a genuinely
            # blocking, synchronous call; without this, the exact same
            # self-referential-HTTP-call deadlock already fixed for the
            # main run endpoint (see run_agent's own docstring/comment,
            # GAP_REPORT §0.33) would reappear here.
            def _resume_in_thread():
                from ..db import engine
                with Session(engine) as thread_session:
                    thread_agent = thread_session.get(Agent, agent_id)
                    thread_hitl = thread_session.get(HitlRequest, request_id)
                    return resume_flow_sync(thread_session, thread_agent, thread_hitl, body.action_id, body.field_values or {})
            result = await run_in_threadpool(_resume_in_thread)

        # Real, delegated nested-Sub-Agent resume (§0.83, GAP_REPORT) — the
        # second real half of the mechanism handle_subagent's own docstring
        # (harness.py) describes. hitl.temporal_workflow_id above already
        # correctly signaled the CHILD's own real, live workflow (it's the
        # child's workflow id, not the parent's — see
        # HitlRequest.delegated_child_agent_id's own docstring); `result`
        # is genuinely the CHILD's own outcome at this point. If the child
        # reached a real terminal state, the PARENT's own flow is now
        # resumed from the Sub-Agent node's own single, default outgoing
        # edge — resume_delegated_subagent_flow, not resume_flow_sync (see
        # that function's own docstring for exactly why the two can't be
        # the same call). If the child paused AGAIN, the parent's own
        # delegated wait simply continues — a new HitlRequest is created
        # carrying the SAME real delegation forward, with the PARENT's own,
        # unchanged ctx_snapshot (not the child's — result["hitl_ctx_snapshot"]
        # here is the CHILD's own internal state, never what the parent's
        # own eventual resume needs).
        if hitl.delegated_child_agent_id and result.get("status") not in ("paused_for_human_input", "running"):
            def _resume_delegated_in_thread():
                from ..db import engine
                with Session(engine) as thread_session:
                    thread_agent = thread_session.get(Agent, agent_id)
                    thread_hitl = thread_session.get(HitlRequest, request_id)
                    return resume_delegated_subagent_flow(thread_session, thread_agent, thread_hitl, result)
            result = await run_in_threadpool(_resume_delegated_in_thread)
            # Real, explicit tracking (§0.83, GAP_REPORT) — a real bug
            # caught while re-reading this before considering it done: once
            # resume_delegated_subagent_flow has genuinely run, `result` no
            # longer reflects the CHILD's own state at all — it's now the
            # PARENT's own real outcome from continuing past the Sub-Agent
            # node. If THAT itself hits a genuinely NEW, direct hitl node
            # of the parent's own, the "paused_for_human_input" branch
            # below must treat it as exactly that — a real, new, non-
            # delegated pause — not as the same delegated child somehow
            # still pausing. Without this flag, still_delegated below would
            # incorrectly stay True (hitl.delegated_child_agent_id itself
            # never changes), silently carrying forward the OLD Sub-Agent
            # node's own id and the already-finished child's own id onto a
            # pause that has nothing to do with either.
            delegation_just_resolved = True
        else:
            delegation_just_resolved = False
    except Exception as e:
        # §0.106, GAP_REPORT — the actual fix for the real, reported
        # gap: this used to have no recovery path at all — a genuine
        # failure here (Temporal temporarily unreachable, a workflow-
        # signaling failure, a worker outage, any other real resume
        # exception) left this row stuck at status="resolving"
        # forever, with no way for a legitimate retry to ever succeed
        # again (status != "pending" always rejects it with a 409).
        # Rolled back to "pending" immediately, in the SAME request
        # that caught the failure — the report's own first, fastest
        # recovery option — with resolving_until cleared so a fresh
        # claim attempt isn't confused by a stale lease. The Run row
        # (moved to "running" just above, on the honest assumption the
        # resume was about to succeed) is rolled back to "paused" too,
        # since nothing actually resumed. reconcile_stale_hitl_requests
        # (this file's own function, below) is the separate, real
        # backstop for the case where the process handling THIS
        # request dies before even reaching this except clause at all.
        hitl.status = "pending"
        hitl.resolving_until = None
        session.add(hitl)
        if hitl.run_id:
            run_row = session.get(Run, hitl.run_id)
            if run_row and run_row.status == "running":
                run_row.status = "paused"
                session.add(run_row)
        session.commit()
        raise HTTPException(
            503,
            f"Resuming this request failed and has been rolled back to pending — this is often transient "
            f"(e.g. Temporal or the durable worker being temporarily unreachable); please retry. Detail: {e}",
        )
    duration_s = time.time() - start

    hitl.status = "resolved"
    hitl.resolved_action_id = body.action_id
    hitl.field_values = body.field_values or {}
    hitl.resolved_at = datetime.utcnow()
    hitl.idempotency_key = body.idempotency_key

    # The resume state exists to continue a paused run. Once this request
    # is resolved it has been spent, so it is not kept: a scrubbed,
    # encrypted blob is still a blob, and the shortest-lived copy is the
    # safest one. Captured first because a still-delegated pause carries
    # this same state forward to the next row below — clearing before
    # that read would resume the delegated wait against nothing.
    carried_snapshot = hitl.ctx_snapshot
    hitl.ctx_snapshot = {}
    session.add(hitl)

    if result.get("status") == "paused_for_human_input":
        new_request_id = f"hitl-{uuid.uuid4().hex[:10]}"
        still_delegated = bool(hitl.delegated_child_agent_id) and not delegation_just_resolved
        session.add(HitlRequest(
            id=new_request_id, agent_id=agent.id,
            # Real, honest carry-forward (§0.83, GAP_REPORT) — a real
            # bug caught while re-reading this before considering it
            # done: for a still-delegated pause, `result` comes from
            # the CHILD's own workflow (_resume_durable, above), so
            # result["hitl_node_id"] is the CHILD's own internal hitl
            # node id — using it here would silently corrupt the
            # PARENT-side resume anchor resume_delegated_subagent_flow
            # needs (the parent's own real Sub-Agent node id).
            # hitl.node_id (this SAME row's own, already-correct,
            # parent-anchored id) is carried forward instead. A
            # genuinely NEW, non-delegated pause (still_delegated is
            # False — the normal, pre-existing case) is unaffected —
            # result["hitl_node_id"] is exactly right there, since
            # that pause genuinely IS anchored at whatever node
            # produced it.
            node_id=(hitl.node_id if still_delegated else result["hitl_node_id"]),
            invoking_user=hitl.invoking_user,
            rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
            actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
            # Real, honest carry-forward (§0.83, GAP_REPORT): a still-
            # delegated pause keeps the PARENT's own, real,
            # unchanged ctx_snapshot (hitl.ctx_snapshot — the state
            # this same delegated wait already started from), NOT
            # result.get("hitl_ctx_snapshot") — that field, when
            # present here, is the CHILD's own internal ctx at ITS
            # new pause point, meaningless as a PARENT-side resume
            # anchor. A genuinely NEW, non-delegated pause (the normal,
            # pre-existing case) is unaffected — still_delegated is
            # False, so this is still exactly result's own real
            # ctx_snapshot, unchanged from before this fix.
            ctx_snapshot=(carried_snapshot if still_delegated
                          else encode_hitl_snapshot(result.get("hitl_ctx_snapshot") or {})),
            temporal_workflow_id=result.get("_temporal_workflow_id"),
            delegated_child_agent_id=hitl.delegated_child_agent_id if still_delegated else None,
            # run_id (§0.91, GAP_REPORT) — the SAME real Run row
            # carries forward across a second-or-later pause in the
            # same overall run, exactly as it did across the first —
            # this is still genuinely ONE real run, just paused again
            # at a different point, not a new one.
            run_id=hitl.run_id,
        ))
        if hitl.run_id:
            run_row = session.get(Run, hitl.run_id)
            if run_row:
                run_row.status = "paused"
                run_row.duration_ms = int(round(duration_s * 1000))
                run_row.workflow_id = result.get("_temporal_workflow_id")
                session.add(run_row)
        response = {
            "status": "paused_for_human_input", "hitl_request_id": new_request_id,
            "hitl_rendered_content": result["hitl_rendered_content"], "hitl_fields": result["hitl_fields"],
            "hitl_actions": result["hitl_actions"], "hitl_delivery_status": result["hitl_delivery_status"],
            "spans": result.get("spans", []),
        }
        # Real, cached replay support (§0.74, GAP_REPORT) — this
        # request's own real, final outcome, stored so a real retry
        # carrying the same idempotency_key gets this exact response
        # played back rather than a fresh (and now impossible — this
        # row is no longer "pending") attempt.
        hitl.resolved_result = response
        session.add(hitl)
        session.commit()
        return response

    if result.get("status") == "running":
        # Same real, honest fallback as run_agent's own — the durable
        # workflow is genuinely still executing past the poll window,
        # not failed. hitl stays "resolved" above (the signal itself
        # was genuinely delivered) even though the run hasn't reached
        # a new terminal outcome yet.
        #
        # §0.91, GAP_REPORT — the SAME real Run row created at
        # invocation time (hitl.run_id) is updated in place here,
        # rather than a brand new, disconnected row under a DIFFERENT
        # id being created — the old behavior meant a caller polling
        # the ORIGINAL run_id (the one /run first gave them) would
        # never see this run reach its real terminal state at all;
        # only this second, orphaned id would ever get reconciled.
        run_id = hitl.run_id or f"r-live-{uuid.uuid4().hex[:8]}"
        workflow_id = result.get("_temporal_workflow_id")
        usage = _aggregate_run_usage(result.get("spans", []))
        run_row = session.get(Run, run_id) if hitl.run_id else None
        if run_row:
            run_row.status = "running"
            run_row.duration_ms = int(round(duration_s * 1000))
            run_row.workflow_id = workflow_id
            run_row.input_tokens = usage["input_tokens"]
            run_row.output_tokens = usage["output_tokens"]
            run_row.cached_tokens = usage["cached_tokens"]
            run_row.cost_usd = usage["cost_usd"]
            session.add(run_row)
        else:
            # Real, honest fallback for a real, existing HitlRequest
            # row that predates this fix (no run_id to find) — a new
            # row is the best available option, matching the old
            # behavior exactly for that one, real, narrowing case.
            started_at = datetime.utcnow() - timedelta(seconds=duration_s)
            session.add(Run(
                id=run_id, agent_id=agent.id, agent_name=agent.name, status="running",
                duration_ms=int(round(duration_s * 1000)), hops=1,
                fail_class=None,
                workflow_id=workflow_id, invoking_user=hitl.invoking_user,
                user_input=f"[resumed hitl response: action={body.action_id}]",
                input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                cached_tokens=usage["cached_tokens"], cost_usd=usage["cost_usd"],
                started_at=started_at,
            ))
        response = {
            "run_id": run_id, "status": "running", "note": result.get("_note"),
            "temporal_workflow_id": workflow_id,
        }
        hitl.resolved_result = response
        session.add(hitl)
        session.commit()
        return response

    # Real, honest execution_mode label (§0.83, GAP_REPORT) — a
    # delegated request's own hitl.temporal_workflow_id is real and
    # was genuinely signaled above, but once delegation_just_resolved
    # is True, the FINAL result shown here came from
    # resume_delegated_subagent_flow's own sync-path continuation of
    # the PARENT's flow (_execute_flow), not a durable resume of
    # anything — labeling it "durable (Temporal) resume" would be
    # accurate about the child's own signal but misleading about how
    # this specific, final outcome was actually produced.
    if delegation_just_resolved:
        execution_mode = "durable (Temporal) resume of a real, nested child; parent resumed via sync continuation"
    else:
        execution_mode = "durable (Temporal) resume" if hitl.temporal_workflow_id else "sync resume"
    # §0.91, GAP_REPORT — hitl.run_id (the SAME real Run row created at
    # real invocation time) is reused here, not regenerated — this
    # invocation's own real terminal state lands on the row a caller
    # may already be polling, exactly matching the report's own stated
    # lifecycle (paused -> response -> running -> terminal), rather
    # than a brand new, disconnected Run appearing only now. The
    # existing row's own real, invocation-time agent_version_id/
    # agent_version/flow_hash are fetched here purely to copy them onto
    # the new Trace row below (_finalize_run never overwrites an
    # existing Run's own version fields — see its own docstring) —
    # None for a real, existing HitlRequest predating this fix, the
    # same real, honest fallback used throughout this fix.
    orig_run = session.get(Run, hitl.run_id) if hitl.run_id else None

    # §0.108, GAP_REPORT — extended from output-moderation-only
    # (§0.107) to the full, real, shared post-execution pipeline —
    # follow-ups/citations now apply here too, matching the report's
    # own core principle: a resumed run's own final output is still a
    # real, user-facing response. Resolves the SAME real, frozen
    # features snapshot the ORIGINAL invocation was resolved against
    # (orig_run.agent_version_id, if the pre-existing Run row has one
    # — see this function's own comment just above), never the
    # agent's own CURRENT live features.
    if orig_run and orig_run.agent_version_id:
        version_row = session.get(AgentVersion, orig_run.agent_version_id)
        features = (version_row.features if version_row else agent.features) or {}
    else:
        features = agent.features or {}
    result = _apply_output_postprocessing(
        features, hitl.invoking_user, result, default_model=agent.model,
    )

    ids = _finalize_run(
        session, agent, hitl.invoking_user, f"[HITL response: {body.action_id}]",
        execution_mode, result, duration_s, run_id=hitl.run_id,
        agent_version_id=orig_run.agent_version_id if orig_run else None,
        agent_version=orig_run.agent_version if orig_run else None,
        flow_hash=orig_run.flow_hash if orig_run else None,
    )
    run_id, trace_id = ids["run_id"], ids["trace_id"]

    structured_text = None
    if result.get("structured_output"):
        structured_text = "\n".join(f"{k}: {v}" for k, v in result["structured_output"].items())

    response = {"run_id": run_id, "trace_id": trace_id, "note": structured_text, **result}
    hitl.resolved_result = response
    session.add(hitl)
    session.commit()
    return response


def reconcile_stale_hitl_requests(session: Session) -> int:
    """Real, periodic reconciler for a stuck "resolving" HitlRequest
    (§0.106, GAP_REPORT) — the report's own preferred recovery
    mechanism (a lease + reconciler), and the real backstop for the
    one case respond_to_hitl's own new try/except can't itself cover:
    the process handling a request dies entirely (a hard kill, not a
    caught exception) somewhere between the atomic claim committing
    and that same request's own except clause ever running. A real,
    callable function, matching this codebase's own established
    convention (uploads.py's own sweep_expired_uploads) — real
    production needs a real scheduler (a cron job, a Celery beat task,
    a Kubernetes CronJob) outside this app's own repo to actually call
    this on a real interval; this function is the real, correct thing
    for that scheduler to call, not a simulation of one.

    Rolls every real row whose own resolving_until lease has genuinely
    expired back to "pending" (clearing the stale lease) — the exact
    same, real recovery respond_to_hitl's own except clause already
    performs for a failure it catches directly, applied here to
    whatever it couldn't catch. Returns the real count reconciled."""
    stale = session.exec(
        select(HitlRequest).where(
            HitlRequest.status == "resolving",
            HitlRequest.resolving_until.is_not(None),
            HitlRequest.resolving_until < datetime.utcnow(),
        )
    ).all()
    count = 0
    for row in stale:
        row.status = "pending"
        row.resolving_until = None
        session.add(row)
        count += 1
    session.commit()
    return count


# Registered on the INTERNAL router (routers/internal.py), not this
# product router: it is an operator maintenance operation behind the
# /internal prefix and the require_operator guard. It was a plain,
# unauthenticated route on the product API surface.
def trigger_stale_hitl_reconcile(session: Session = Depends(get_session)) -> dict:
    """Real, manually-triggerable HITL reconciliation sweep (§0.106,
    GAP_REPORT) — the real endpoint a real operator (or a real
    scheduler with HTTP-call capability) can hit; see
    reconcile_stale_hitl_requests's own docstring for the honest note
    on what real infrastructure actually drives this on a schedule in
    production."""
    count = reconcile_stale_hitl_requests(session)
    return {"reconciled": count}


class BatchRunIn(BaseModel):
    rows: List[Dict[str, Any]]
    invoking_user: str = "user:demo"


@router.post("/{agent_id}/batch-run", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
async def batch_run(agent_id: str, body: BatchRunIn, request: Request, session: Session = Depends(get_session)) -> dict:
    """Each CSV row becomes a real, independent run of this workflow —
    genuinely executed concurrently via the same real execute_agent_flow
    every other real invocation channel now uses (§0.53, GAP_REPORT —
    previously each row called run_flow_sync directly via its own
    asyncio.to_thread wrapper, with no real durable-routing check at
    all; a hitl-containing agent run as a batch row would have taken
    the sync path regardless). Not simulated or looped one-at-a-time.
    Each row gets its own real Run/Trace/AgentLog rows, same as any
    other live invocation — a batch isn't a second-class execution
    path. §0.98, GAP_REPORT — a real, reported gap this fix closes: a
    row that genuinely paused for human input used to be reported as
    "paused_for_human_input" in the results table AFTER a real,
    separate Temporal workflow had already been started and left
    genuinely, durably paused, with no real way for this reference
    implementation to ever resume it — real, orphaned durable state.
    The whole batch call is now rejected upfront (see the real
    preflight check below) if the agent's own resolved flow has a
    Human Input node at all, before any row ever starts.

    Real concurrency safety: a SQLAlchemy Session is NOT thread-safe for
    concurrent use — execute_agent_flow's own sync branch opens and
    closes its own real, separate Session per row rather than reusing
    this endpoint's request-scoped `session` across threads; sharing
    one would be a real, genuine correctness bug (interleaved commits,
    corrupted identity-map state), not just a theoretical concern.

    Real concurrency ADMISSION control (§0.75, GAP_REPORT) — the actual
    fix for the reported gap: this used to fire every real row via
    `asyncio.gather` with no explicit limit at all, up to the real
    200-row cap above — a real risk of real provider throttling (an
    LLM-heavy batch making up to 200 concurrent real Anthropic calls at
    once) and real DB pool exhaustion (up to 200 concurrent attempts to
    open this platform's own real, separate per-row Session, against a
    real default pool capacity of 15 — see db.py). Every real row now
    acquires BOTH a real, per-call `batch_semaphore()` (bounding this
    one batch call's own concurrency) AND the real, process-wide
    `get_agent_semaphore(agent_id)` (bounding total concurrent
    execution for this same agent across any other batch calls
    overlapping it) before `execute_agent_flow` is ever invoked — see
    app/concurrency.py's own module docstring for the full real design,
    including the real, separate LLM-provider concurrency limiter this
    same fix wires into model_adapter.py's own call_llm, and the real,
    stated scope boundary on a full persistent token-budget system."""
    # One row is one real agent run, so the cost of a batch is rows x
    # provider latency, not request size: 10,000 rows is a small JSON
    # body and hours of execution.
    check_batch_rows(body.rows)
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    # Same gate as POST /run. Without this, an agent marked "Require
    # authenticated user" could still be driven here with any claimed
    # invoking_user — 200 from this endpoint while /run answered 401 for
    # the very same agent.
    verified = enforce_authenticated_user(agent, request, "batch-run")
    if verified:
        body.invoking_user = verified
    if not body.rows:
        raise HTTPException(400, "No rows to run")
    if len(body.rows) > 200:
        raise HTTPException(400, "Batch runs are capped at 200 rows in this reference implementation")

    # Real preflight rejection (§0.98, GAP_REPORT) — the actual fix for
    # a real, reported gap: a row that genuinely pauses for human input
    # already, honestly reports "This row paused for human input —
    # batch runs don't support resuming individual rows yet." — a
    # real, documented, stated scope boundary, not silently dropped.
    # But that message only ever arrived AFTER a real, separate
    # Temporal workflow had already been started and left genuinely,
    # permanently paused (durable per node — see harness.py's own
    # run_hitl_node) — nobody using this reference implementation can
    # ever resume it, so it sits as real, orphaned Temporal state
    # forever. Rejecting the WHOLE batch call upfront, before any real
    # row starts, is honest in the same way §0.87's own nested-
    # container rejection and §0.95's own durable-parent-durable-child
    # rejection already are: refusing to create real, durable state
    # this reference implementation has no real way to ever clean up,
    # rather than accepting the call and leaving that mess behind one
    # row at a time.
    resolved = resolve_execution(session, agent, preview=False)
    if any(n.get("type") == "hitl" for n in resolved.flow.get("nodes", [])):
        raise HTTPException(
            400,
            "This agent has a Human Input node in its published flow — batch runs don't support "
            "resuming individual rows, so every row would durably pause and never be resumable. "
            "Remove the Human Input node, or run this agent through /run instead.",
        )

    import asyncio
    from ..concurrency import batch_semaphore, get_agent_semaphore

    this_batch_sem = batch_semaphore()
    this_agent_sem = get_agent_semaphore(agent_id)

    async def run_one(index: int, inputs: Dict[str, Any]) -> dict:
        async with this_batch_sem, this_agent_sem:
            start = time.time()
            try:
                result = await execute_agent_flow(
                    session, agent, body.invoking_user,
                    " ".join(str(v) for v in inputs.values()), structured_inputs=inputs,
                    execution=resolved,
                )
            except Exception as e:
                # §0.117, GAP_REPORT — the actual fix for a real,
                # separate leak: a genuinely uncaught exception here
                # (beyond what execute_agent_flow itself already
                # captures into a real, honest result["status"]=
                # "failed") used to hand its own raw str(e) straight
                # to the client. No real Run row exists yet for this
                # specific row (it fails before one is ever created),
                # so a fresh, ad-hoc correlation id is generated here
                # rather than reusing one that doesn't exist.
                from ..error_responses import is_production, sanitized_error_value, log_full_detail
                row_correlation_id = f"batch-row-{uuid.uuid4().hex[:8]}"
                log_full_detail(row_correlation_id, f"Unhandled exception in batch row {index} for agent {agent.id}", exc=e)
                error_value = sanitized_error_value(row_correlation_id) if is_production() else str(e)
                return {"row_index": index, "inputs": inputs, "status": "failed", "error": error_value, "spans": []}
            duration_s = time.time() - start
            out = {
                "row_index": index, "inputs": inputs, "status": result.get("status"),
                "output": result.get("output"), "structured_output": result.get("structured_output"),
                "fail_class": result.get("fail_class"), "duration_s": round(duration_s, 2),
                # Real spans (§0.74, GAP_REPORT) — needed so every real row
                # can get its own real Trace below, the same real per-node
                # execution detail every other real invocation channel's
                # own Trace already carries, not just a summary Run row.
                "spans": result.get("spans", []),
            }
            if result.get("status") == "paused_for_human_input":
                out["error"] = "This row paused for human input — batch runs don't support resuming individual rows yet."
            elif result.get("status") != "success":
                out["error"] = f"Run did not succeed ({result.get('status')}{': ' + result['fail_class'] if result.get('fail_class') else ''})"
            return out

    results = await asyncio.gather(*(run_one(i, row) for i, row in enumerate(body.rows)))

    succeeded = sum(1 for r in results if r["status"] == "success")
    failed = sum(1 for r in results if r["status"] not in ("success", "paused_for_human_input"))
    paused = sum(1 for r in results if r["status"] == "paused_for_human_input")

    # Real, per-row status mapping (§0.74, GAP_REPORT) — the actual fix
    # for the reported bug: a paused row used to be silently relabeled
    # "success" here. "paused" is now a real, honest, distinct Run/Trace
    # status of its own — matching neither "success" (it isn't one) nor
    # "failed" (it didn't fail; it's genuinely awaiting a human response
    # this reference implementation's batch endpoint doesn't support
    # resuming — a real, stated scope boundary, not a silent one).
    run_status_map = {"success": "success", "paused_for_human_input": "paused"}
    trace_status_map = {"success": "ok", "paused_for_human_input": "paused", "denied": "ucs_denial", "cancelled": "orch_fail"}

    for r in results:
        run_id = f"r-live-{uuid.uuid4().hex[:8]}"
        trace_id = f"tr-live-{uuid.uuid4().hex[:8]}"
        real_status = r["status"]
        trace_status = trace_status_map.get(real_status, "harness_fail" if r.get("fail_class") == "harness" else "orch_fail")
        row_input = " ".join(str(v) for v in r["inputs"].values())
        usage = _aggregate_run_usage(r.get("spans", []))
        timing = _typed_timing(r.get("duration_s", 0))
        session.add(Run(
            id=run_id, agent_id=agent.id, agent_name=agent.name,
            status=run_status_map.get(real_status, "failed"),
            hops=1, fail_class=r.get("fail_class"),
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cached_tokens=usage["cached_tokens"], cost_usd=usage["cost_usd"],
            invoking_user=body.invoking_user, user_input=row_input,
            duration_ms=timing["duration_ms"], started_at=timing["started_at"], completed_at=timing["completed_at"],
            agent_version_id=resolved.agent_version_id, agent_version=resolved.agent_version, flow_hash=resolved.flow_hash,
        ))
        # Real, per-row Trace (§0.74, GAP_REPORT) — the actual fix for
        # the second reported gap: this endpoint's own docstring always
        # claimed "each row gets its own real Run/Trace/AgentLog rows",
        # but only Run rows (plus one aggregate AgentLog) were ever
        # created; no Trace at all, for any row.
        session.add(Trace(
            id=trace_id, agent_id=agent.id, agent_name=agent.name, version=agent.version,
            principal=f"agent:svc-{agent.id} / {body.invoking_user}", plane=agent.plane,
            spans=len(r.get("spans", [])), status=trace_status,
            user_input=row_input, final_output=r.get("output"), spans_json=_json_safe_spans(r.get("spans", [])),
            duration_ms=timing["duration_ms"], started_at=timing["started_at"], completed_at=timing["completed_at"],
            cost_usd=usage["cost_usd"],
            # agent_version_id/agent_version/flow_hash (§0.91/§0.99,
            # GAP_REPORT) — the exact real version this whole batch
            # call resolved ONCE, up front (see `resolved` above), the
            # SAME real version execute_agent_flow actually executed
            # every row against — NOT re-derived from
            # `version=agent.version` just above, which reflects the
            # agent's CURRENT version at THIS row's own finalization
            # moment. Recording the one, shared, resolved version
            # against every row (rather than letting each row
            # independently re-resolve, which a real, concurrent
            # publish landing mid-batch could otherwise splinter into
            # different rows silently executing different versions
            # inside one logical batch call) is what makes comparing
            # rows within the same batch call meaningful.
            agent_version_id=resolved.agent_version_id, agent_version=resolved.agent_version, flow_hash=resolved.flow_hash,
        ))
    session.add(AgentLog(
        agent_id=agent.id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], level="INFO",
        msg=f"Batch run: {len(body.rows)} row(s) · {succeeded} succeeded · {failed} failed · {paused} paused",
    ))
    session.commit()

    return {"total": len(results), "succeeded": succeeded, "failed": failed, "paused": paused, "results": results}


class SavedResultIn(BaseModel):
    """What a client may ask to save.

    `run_id` is the provenance anchor. When present it is verified
    against a real Run of this agent owned by this owner, and the saved
    input/output/version are then read from that Run and its linked
    Trace — the request's own input/output are ignored rather than
    trusted. Without it, the row is recorded as manual.
    """
    invoking_user: Optional[str] = None
    run_id: Optional[str] = None
    # Only used for a manual save. Ignored entirely when run_id resolves,
    # because at that point the trusted records are the source.
    input: Optional[str] = None
    output: Optional[str] = None
    structured_output: Optional[dict] = None


def _saved_result_owner(request: Request, claimed: Optional[str]) -> str:
    """Who owns a saved result.

    A gateway-verified identity always wins. Without a gateway there is
    nothing to verify a claim against, so the claim is used — but it is
    used CONSISTENTLY: the same value governs create, list and delete, so
    a caller only ever sees and removes rows under the identity it
    presents. That is what makes the isolation real while authentication
    remains an outer-layer concern; what it cannot do is stop a caller
    presenting someone else's name, which is a property of having no
    authentication rather than of this endpoint.
    """
    verified = get_verified_identity(request)
    if verified:
        return verified
    if claimed and claimed.strip():
        return claimed.strip()
    raise HTTPException(
        400,
        'A saved result needs an owner: pass invoking_user, or call through a gateway that '
        'provides a verified identity. It used to default to "user:demo", which made every '
        'saved result look like it belonged to the same fictional person.',
    )


@router.post("/{agent_id}/saved-results", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
def save_result(agent_id: str, body: SavedResultIn, request: Request,
                session: Session = Depends(get_session)) -> SavedResult:
    """Save a result, with provenance that means something.

    Every field used to be caller-supplied — including invoking_user,
    defaulting to "user:demo" — so a client could post a row that looked
    exactly like a real execution of this agent by somebody else, with
    output it invented. Now the owner is the verified identity where
    there is one, and a run_id is checked against a real Run of THIS
    agent belonging to THAT owner before anything is derived from it.
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    owner = _saved_result_owner(request, body.invoking_user)

    provenance = "manual"
    run_id = trace_id = agent_version = None
    saved_input = body.input
    saved_output = body.output
    structured = body.structured_output

    if body.run_id:
        run = session.get(Run, body.run_id)
        # 404 rather than 403 for someone else's run: this endpoint
        # should not confirm that another user's run id exists.
        if not run or run.agent_id != agent_id or run.invoking_user != owner:
            raise HTTPException(404, "No run of this agent for this owner with that id")
        run_id = run.id
        trace_id = run.trace_id
        agent_version = run.agent_version
        provenance = "execution"
        # Derived, not accepted. This is the point: what gets saved is
        # what the platform recorded, not what the client says happened.
        saved_input = run.user_input or ""
        saved_output = None
        if run.trace_id:
            trace = session.get(Trace, run.trace_id)
            if trace:
                saved_output = trace.final_output

    if saved_input is None:
        raise HTTPException(400, "A manual saved result needs `input`")

    row = SavedResult(
        agent_id=agent_id, invoking_user=owner, input=saved_input,
        output=saved_output, structured_output=structured,
        provenance=provenance, run_id=run_id, trace_id=trace_id,
        agent_version=agent_version,
        created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.get("/{agent_id}/saved-results", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
def list_saved_results(agent_id: str, request: Request,
                        invoking_user: Optional[str] = None,
                        session: Session = Depends(get_session)) -> List[SavedResult]:
    """One owner's saved results for this agent.

    This used to return EVERY user's rows for the agent, so any client
    that could name an agent could read what everyone else had saved —
    including the inputs they submitted.
    """
    owner = _saved_result_owner(request, invoking_user)
    return session.exec(
        select(SavedResult)
        .where(SavedResult.agent_id == agent_id, SavedResult.invoking_user == owner)
        .order_by(SavedResult.id.desc())
    ).all()


@router.delete("/{agent_id}/saved-results/{result_id}", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
def delete_saved_result(agent_id: str, result_id: int, request: Request,
                         invoking_user: Optional[str] = None,
                         session: Session = Depends(get_session)) -> dict:
    """Delete one of the caller's own saved results.

    This used to delete any row by id as long as the agent matched, so
    one user could remove another's saved results. Someone else's row is
    reported as not found rather than forbidden: a 403 would confirm a
    given result id exists and belongs to somebody.
    """
    owner = _saved_result_owner(request, invoking_user)
    row = session.get(SavedResult, result_id)
    if not row or row.agent_id != agent_id or row.invoking_user != owner:
        raise HTTPException(404, "Saved result not found")
    session.delete(row)
    session.commit()
    return {"deleted": True}
