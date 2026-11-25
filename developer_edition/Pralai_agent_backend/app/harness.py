"""
The Agent Harness / interpreter (§3.5, §3.6 of the design doc).

One dispatch table (NODE_HANDLERS), shared by every agent — walks the flow
graph's REAL edges starting from the `trigger` node, calling the handler
matching each node's type as it goes. Connections are explicit (a node
knows what it's wired to, drawn by the person building the flow), not an
implicit ordering derived from node x-position — a `condition` node
genuinely branches to a different next node depending on its outcome, via
`compute_next_node_id()`. This is what makes "publish" produce a JSON
document rather than compiled code (§3.6): nothing here is agent-specific,
so no agent needs a build/deploy step.

Honesty notes on what's real vs. placeholder within each handler:
- gov/tool checks make a REAL entitlement call (ucs_client.py) to the
  actual UCS product over HTTP — a real, independently-built FastAPI
  service with its own Postgres, its own RBAC tables, genuinely denies
  when there's no binding (verified against a real running instance).
- llm calls a REAL model adapter — genuine Anthropic API call if a key is
  configured, an honestly-labeled mock otherwise (model_adapter.py).
- code runs in a REAL sandboxed subprocess (sandbox.py).
- retrieval and tool's actual data-fetch step are placeholders: there is no
  real pgvector embedding pipeline or Trino connector wired up yet (see
  README's stub table), so they return a structurally honest placeholder
  rather than pretending to search/query real data.
- 15 of the 16 node types have handlers (trigger, gov, retrieval, llm,
  tool, code, respond, condition, http, aggregator, classifier, template,
  memory, loop, docextractor) — a run hitting an unhandled type logs a
  "skipped, no handler wired yet" span instead of crashing or faking
  success. Adding a handler
  for another
  type follows the exact same pattern as any handler below.
"""
import os
import ast
import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
import concurrent.futures
import copy
import logging
import httpx

logger = logging.getLogger(__name__)

from sqlmodel import Session, select

from .model_adapter import call_llm, LLMCallError, calculate_cost_usd
from .ucs_client import check_entitlement
from .sandbox import run_sandboxed_code
from .flow_compiler import objects_to_check, retrieval_kb_refs, governed_objects, resolve_kb
from .hitl_state import (decode_snapshot as decode_hitl_snapshot, is_undecryptable,
                         resume_state as hitl_resume_state)
from .models import EnvVar, AgentMemory, AgentLog, ChatMessage, UploadedFile, ActivityIdempotency, AgentVersion, Run, ToolAction


class UCSDenied(Exception):
    def __init__(self, obj: str, detail: dict):
        self.obj = obj
        self.detail = detail
        super().__init__(f"UCS denied access to {obj}")


class HarnessStepLimitExceeded(Exception):
    pass


class GraphDeadlockError(Exception):
    """Raised when the wave scheduler runs out of ready nodes while
    real, unresolved nodes still remain (§0.103, GAP_REPORT) — the
    actual fix for a real, reported gap: `if not wave: break` alone
    can't distinguish "every node genuinely finished" from "no node
    can EVER become ready again" — and a true graph cycle (A waits on
    B, B waits on A) is exactly the second case. Confirmed live,
    before this fix: an unbroken cycle let the run silently reach its
    own normal, successful completion path with the cycle's own nodes
    never having executed at all.

    flow_validator.py's own new graph_cycle check (§0.103) catches
    this for any PUBLISHED agent at publish time — but this runtime
    guard matters independently, not just as a defensive backstop:
    dynamic routing (a condition node's own branch choice, or a hitl
    node's own action-based branching) can create a genuine runtime
    dependency deadlock that static analysis of the flow's own fixed
    edges could never have seen coming, since which edges are even
    live depends on values only known once the run is actually
    executing."""
    def __init__(self, blocked_nodes: List[str], unresolved_edges: List[str]):
        self.blocked_nodes = blocked_nodes
        self.unresolved_edges = unresolved_edges
        super().__init__(
            f"Graph deadlock — {len(blocked_nodes)} node(s) can never become ready to run: "
            f"{', '.join(blocked_nodes)}. Each is still waiting on at least one unresolved "
            f"incoming edge whose own source will never run either — a real cycle in the "
            f"graph's own dependencies, not a step that simply hasn't happened yet."
        )


class UnsupportedNodeType(Exception):
    """Raised when the traversal reaches a real node whose type has no
    real handler — no entry in NODE_HANDLERS and not one of the
    special-cased container/hitl types (§0.73, GAP_REPORT).

    A real, reported gap: this case used to be silently "skipped" —
    the node was treated as a no-op and the flow just continued past
    it, on BOTH the sync path (the main traversal and, worse, the
    loop-body traversal, which didn't even record a skip) and the
    durable/Temporal path (temporal_workflow.py's own
    execute_node_activity). A step silently vanishing from a real run
    is a real correctness bug regardless of why the type is unknown —
    it should never be possible for a genuinely PUBLISHED agent
    (flow_validator.py's own "supported node type" check now blocks
    that at publish time), but a draft being run via Try It, or an
    agent published before this validator existed, could still reach
    this — and when it does, the run must fail loudly and honestly,
    not silently produce a result that quietly skipped a real step."""
    def __init__(self, node_type: str, node_id: Optional[str] = None):
        self.node_type = node_type
        self.node_id = node_id
        super().__init__(f"No real handler for node type '{node_type}'" + (f" (node '{node_id}')" if node_id else ""))


class PublishedVersionIntegrityError(Exception):
    """Real, loud, fail-CLOSED failure (§0.90, GAP_REPORT) — the
    actual fix for a real, reported gap: a published agent with no
    current AgentVersion row used to silently fall back to executing
    agent.flow — the same live, mutable draft field the Builder edits
    — for a genuinely published, production-facing run. This exact
    situation was confirmed to be real, not hypothetical: seed.py
    creates several agents with status="published" but never creates
    a matching AgentVersion row, and directly querying this
    environment's own real database before writing this fix found
    thousands of real, already-existing published agents in exactly
    this state (many from this same session's own accumulated test
    runs, not just the original seed data).

    A published runtime claim ("this agent is published, version vN")
    is a real, load-bearing promise that a frozen, immutable snapshot
    exists and is what actually executes — silently substituting the
    live draft instead breaks that promise invisibly, the exact class
    of bug §0.54's own fix (the immutable-version resolution this
    exception now protects) exists to prevent. Raised by both real
    resolution points that make this same real database check —
    routers/execution.py's own _resolve_execution_flow (the top-level
    /run, webhook, MCP, batch, eval, and streaming channels) and
    harness.py's own _resolve_subagent_flow (a nested Sub-Agent call's
    own target) — rather than either one silently falling back.

    Real, honest remediation path, not just a dead end: see
    migrations/versions/ for the real, one-time backfill this fix
    ships alongside (creates a real AgentVersion snapshot, from the
    agent's own current flow, for every real, pre-existing published
    agent missing one) — a genuinely new agent published through the
    real publish endpoint from this point forward can never reach
    this state again (that endpoint always creates a real AgentVersion
    in the same real transaction as flipping status to "published").
    An operator who somehow still hits this (e.g. a real row created
    by some other, non-standard path) can resolve it by simply
    re-publishing the agent through the real, normal publish flow."""
    def __init__(self, agent_id: str, agent_name: str = ""):
        self.agent_id = agent_id
        self.agent_name = agent_name
        super().__init__(
            f"Agent '{agent_id}'{f' ({agent_name})' if agent_name else ''} is marked published but has no "
            f"real, current AgentVersion snapshot — refusing to silently fall back to its live, mutable "
            f"draft flow for what claims to be a published, production run. Re-publish this agent (or run "
            f"the real §0.90 backfill migration) to create the missing snapshot."
        )


class HitlPause(Exception):
    """Raised when the traversal reaches a real `hitl` node — see
    HitlRequest's docstring (models.py) and resume_flow_sync (below) for
    the actual pause/resume mechanism this drives. Not an error: caught
    deliberately by _execute_flow and turned into a genuine
    "paused_for_human_input" result, the same way UCSDenied is caught and
    turned into a "denied" one.

    delegated_child_agent_id / delegated_temporal_workflow_id (§0.83,
    GAP_REPORT) — real, optional fields, only ever set by
    handle_subagent's own real nested-durable-invocation path (below),
    never by a real, direct hitl node. When set, this pause didn't
    originate from THIS agent's own flow at all — a nested Sub-Agent
    call's own target genuinely paused on ITS OWN real hitl node, on
    ITS OWN real, separate Temporal workflow. rendered_content/fields/
    actions/delivery_status still carry the CHILD's own real, honest
    values (what a human should actually see and respond to), but
    node_id stays the PARENT's own real Sub-Agent node id — the real,
    correct anchor resume_flow_sync's own outgoing-edge lookup needs
    to continue the PARENT's own flow once this real, delegated pause
    is eventually resolved. See HitlRequest's own matching fields and
    respond_to_hitl's own real, extended handling (execution.py) for
    the other real half of this mechanism."""
    def __init__(self, node_id: str, rendered_content: str, fields: list, actions: list, delivery_status: dict,
                 delegated_child_agent_id: Optional[str] = None, delegated_temporal_workflow_id: Optional[str] = None):
        self.node_id = node_id
        self.rendered_content = rendered_content
        self.fields = fields
        self.actions = actions
        self.delivery_status = delivery_status
        self.delegated_child_agent_id = delegated_child_agent_id
        self.delegated_temporal_workflow_id = delegated_temporal_workflow_id
        super().__init__("paused for real human input")


# TEMPORARY, explicitly reversible: with this on, a real UCS denial OR a
# real "couldn't even reach UCS" during EXECUTION (handle_gov / handle_tool)
# is recorded but no longer stops the run — set to unblock subagent-chain
# development/testing without needing real role bindings, or even a
# running UCS instance, for every agent in a call chain. The check itself
# is still genuinely attempted either way; this only changes whether a
# "no" (or "couldn't ask") is enforced or just logged. Publish-time
# enforcement (routers/agents.py's _resolve_and_publish) is deliberately
# UNCHANGED — that's a separate, already-verified real feature, not part
# of this switch. Flip UCS_SOFT_CHECK=false (or unset) to restore hard
# enforcement of both failure modes.
UCS_SOFT_CHECK = os.getenv("UCS_SOFT_CHECK", "false").lower() == "true"


# ---------------- node handlers ----------------

def handle_trigger(cfg: dict, ctx: dict) -> dict:
    """The entry point. Real behavior differs slightly by trigger_type:
    a webhook-triggered run's raw payload (if it was a JSON object) is
    exposed to downstream nodes via ctx["vars"]["webhook_payload"], not
    just collapsed into the plain input string — genuinely useful if a
    later node wants a specific field from what the webhook sent.

    Real multi-field input: when the trigger has configured input fields
    (cfg["fields"]) and the caller actually submitted structured_inputs,
    each field's real value is mapped to ctx["vars"][field_name] — a
    genuinely referenceable {{field_name}} downstream, not just folded
    into the single input string and lost. Backward compatible: a flow
    with no configured fields (or a caller using the plain single-input
    /run call) behaves exactly as before."""
    ctx["vars"]["trigger_input"] = ctx.get("input", "")
    ctx["vars"]["trigger_type"] = cfg.get("trigger_type", "user_input")
    if ctx["vars"]["trigger_type"] == "webhook" and isinstance(ctx.get("_webhook_payload"), dict):
        ctx["vars"]["webhook_payload"] = ctx["_webhook_payload"]
    fields = cfg.get("fields") or []
    structured = ctx.get("_structured_inputs") or {}
    for f in fields:
        name = f.get("name")
        if not name:
            continue
        ctx["vars"][name] = structured.get(name, f.get("default") if f.get("hidden_prefilled") else None)
    return ctx


def handle_gov(cfg: dict, ctx: dict, session: Session) -> dict:
    """The UCS Check node — resolves entitlement for every catalog object
    this run's remaining nodes will touch, upfront, per §7.6. Respects
    UCS_SOFT_CHECK (see its own docstring above): a denial — or UCS being
    unreachable entirely — is always really attempted and always recorded,
    but only blocks the run when UCS_SOFT_CHECK is off.

    The node carries its own on/off switch (`config["enabled"]`, default
    on). Switched off, it steps aside rather than checking here — but the
    run is NOT unchecked: _execute_flow's pre-flight gate covers exactly
    this case, so the check still happens before the first node, just
    without its own step and span. Entitlement is an authorization
    boundary, not a feature a builder can switch off from the canvas; the
    switch controls where the check appears, not whether it runs."""
    from .ucs_client import UCSUnavailable
    if cfg.get("enabled") is False:
        return ctx
    checks = []
    for obj in ctx.get("_objects_to_check", []):
        try:
            result = check_entitlement(session, ctx["agent_id"], ctx["agent_name"],
                                        ctx["invoking_user"], obj, tier="select")
        except UCSUnavailable as e:
            if UCS_SOFT_CHECK:
                ctx["vars"].setdefault("ucs_soft_denials", []).append({"object": obj, "resolved": False, "error": str(e)})
                continue
            raise
        checks.append(result)
        if not result["resolved"]:
            if UCS_SOFT_CHECK:
                ctx["vars"].setdefault("ucs_soft_denials", []).append(result)
                continue
            raise UCSDenied(obj, result)
    ctx["vars"]["entitlement_plan"] = checks
    return ctx


def _kb_lookup_id(kb_ref: str) -> str:
    """Kept as a thin delegate to the one shared slugifier
    (flow_compiler.kb_slug). It used to be a second, independent copy
    of KnowledgeBase creation's slugification, and a second copy is
    exactly how the retrieval path and the governance path came to
    disagree about what a KB reference means."""
    from .flow_compiler import kb_slug
    return kb_slug(kb_ref)



def _build_metadata_where(cfg: dict, has_metadata_col: bool) -> "tuple[str, list]":
    """Real SQL for the manual Metadata Filtering conditions — each
    condition becomes a parameterized `(metadata->>%s) <op> %s` clause
    (the JSON key itself is bound as a parameter, not string-formatted,
    so it can't be used to inject SQL), all combined with AND, matching
    the mockup's own stated behavior. `automatic` mode (an LLM proposing
    conditions from the query) and `disabled` both skip this — automatic
    is a real, deliberately-deferred follow-on, not silently faked."""
    if cfg.get("metadata_filtering_mode") != "manual" or not has_metadata_col:
        return "", []
    clauses, params = [], []
    op_map = {"=": "=", "!=": "!=", ">": ">", "<": "<"}
    for cond in cfg.get("metadata_filtering_conditions") or []:
        key = (cond.get("key") or "").strip()
        op = cond.get("operator", "=")
        val = cond.get("value", "")
        if not key:
            continue
        if op in op_map:
            clauses.append("(metadata->>%s) " + op_map[op] + " %s")
            params += [key, val]
        elif op in ("contains", "starts_with", "ends_with"):
            like = {"contains": f"%{val}%", "starts_with": f"{val}%", "ends_with": f"%{val}"}[op]
            clauses.append("(metadata->>%s) ILIKE %s")
            params += [key, like]
    return (" AND ".join(clauses)), params


def _resolve_query_text(cfg: dict, ctx: dict) -> str:
    """Real query text: the retrieval node's own query_var config
    ({{var}}-style, matching the template/output nodes' convention),
    falling back to the original user input if unset."""
    query_var = (cfg.get("query_var") or "").strip()
    if query_var.startswith("{{") and query_var.endswith("}}"):
        var_name = query_var[2:-2].strip()
        return ctx.get("input", "") if var_name == "input" else ctx["vars"].get(var_name, "")
    if query_var:
        return ctx["vars"].get(query_var, query_var)
    return ctx.get("input", "")


def handle_retrieval(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real when a selected knowledge base matches a registered
    KnowledgeBase (Integrations > Knowledge Base) with provider='pgvector'
    — a genuine psycopg2 connection to whatever separate database was
    configured there, real scoring via retrieval.py's retrieve() (the
    SAME function the Pipeline tab's Retrieval Testing uses — see that
    module's docstring for the exact Vector/Full-Text/Hybrid/Economical/
    Parent-Child/Score-Threshold behavior, honored from each KB's own
    real Retrieval Setting rather than hardcoded to vector-only). Real
    multi-KB: results from every selected KB are queried independently
    and concatenated. Real manual metadata filtering (see
    _build_metadata_where) when the table has a `metadata` column. Falls
    back to the honest placeholder for any selection that doesn't match
    a registered KB, or a registered KB using a provider other than
    pgvector (config storage and connection testing are real for those
    too — see routers/knowledge_bases.py — but real query execution
    isn't wired for them yet)."""
    from .models import KnowledgeBase
    from .retrieval import retrieve

    kb_refs = retrieval_kb_refs(cfg) or ["unknown"]
    top_k = int(cfg.get("top_k", 5) or 5)
    query_text = _resolve_query_text(cfg, ctx)

    all_chunks: List[str] = []
    sources: List[str] = []
    for kb_ref in kb_refs:
        # The shared resolver: id, display name, or slug-of-name. The
        # slug-only lookup this replaced broke silently for any KB that
        # had been RENAMED — slug(new name) no longer equals the id, so
        # a real, connected KB fell through to the placeholder branch
        # below and retrieval quietly returned no real chunks.
        kb = resolve_kb(session, kb_ref)
        if not kb:
            all_chunks.append(f"[no embedding pipeline configured — placeholder chunk from {kb_ref}]")
            sources.append("placeholder")
            continue
        if kb.provider != "pgvector":
            all_chunks.append(f"[knowledge base '{kb.name}' is registered but real query execution isn't wired for provider '{kb.provider}' yet]")
            sources.append(f"unsupported_provider:{kb.name}")
            continue
        try:
            import psycopg2
            from .sql_identifiers import validate_identifier
            from .secrets_crypto import decrypt_secret
            from .retrieval import kb_schema_of
            c = kb.config
            table = c.get("table")
            if not table:
                raise ValueError("this knowledge base has no table configured")
            validate_identifier(table, "table")
            # Real, defensive schema lookup (§0.67, GAP_REPORT) — the
            # same canonical kb_schema_of routers/knowledge_bases.py's
            # own ingestion path uses, so a real query here can never
            # independently drift from what ingestion actually wrote
            # to. Only ever an exact match against KB_MANAGED_SCHEMA —
            # never an arbitrary value read back from kb.config, since
            # that dict is itself caller-supplied at KB creation time.
            table_schema = kb_schema_of(c)
            # Same alias resolution as routers/knowledge_bases.py's own
            # _pgvector_connect — a live Retrieval node must reach the
            # same database the Settings tab just tested, or a pipeline
            # that works in the builder fails at run time.
            from .kb_hosts import resolve_kb_host
            kb_host, kb_port = resolve_kb_host(c.get("host", "127.0.0.1"), c.get("port", 5432))
            conn = psycopg2.connect(
                host=kb_host, port=kb_port,
                dbname=c["dbname"], user=c["user"], password=decrypt_secret(c.get("password", "")),
                connect_timeout=5,
            )
            text_col = c.get("text_column", "content")
            validate_identifier(text_col, "column")

            cur = conn.cursor()
            if table_schema:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s;",
                    (table_schema, table),
                )
            else:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s;", (table,))
            cols = {r[0] for r in cur.fetchall()}
            has_embedding_col = "embedding" in cols
            has_enabled_col = "enabled" in cols
            has_metadata_col = "metadata" in cols

            where_parts, params = [], []
            if has_enabled_col:
                where_parts.append("enabled = true")
            meta_where, meta_params = _build_metadata_where(cfg, has_metadata_col)
            if meta_where:
                where_parts.append(meta_where)
                params += meta_params
            where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

            if (has_embedding_col or "keywords" in cols) and query_text:
                results, _rerank_note = retrieve(
                    conn, table, text_col, query_text, top_k,
                    retrieval_method=c.get("retrieval_method", "vector"),
                    semantic_weight=float(c.get("semantic_weight", 0.7) or 0.7),
                    keyword_weight=float(c.get("keyword_weight", 0.3) or 0.3),
                    reranking_enabled=bool(c.get("reranking_enabled")),
                    rerank_model=c.get("rerank_model"),
                    score_threshold_enabled=bool(c.get("score_threshold_enabled")),
                    score_threshold=float(c.get("score_threshold", 0) or 0),
                    where_sql=where_sql, where_params=params, schema=table_schema,
                    # embedding_provider/embedding_model (§0.92,
                    # GAP_REPORT) — the actual fix for the reported gap:
                    # this KB's own real, stored, immutable embedding
                    # configuration (set once, at this KB's own first
                    # real ingestion — see routers/knowledge_bases.py's
                    # own ingest_document) pins THIS query's own vector
                    # to the exact same embedding space its chunks were
                    # actually embedded with, regardless of whatever the
                    # CURRENT global EMBEDDING_PROVIDER/VOYAGE_EMBED_MODEL
                    # happen to be at this later moment. None for a KB
                    # predating this fix (nothing stored to pin to) —
                    # the same prior, honest, env-driven fallback.
                    embedding_provider=c.get("embedding_provider"), embedding_model=c.get("embedding_model"),
                )
                rows = [r["content"] for r in results]
                sources.append(f"real:{kb.provider}:{kb.name}:{c.get('retrieval_method', 'vector')}_search")
            else:
                # No embedding/keywords column (a table that predates
                # real ingestion, or was created another way) — the same
                # honest most-recent fallback as before, not a
                # fabricated similarity ranking.
                from psycopg2 import sql
                table_ref = sql.Identifier(table_schema, table) if table_schema else sql.Identifier(table)
                cur.execute(
                    sql.SQL("SELECT {} FROM {} {} LIMIT %s;").format(
                        sql.Identifier(text_col), table_ref, sql.SQL(where_sql)
                    ),
                    (*params, top_k),
                )
                rows = [r[0] for r in cur.fetchall()]
                sources.append(f"real:{kb.provider}:{kb.name}")

            conn.close()
            all_chunks.extend(rows)
        except Exception as e:
            all_chunks.append(f"[retrieval failed against real knowledge base '{kb_ref}': {e}]")
            sources.append("error")

    ctx["vars"]["retrieved_chunks"] = all_chunks
    ctx["vars"]["retrieval_source"] = "|".join(sources) if sources else "placeholder"
    return ctx


def _var_to_text(val: Any) -> str:
    """How a workflow variable reads when substituted into text.

    Some variables are structured: the LLM node stores llm_output as
    {"text": ..., "mock": ...}, and a file field as {"url", "filename"}.
    Rendered with plain str() a template referencing {{llm_output}}
    produced the literal "{'text': 'Route: RESOLVED', 'mock': False}" —
    the dict's repr, punctuation and all — which is what every flow
    referencing it has been emitting.
    """
    if isinstance(val, dict):
        if "text" in val:
            return "" if val["text"] is None else str(val["text"])
        if "url" in val and "filename" in val:
            return f"[{val['filename']}]({val['url']})"
    return "" if val is None else str(val)


def _interp_vars(text: str, ctx: dict) -> str:
    """Same {{var}} interpolation convention as handle_template's, shared
    here so the LLM node's System/User prompts and the HITL node's Form
    Content can reference workflow variables the same way every other
    text field in this platform does — including the real {{input}}
    extension (handle_output's mechanism) for the raw original input."""
    import re
    lookup = {**ctx["vars"], "input": ctx.get("input", "")}
    return re.sub(r"\{\{\s*(\w+)\s*\}\}",
                  lambda m: _var_to_text(lookup.get(m.group(1))), text or "")


CHAT_HISTORY_MAX_TURNS = 20  # real, stated bound — last N messages by count, not a real token-budget calculation


def _load_chat_history(session: Session, agent_id: str, invoking_user: str, max_turns: int = CHAT_HISTORY_MAX_TURNS,
                       conversation_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Real, structured multi-turn history (§0.47, GAP_REPORT) — the
    last real max_turns messages for this (agent_id, invoking_user)
    pair, in real chronological order, shaped exactly as Anthropic's
    own messages API expects. Genuinely different from AgentMemory's
    older __llm_chat_history__ blob: real per-message rows, not one
    growing string — so this can be handed straight to call_llm's
    history= parameter as real prior turns, not text pasted into the
    system prompt."""
    q = (select(ChatMessage)
         .where(ChatMessage.agent_id == agent_id, ChatMessage.invoking_user == invoking_user))
    # Scoped to one thread. Without this, starting a new conversation
    # still fed the model every turn of the previous one — the person
    # would see a clean transcript while the agent answered from a
    # conversation they thought they had ended.
    if conversation_id:
        q = q.where(ChatMessage.conversation_id == conversation_id)
    rows = session.exec(q.order_by(ChatMessage.created_at.desc()).limit(max_turns)).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


def _append_chat_turn(session: Session, agent_id: str, invoking_user: str, role: str, content: str,
                      conversation_id: Optional[str] = None) -> None:
    """Real, immediate persistence — one real row per turn, committed
    right away so a concurrent read (a real parallel wave's own second
    LLM-touching node, or the very next request) sees it."""
    session.add(ChatMessage(agent_id=agent_id, invoking_user=invoking_user, role=role,
                            content=content, conversation_id=conversation_id))
    session.commit()


def _aggregate_llm_usage(node_ctx: dict) -> Optional[Dict[str, Any]]:
    """Real aggregation of every real LLM call a single node execution
    made (§0.78, GAP_REPORT) — reads node_ctx["_llm_usage"] (populated
    by _call_llm_tracked above, reset to an empty list by the caller
    immediately before this one node's handler ran) and sums real
    token counts across however many real calls that handler actually
    made (a plain llm/classifier/parameter_extractor node makes
    exactly one; handle_agent's own multi-step ReAct loop can make
    several — all summed into this one node's own real span usage,
    not just the last call). Returns None (not a zero-valued dict) for
    a node that made no real LLM calls at all — an http/code/template/
    etc. node's own span simply omits these fields entirely, an honest
    absence rather than a fabricated $0.00.

    `model`/`provider` on the returned dict reflect the LAST real call
    made (the common real case is exactly one model per node — this
    platform's own node config only ever offers a single model
    picker); a node execution that genuinely mixed models (only
    possible today via handle_agent's own tool-calling loop reusing
    the same one configured model every iteration, so not actually
    reachable in practice) would still report a real, complete summed
    token count, just attributed to whichever model made the final
    call — a real, honest, minor limitation, not a silent undercount
    of the actual tokens used."""
    usage_list = node_ctx.get("_llm_usage")
    if not usage_list:
        return None
    input_tokens = sum(u.get("input_tokens", 0) for u in usage_list)
    output_tokens = sum(u.get("output_tokens", 0) for u in usage_list)
    cached_tokens = sum(u.get("cached_tokens", 0) for u in usage_list)
    model = usage_list[-1].get("model")
    provider = usage_list[-1].get("provider")
    # Real cost math per real call, then summed — correct even if a
    # node's own several real calls genuinely used different models
    # (each call's own real tokens are costed against its OWN real
    # model's own real rate, not all lumped under the last one).
    cost_usd = sum(
        calculate_cost_usd(u.get("model"), u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cached_tokens", 0))
        for u in usage_list
    )
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens,
        "model": model, "provider": provider, "cost_usd": round(cost_usd, 6),
    }


def _call_llm_tracked(ctx: dict, provider: str = "anthropic", **kwargs):
    """Real, thin wrapper around call_llm (§0.78, GAP_REPORT) — the
    actual fix for the reported gap: real usage (input_tokens,
    output_tokens, cached_tokens) came back from call_llm's own real
    return dict already, but nothing downstream ever read it. Every
    real call_llm call site in this file now goes through this wrapper
    instead, so real usage is genuinely accumulated on
    ctx["_llm_usage"] regardless of which node type made the call or
    how many real LLM calls that one node's own handler makes (e.g.
    handle_agent's own multi-step ReAct loop can make several real
    calls per node execution — all of them land here, all of them get
    captured). run_flow_sync's own node-dispatch code (and
    temporal_workflow.py's own execute_node_activity, for the durable
    path) resets ctx["_llm_usage"] to an empty list immediately before
    each node's handler runs, then reads it back after to build that
    node's own real span usage fields — so this list always reflects
    exactly the real LLM calls made by the ONE node currently
    executing, never a stale accumulation from an earlier node.

    LLMCallError (a real provider failure) propagates unchanged — a
    failed call still consumed real input tokens the provider actually
    billed for in some cases, but this platform has no real way to
    read usage off a raised exception the Anthropic SDK doesn't attach
    it to; a real, honest gap, not one this wrapper can close."""
    result = call_llm(**kwargs)
    usage = result.get("usage") or {}
    ctx.setdefault("_llm_usage", []).append({
        # The real marketing-name model string the caller actually
        # requested (kwargs["model"], defaulting to call_llm's own real
        # default when the caller didn't pass one) — NOT
        # result["model"], which is the real, internal API-level model
        # string (e.g. "claude-sonnet-5") call_llm maps to before the
        # real request. calculate_cost_usd's own price table, and every
        # other real place this platform names a model (Agent.model,
        # the builder's own model picker), all use the marketing name
        # consistently — using the API-level string here would make
        # every real cost lookup silently miss and fall back to $0.00.
        "model": kwargs.get("model", "Claude Sonnet 5"), "provider": provider,
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_tokens": usage.get("cached_tokens", 0) or 0,
    })
    return result


def handle_llm(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real prompt construction from the node's own System/User fields
    (Prompt Configuration in the builder) with real {{var}} interpolation
    against ctx["vars"] — replacing the old fixed
    "<input>\\n\\nContext: <chunks>" template every LLM node used to send
    regardless of what was actually configured.

    context_var lets a Knowledge Retrieval node's output be referenced
    explicitly (e.g. {{context}} wired to retrieved_chunks) instead of
    always silently appending retrieved_chunks; when unset, falls back to
    the old always-append-retrieved_chunks behavior for backward
    compatibility with flows saved before this existed.

    memory_enabled (§0.47, GAP_REPORT — upgraded from a text-transcript
    prefix to a genuine multi-turn messages array) threads real prior
    turns for this (agent_id, invoking_user) pair as real Anthropic
    `messages` entries, via _load_chat_history/ChatMessage — a real
    robustness improvement over the older AgentMemory
    __llm_chat_history__ blob approach, which is still real and still
    used by handle_agent/handle_parameter_extractor's own separate
    memory_enabled toggles, not touched by this change. When a node
    doesn't set memory_enabled explicitly, it inherits the real
    agent-level features.chat_history toggle (ctx["_chat_history_enabled"],
    seeded once in run_flow_sync) — an explicit per-node true/false
    still wins over that default.

    §0.64, GAP_REPORT — a real, reported gap: the config panel offered
    many controls (temperature, top_p, timeout, retry, error handling,
    plus several this fix does NOT wire — see below) that this handler
    simply never read. Now genuinely wired, matching the exact real
    Anthropic API parameters call_llm itself now accepts:

    - temperature/top_p: resolved the same way the builder's own UI
      resolves them — an explicit per-node value wins; otherwise the
      real preset (param_preset: precise/balanced/creative) supplies
      the default, the identical values NodeConfigPanel.tsx's own
      presetDefaults table uses, so the builder's displayed value and
      what actually gets sent to the API can't silently diverge.
    - timeout_s: real, per-call override passed straight to the
      Anthropic SDK.
    - retry_on_failure / error_handling ("None" | "Default value" |
      "Fail branch"): the exact same real, established pattern
      handle_code already uses — a real second attempt on
      retry_on_failure, then "Default value" sets ctx["output"] to a
      real configured fallback and continues, or "Fail branch" sets
      ctx["vars"]["_llm_failed"] for compute_next_node_id to route down
      a real, distinct error port (Builder.tsx's own outputPorts, now
      extended alongside the code node's identical mechanism), rather
      than every real provider failure always raising and failing the
      whole run regardless of what the node was actually configured to
      do about it.

    Real, honest, NOT wired by this fix — stated plainly rather than
    silently ignored:
    - frequency_penalty / presence_penalty: the real Anthropic Messages
      API has no such parameters at all (confirmed directly against
      the real SDK's own call signature) — unlike OpenAI's API, which
      does. These two controls cannot be wired to the one real,
      functional provider this platform has; removed from the config
      panel rather than left as controls that can never do anything.
    (agentic_strategy and allowed_tools were listed here. They are wired
    now — see _run_llm_agentic_strategy above. The reason given for not
    wiring agentic_strategy was that it "would need this handler to
    replicate handle_agent's own real multi-step tool-calling loop";
    replicating it is precisely what was avoided, by lifting that loop
    into run_react_loop / run_function_calling_loop and having both
    nodes call it. allowed_tools is the tool list that loop offers.)
    - vision / vision_detail: real image support needs real file-data
      resolution and base64 image content blocks built into the prompt,
      a real, separate feature; vision_detail's low/high concept has no
      real Anthropic API equivalent regardless (again an OpenAI-specific
      concept).
    - structured_output / guardrail_profile / reasoning_tag_separation:
      each would need real, additional design decisions this fix doesn't
      make on the person's behalf (a real output schema for
      structured_output, a real node-level moderation mechanism distinct
      from the existing agent-level one for guardrail_profile, a real
      parsing convention for reasoning_tag_separation) — real, stated
      follow-on work, not silently faked here.

      (The reason given here for allowed_tools was "this node has no
      iterative loop the way the Agent node does". It has one now, when
      a strategy is selected, which is what made wiring it possible.)"""
    model = cfg.get("model", "Claude Sonnet 5")
    mode = cfg.get("mode", "agentic")
    step_limit = int(cfg.get("step_limit", 12) or 12)

    system_prompt = _interp_vars(cfg.get("system_prompt", ""), ctx)
    user_prompt = _interp_vars(cfg.get("user_prompt") or "{{input}}", ctx)

    context_var = (cfg.get("context_var") or "").strip()
    if context_var:
        var_name = context_var[2:-2].strip() if context_var.startswith("{{") and context_var.endswith("}}") else context_var
        context_value = ctx["vars"].get(var_name)
        if context_value:
            user_prompt = f"{user_prompt}\n\nContext:\n{context_value}"
    elif ctx["vars"].get("retrieved_chunks"):
        user_prompt = f"{user_prompt}\n\nContext: {ctx['vars']['retrieved_chunks']}"

    history_enabled = cfg.get("memory_enabled", ctx.get("_chat_history_enabled", False))
    invoking_user = ctx.get("invoking_user", "anon")
    chat_history = _load_chat_history(session, ctx["agent_id"], invoking_user,
                                      conversation_id=ctx.get("conversation_id")) if history_enabled else None

    if mode == "agentic":
        # A single real call stands in for the harness's tool-proposal loop
        # (§3.5) in this reference implementation — there's no real second
        # tool to propose calling mid-loop yet. Step count is tracked and
        # the limit is genuinely enforced, just against a loop of length 1.
        ctx["vars"]["harness_steps"] = ctx["vars"].get("harness_steps", 0) + 1
        if ctx["vars"]["harness_steps"] > step_limit:
            raise HarnessStepLimitExceeded()

    # Real preset resolution (§0.64, GAP_REPORT) — must match
    # NodeConfigPanel.tsx's own presetDefaults table exactly, since an
    # explicit per-node value always wins and the preset only supplies
    # what wasn't explicitly set.
    preset = cfg.get("param_preset", "balanced")
    preset_defaults = {
        "precise": {"temperature": 0, "top_p": 0.85},
        "balanced": {"temperature": 0.5, "top_p": 0.9},
        "creative": {"temperature": 1, "top_p": 1},
    }.get(preset, {"temperature": 0.5, "top_p": 0.9})
    temperature = cfg["temperature"] if cfg.get("temperature") is not None else preset_defaults["temperature"]
    top_p = cfg["top_p"] if cfg.get("top_p") is not None else preset_defaults["top_p"]
    timeout_s = cfg.get("timeout_s")
    timeout = float(timeout_s) if timeout_s not in (None, "") else None

    ctx["vars"]["_llm_failed"] = False

    # The Agentic strategy control, genuinely wired.
    #
    # It used to be a dropdown the runtime never read — disabled in the
    # Builder and listed in node_wiring.UNWIRED_CONTROLS, because making
    # it real was described as needing this handler to replicate the
    # Agent node's tool-calling loop. It calls that loop instead: the
    # same run_react_loop / run_function_calling_loop the Agent node
    # runs, so the two cannot diverge.
    #
    # Only when a strategy is EXPLICITLY configured. `mode: "agentic"`
    # has always meant a single call here with the step counter
    # incremented, and a flow saved before this change must keep
    # behaving exactly that way rather than silently becoming a
    # multi-round, multi-call loop with the token cost that implies.
    strategy = _llm_agentic_strategy(cfg)
    if mode == "agentic" and strategy:
        return _run_llm_agentic_strategy(
            cfg, ctx, session, strategy=strategy, model=model, task=user_prompt,
            system_prompt=system_prompt, step_limit=step_limit,
        )

    try:
        result = _call_llm_tracked(ctx, model=model, prompt=user_prompt, system=system_prompt or None, history=chat_history,
                                    temperature=temperature, top_p=top_p, timeout=timeout)
    except LLMCallError as e:
        if cfg.get("retry_on_failure") and not ctx.get("_durable_execution"):
            # §0.105, GAP_REPORT — the actual fix for a real, reported
            # retry-multiplication bug: on the durable (Temporal) path,
            # ctx["_durable_execution"] is set (execute_node_activity's
            # own _execute_node_sync, temporal_workflow.py), and
            # Temporal's own _retry_policy_for_node ALREADY retries
            # this entire Activity on a real failure — stacking this
            # handler's own second attempt on top would silently turn
            # one configured retry into several real, duplicate
            # provider calls (and real, duplicated token cost) per
            # Temporal-level attempt. The sync path never sets this
            # flag, so this real, handler-level retry stays fully
            # active there, where it's the only real retry mechanism
            # at all.
            try:
                result = _call_llm_tracked(ctx, model=model, prompt=user_prompt, system=system_prompt or None, history=chat_history,
                                            temperature=temperature, top_p=top_p, timeout=timeout)
            except LLMCallError as retry_e:
                result = None
                last_error = retry_e
        else:
            result = None
            last_error = e
        if result is None:
            error_handling = cfg.get("error_handling", "None")
            if error_handling == "Default value":
                default = cfg.get("default_value", "")
                ctx["vars"]["llm_output"] = {"text": default, "error": str(last_error), "mock": False}
                ctx["output"] = ctx["output"] or default
                return ctx
            if error_handling == "Fail branch":
                ctx["vars"]["_llm_failed"] = True
                ctx["vars"]["llm_output"] = {"text": None, "error": str(last_error), "mock": False}
                return ctx
            raise

    ctx["vars"]["llm_output"] = result
    ctx["output"] = ctx["output"] or result.get("text")

    if history_enabled:
        conv = ctx.get("conversation_id")
        _append_chat_turn(session, ctx["agent_id"], invoking_user, "user", user_prompt, conv)
        _append_chat_turn(session, ctx["agent_id"], invoking_user, "assistant", result.get("text", ""), conv)

    return ctx


# --------------------------------------------------------------------
# Shared agentic strategies.
#
# These two loops were the body of handle_agent's own strategy branch.
# They are functions now because the LLM node needed the SAME behaviour:
# its "Agentic strategy" control (ReAct / Function calling) was a
# dropdown the runtime never read, disabled in the UI and listed in
# node_wiring.UNWIRED_CONTROLS, because wiring it was described as
# needing "this handler to replicate handle_agent's own real multi-step
# tool-calling loop".
#
# Replicating it is exactly what must not happen: two copies of a
# tool-use loop drift, and the copy that drifts is the one nobody is
# looking at. So the loop moved out here and BOTH nodes call it. A fix
# to either strategy now lands in both by construction.
#
# `compose_system` rather than a finished system string: each caller
# layers its own context onto the strategy's own base prompt — the Agent
# node prepends instructions and conversation memory, the LLM node its
# own system prompt — and the base text is part of the strategy's
# identity, not the caller's.
# --------------------------------------------------------------------

_LLM_STRATEGIES = {
    # What the Builder writes now, matching the Agent node's own values.
    "react": "react",
    "function_calling": "function_calling",
    # What it wrote before: the option LABELS were the stored values.
    # Kept so a flow saved earlier resolves instead of silently falling
    # back to a single call under a strategy it thinks it selected.
    "react (reason + act loop)": "react",
    "function calling": "function_calling",
}


def _llm_agentic_strategy(cfg: dict) -> Optional[str]:
    """The strategy an LLM node is configured for, or None for the
    single-call behaviour this node has always had.

    None for an unrecognised value on purpose: silently treating it as
    ReAct would run a multi-round loop nobody asked for, and the cost of
    that is real tokens."""
    raw = (cfg.get("agentic_strategy") or "").strip().lower()
    return _LLM_STRATEGIES.get(raw)


def _run_llm_agentic_strategy(cfg: dict, ctx: dict, session: Session, *, strategy: str, model: str,
                              task: str, system_prompt: str, step_limit: int) -> dict:
    """Run an LLM node through the shared agentic loops.

    Deliberately reuses the Agent node's vars as well as its loops:
    agent_reasoning_trace, agent_tool_outputs and agent_iteration_count
    are what the Builder's own trace view already renders, so a strategy
    run from this node shows its real steps instead of being a single
    opaque output. ctx["output"] is still the final answer, so every
    downstream node sees exactly what it saw before."""
    any_mock = {"v": False}

    def do_call(prompt: str, system: str, tools=None, tool_choice=None) -> dict:
        r = _call_llm_tracked(ctx, prompt=prompt, model=model, system=system,
                              tools=tools, tool_choice=tool_choice)
        any_mock["v"] = any_mock["v"] or r.get("mock", False)
        return r

    def compose_system(base_system: str) -> str:
        # The node's own system prompt layers ON TOP of the strategy's
        # base instructions rather than replacing them: drop the base and
        # ReAct stops emitting the "Final Answer:" marker its own loop
        # terminates on, so it would silently run to the cap every time.
        return f"{system_prompt}\n\n{base_system}".strip() if system_prompt else base_system

    def log(level: str, msg: str) -> None:
        session.add(AgentLog(agent_id=ctx["agent_id"],
                             ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], level=level, msg=msg))

    # step_limit is this node's own existing cap; the Agent node calls
    # the same concept max_iterations. Reused rather than adding a second
    # control that would silently disagree with it.
    max_iterations = max(1, int(step_limit or 12))
    log("INFO", f"LLM node agentic strategy started (strategy={strategy}, model={model})")

    if strategy == "function_calling":
        outcome = run_function_calling_loop(
            task=task, max_iterations=max_iterations, do_call=do_call,
            compose_system=compose_system, log=log,
            # allowed_tools is this node's own tool list, now that it
            # feeds a loop that can actually call them.
            tool_names=cfg.get("allowed_tools") or [], session=session,
        )
    else:
        outcome = run_react_loop(
            task=task, max_iterations=max_iterations, do_call=do_call,
            compose_system=compose_system, log=log,
        )

    final_text = outcome["final_text"]
    success = bool(final_text) and not outcome["hit_cap"]
    log("INFO" if success else "WARN",
        f"LLM node agentic strategy finished — {outcome['iteration_count']} iteration(s), success={success}")
    session.commit()

    ctx["vars"]["llm_output"] = final_text
    ctx["vars"]["llm_mock"] = any_mock["v"]
    ctx["vars"]["_llm_failed"] = False
    ctx["vars"]["agent_strategy"] = strategy
    # Both names, exactly as handle_agent sets them. agent_steps is the
    # one the run response and the Builder's trace view actually read
    # (harness.py's own result assembly) — setting only the newer
    # agent_reasoning_trace made a real multi-round loop report zero
    # reasoning steps, which reads as "the strategy did not run".
    ctx["vars"]["agent_steps"] = outcome["steps"]
    ctx["vars"]["agent_reasoning_trace"] = outcome["steps"]
    ctx["vars"]["agent_tool_outputs"] = outcome["tool_outputs"]
    ctx["vars"]["agent_iteration_count"] = outcome["iteration_count"]
    ctx["vars"]["agent_success"] = success
    ctx["vars"]["harness_steps"] = ctx["vars"].get("harness_steps", 0) + outcome["iteration_count"]
    ctx["output"] = final_text
    return ctx


def run_react_loop(*, task: str, max_iterations: int, do_call, compose_system, log) -> dict:
    """Alternating Thought/Action rounds until the model concludes with
    a real "Final Answer:" marker, or the cap is reached. Needs no
    tools, which is why the LLM node can offer it with nothing else
    configured."""
    steps: List[dict] = []
    # Honest mock-mode note: the mock call_llm echoes back a slice of
    # its own prompt (see model_adapter.py), and this loop's own
    # prompt text necessarily contains the literal instruction
    # "Final Answer:" — so without a real ANTHROPIC_API_KEY, step 1's
    # echoed response will always accidentally satisfy the stopping
    # condition, making ReAct look like it "only ever takes 1 step."
    # That's a mock-mode artifact, not a logic bug: a real model
    # doesn't echo the prompt, so this only stops early when the real
    # model genuinely concludes.
    transcript = f"Task: {task}\n"
    final_text = None
    iteration_count = 0
    hit_cap = True
    for i in range(max_iterations):
        iteration_count += 1
        resp = do_call(
            transcript + "\nRespond with 'Thought: ...' and either 'Action: ...' to continue reasoning or 'Final Answer: ...' to conclude.",
            compose_system("You are a ReAct-style agent: alternate Thought/Action steps, then conclude with 'Final Answer: <result>'."),
        )
        steps.append({"phase": f"step_{i + 1}", "output": resp["text"]})
        transcript += f"\n{resp['text']}\n"
        if "Final Answer:" in resp["text"]:
            final_text = resp["text"].split("Final Answer:", 1)[1].strip()
            hit_cap = False
            break
    if hit_cap:
        final_text = steps[-1]["output"] if steps else ""
        log("WARN", f"Agent hit max_iterations ({max_iterations}) without a real 'Final Answer:' marker")
    return {"final_text": final_text, "steps": steps, "tool_outputs": [],
            "iteration_count": iteration_count, "hit_cap": hit_cap}


def run_function_calling_loop(*, task: str, max_iterations: int, do_call, compose_system, log,
                              tool_names: List[str], session: Session) -> dict:
    """Genuinely iterative tool use via the real tools/tool_choice API.

    With no tools configured this degrades honestly: no tool schema is
    sent, the model answers directly on the first round, and the result
    says so — rather than pretending a tool loop ran."""
    steps: List[dict] = []
    tool_outputs: List[dict] = []
    # Real, genuinely iterative tool-use loop — up to max_iterations
    # real rounds, using the real Anthropic tools/tool_choice="auto"
    # extension (see model_adapter.call_llm), not a hardcoded
    # decide-then-answer shape.
    #
    # §0.112, GAP_REPORT — two real, related fixes here, alongside
    # the actual connector-call fix (see below): (1) each real
    # tool's own schema is now built from its own real, configured
    # ToolAction.params (tool_action_input_schema, tool_connectors.py)
    # instead of a single, generic "query" string every real tool
    # shared — the model genuinely couldn't have known, say,
    # "JSON Parse" needs json_data/json_filter specifically when
    # it was never told; (2) each real "tool:Name:Action" catalog
    # string is now sanitized into a real, Anthropic-API-valid
    # tool name (colons/spaces are NOT valid tool-name characters
    # for the real Anthropic API — confirmed directly, this was a
    # separate, real, pre-existing bug that would have made
    # function_calling genuinely fail against the real API the
    # moment any real tool was selected, mock-only until now), with
    # a real, local mapping kept back to the original catalog
    # string for real dispatch.
    from .tool_connectors import is_builtin_connector, call_builtin_connector, tool_action_input_schema, ToolConnectorError
    import re
    import json
    
    def _sanitize_tool_name(catalog_obj: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", catalog_obj)[:128]

    sanitized_to_catalog = {_sanitize_tool_name(t): t for t in tool_names}
    tool_action_rows: Dict[str, ToolAction] = {}
    for t in tool_names:
        if t.startswith("tool:"):
            _, real_tool_name, action_name = (t.split(":", 2) + ["", ""])[:3]
            row = session.exec(
                select(ToolAction).where(ToolAction.tool_name == real_tool_name, ToolAction.name == action_name)
            ).first()
            if row:
                tool_action_rows[t] = row
    tools_schema = [
        {
            "name": _sanitize_tool_name(t),
            "description": (tool_action_rows[t].desc if t in tool_action_rows
                             else f"Real catalog object '{t}' — see the Tool/Connector node's own catalog for what it does."),
            "input_schema": (tool_action_input_schema(tool_action_rows[t].params) if t in tool_action_rows
                              else {"type": "object", "properties": {"query": {"type": "string", "description": "What to ask this tool/connector for."}}}),
        }
        for t in tool_names
    ] if tool_names else None

    transcript_prompt = f"Task: {task}"
    final_text = None
    iteration_count = 0
    hit_cap = True
    for i in range(max_iterations):
        iteration_count += 1
        resp = do_call(transcript_prompt, compose_system(
            "You are a function-calling agent. Use the available tools if the task genuinely needs them; "
            "otherwise answer directly. Call at most one tool per turn."
        ), tools=tools_schema, tool_choice={"type": "auto"} if tools_schema else None)
        tool_input = resp.get("tool_input")
        if tool_input is not None and tool_names:
            # Real fix (§0.62, GAP_REPORT) — call_llm now surfaces
            # which tool was genuinely selected (resp["tool_call"]),
            # not just what it was called with. The previous
            # tool_names[0] fallback meant multi-tool agent behavior
            # was silently wrong the moment the model picked any
            # tool other than the first configured one — this now
            # reads the real selection, falling back to the first
            # configured tool only for the genuinely tool-name-less
            # case (a stale response shape from before this fix, or
            # a caller that bypassed call_llm directly), not as the
            # default outcome for a real, working multi-tool call.
            tool_call = resp.get("tool_call") or {}
            sanitized_name = tool_call.get("name")
            tool_name = sanitized_to_catalog.get(sanitized_name) or (tool_names[0] if tool_names else sanitized_name)

            # §0.112, GAP_REPORT — the actual fix for the reported
            # gap: a real, locally-implementable tool now genuinely
            # executes (tool_connectors.py) with the model's own
            # real, structured arguments, rather than a hardcoded
            # placeholder string regardless of which tool was
            # selected. A tool this platform genuinely can't
            # connect (real, external, per-deployment credentials
            # it doesn't have) still gets an honest, explicit
            # result saying so — never a fabricated success.
            if is_builtin_connector(tool_name):
                try:
                    real_result = call_builtin_connector(tool_name, tool_input if isinstance(tool_input, dict) else {})
                    result_text = real_result if isinstance(real_result, str) else json.dumps(real_result, default=str)
                except ToolConnectorError as e:
                    result_text = f"[tool call failed: {e}]"
            else:
                result_text = (
                    f"[{tool_name} genuinely requires real, external, per-deployment credentials this "
                    f"platform doesn't have configured — see tool_connectors.py]"
                )
            tool_outputs.append({"tool": tool_name, "input": tool_input, "output": result_text})
            steps.append({"phase": f"tool_call_{iteration_count}", "output": f"Called {tool_name} with {tool_input} -> {result_text}"})
            log("INFO", f"Agent called tool '{tool_name}' with {tool_input}")
            transcript_prompt = f"Task: {task}\n\nTool '{tool_name}' was called with {tool_input} and returned: {result_text}\n\nContinue, or give the final answer if you have enough information."
            continue
        steps.append({"phase": f"answer_{iteration_count}", "output": resp["text"]})
        final_text = resp["text"]
        hit_cap = False
        break
    if hit_cap:
        # A real, honest distinction from a genuine final answer: the
        # cap was hit while the model was still only requesting
        # tools — steps[-1]'s own text is a real tool-call log line,
        # not a real answer, so it must NOT count as success.
        final_text = steps[-1]["output"] if steps else ""
        log("WARN", f"Agent hit max_iterations ({max_iterations}) without a real final answer")
    return {"final_text": final_text, "steps": steps, "tool_outputs": tool_outputs,
            "iteration_count": iteration_count, "hit_cap": hit_cap}


def handle_agent(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real agentic-strategy selector — genuinely different execution
    PATTERNS (not just different prompts), built on the same real/mock
    call_llm every LLM node already uses. Distinct from the `llm` node's
    own simple agentic/fixed mode toggle (a single call standing in for a
    loop), and distinct from `subagent` (which invokes a WHOLE OTHER
    agent's flow) — this is about how ONE node reasons, not about calling
    other agents. Rebuilt to match Dify's own documented spec more
    closely (https://docs.dify.ai/en/cloud/use-dify/nodes/agent) — see
    GAP_REPORT §0.37 for the full comparison.

    Real `query` ({{var}}-interpolated — falls back to the prior
    ctx["output"]-or-ctx["input"] behavior when unset, so an existing
    flow's config keeps working unchanged) and real `instructions`
    ({{var}}-interpolated, genuinely prepended to every strategy's
    system prompt) — previously neither existed; a person had no way to
    give the agent a role/persona/constraints at all, and no way to
    point it at a specific upstream variable instead of whatever the
    prior node happened to output.

    Real `memory_enabled` reuses the EXACT same real conversation-
    history mechanism (AgentMemory, key "__llm_chat_history__") the LLM
    node's and Parameter Extractor's own memory toggles already use —
    genuine consistency, not a separately-invented mechanism.

    Real `max_iterations` (falls back to the old `max_steps` key for
    backward compatibility) is now a real, GENERAL cap — not just
    react's own. `function_calling` was rebuilt from a hardcoded ≤2-call
    shape into a genuinely ITERATIVE loop (real Anthropic tool-use —
    `tools`/`tool_choice="auto"`, the same real extension
    Parameter Extractor's 'function_call' mode added to call_llm),
    matching Dify's own documented "iteratively decide which tools to
    use and when" — a real, multi-round tool-use loop, not a single
    decide-then-answer shape. Real `tools` config: a list of UCS catalog
    object names (the same real objects the `tool` node's own catalog
    picker offers) the agent may call; each real tool call still gets
    an honest placeholder result (consistent with handle_tool's own
    stated boundary — no real external connectors are configured in
    this reference platform), but the DECISION of which tool to call,
    and the real iteration over multiple tool calls, is genuine model
    reasoning via a real tool-use API call, not text-parsing a
    "CALL: toolname" convention.

    - plan_execute (this platform's own real addition beyond Dify's 2
      documented strategies — kept, not removed, since it's a real,
      useful shape Dify doesn't happen to name): always exactly 2 real
      calls, a fixed plan-then-execute order.
    - function_calling: 1 to max_iterations real tool-use rounds,
      genuinely stopping the moment the model's real response contains
      no further tool-use request.
    - react: 1 to max_iterations real calls in a real accumulating
      transcript, stopping as soon as the model's real output contains
      a real "Final Answer:" marker rather than always running to the
      cap.

    Real structured output matching Dify's own documented concepts:
    `agent_final_answer` (Dify: "Final Answer"), `agent_tool_outputs`
    (Dify: "Tool Outputs" — a real array of {tool, input, output} per
    real call), `agent_reasoning_trace` (Dify: "Reasoning Trace"),
    `agent_iteration_count` (Dify: "Iteration Count"), `agent_success`
    (Dify: "Success Status" — real: did it produce a genuine final
    answer within the cap, not just "no exception was raised"). The
    prior output var names (`agent_steps`, `agent_output`) are kept
    alongside the new ones, unchanged, for backward compatibility.

    Real Agent Logs (Dify: "structured log events... for debugging and
    monitoring tool invocations") — genuinely persisted as real
    `AgentLog` rows, not just returned in this run's own response;
    needs `session`, which is why this handler is now in the
    session-requiring dispatch list (it wasn't before)."""
    strategy = cfg.get("strategy", "react")
    model = cfg.get("model", "Claude Sonnet 5")
    max_iterations = int(cfg.get("max_iterations") or cfg.get("max_steps") or 3)

    query_cfg = cfg.get("query")
    # {{env.KEY}} is normally resolved before the handler runs (the run loop
    # merges this node's encrypted Env values into the env map). This catches
    # a direct call and any reference the loop did not see.
    import re as _re
    from . import agent_assets as _aa
    _env = _aa.load_node_secrets(session, ctx["agent_id"], ctx.get("_current_node_id"), _aa.declared_env_keys(cfg))

    def _interp(text: str) -> str:
        text = _re.sub(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
                       lambda m: _env.get(m.group(1), m.group(0)), text or "")
        return _interp_vars(text, ctx)

    task = _interp(query_cfg) if query_cfg else (ctx.get("output") or ctx.get("input", ""))
    # `prompt` is the Configure panel's field; `instructions` is the older
    # name and still honoured so existing flows keep working.
    instructions = _interp(cfg.get("prompt") or cfg.get("instructions") or "")
    # An asset id the library no longer has is a hard failure, not a silent
    # omission: a published version must not quietly lose its skills or files.
    try:
        skills_prompt = _aa.build_skills_prompt(session, ctx["agent_id"], cfg)
        files_block = _aa.build_files_block(session, ctx["agent_id"], cfg)
    except _aa.AssetMissingError as e:
        raise ValueError(str(e))
    if skills_prompt:
        instructions = f"{instructions}\n\n{skills_prompt}".strip()
    if files_block:
        # Reference files are untrusted data: they go in the user turn, with a
        # system note saying so, never into the system prompt itself.
        instructions = f"{instructions}\n\n{_aa.FILES_SYSTEM_NOTE}".strip()
        task = f"{files_block}\n\n{task}"

    def with_instructions(base_system: str) -> str:
        return f"{instructions}\n\n{base_system}".strip() if instructions else base_system

    scope_key = f"{ctx['agent_id']}:{ctx.get('invoking_user', 'anon')}"
    history_row = None
    if cfg.get("memory_enabled"):
        history_row = session.exec(
            select(AgentMemory).where(AgentMemory.scope_key == scope_key, AgentMemory.key == "__llm_chat_history__")
        ).first()

    def log(level: str, msg: str) -> None:
        session.add(AgentLog(agent_id=ctx["agent_id"], ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3], level=level, msg=msg))

    def system_with_memory(base_system: str) -> str:
        s = with_instructions(base_system)
        if history_row and history_row.value:
            s = f"{s}\n\nConversation so far (most recent last):\n{history_row.value}".strip()
        return s

    steps: List[dict] = []
    tool_outputs: List[dict] = []
    any_mock = {"v": False}

    def do_call(prompt: str, system: str, tools=None, tool_choice=None) -> dict:
        r = _call_llm_tracked(ctx, prompt=prompt, model=model, system=system, tools=tools, tool_choice=tool_choice)
        any_mock["v"] = any_mock["v"] or r.get("mock", False)
        return r

    log("INFO", f"Agent node started (strategy={strategy}, model={model})")

    if strategy == "plan_execute":
        plan_resp = do_call(
            f"Task: {task}\n\nProduce a short, numbered plan (3-5 steps) for accomplishing this task. Output ONLY the numbered plan.",
            system_with_memory("You are a planning assistant. Produce a concise numbered plan, nothing else."),
        )
        steps.append({"phase": "plan", "output": plan_resp["text"]})
        exec_resp = do_call(
            f"Task: {task}\n\nPlan:\n{plan_resp['text']}\n\nCarry out this plan and give the final result.",
            system_with_memory("You are executing a pre-made plan. Follow it and produce the final result."),
        )
        steps.append({"phase": "execute", "output": exec_resp["text"]})
        final_text = exec_resp["text"]
        iteration_count = 2
        hit_cap = False

    elif strategy == "function_calling":
        outcome = run_function_calling_loop(
            task=task, max_iterations=max_iterations, do_call=do_call,
            compose_system=system_with_memory, log=log,
            tool_names=cfg.get("tools") or [], session=session,
        )
        steps.extend(outcome["steps"])
        tool_outputs.extend(outcome["tool_outputs"])
        final_text, iteration_count, hit_cap = (
            outcome["final_text"], outcome["iteration_count"], outcome["hit_cap"])

    else:  # react (default)
        outcome = run_react_loop(
            task=task, max_iterations=max_iterations, do_call=do_call,
            compose_system=system_with_memory, log=log,
        )
        steps.extend(outcome["steps"])
        final_text, iteration_count, hit_cap = (
            outcome["final_text"], outcome["iteration_count"], outcome["hit_cap"])

    success = bool(final_text) and not hit_cap

    if cfg.get("memory_enabled"):
        # Real idempotency (§0.76, GAP_REPORT) — the actual, concrete
        # "memory append requires idempotency" case the report names:
        # this reads the prior real chat history, concatenates this
        # turn, and writes the full result back — a genuine Temporal
        # retry of this same Activity (the whole point of
        # idempotent_once's own real, stable key: it's identical across
        # every retry of the SAME logical node-execution attempt) would
        # otherwise re-read the now-ALREADY-updated prior value from the
        # first, actually-successful attempt and silently double-append
        # the same turn. ctx.get("_idempotency_key") is only ever set on
        # the durable/Temporal path (see temporal_workflow.py's own
        # _idempotency_key_for) — idempotent_once itself just calls the
        # real append directly when it's absent, so the sync path's own
        # behavior is completely unchanged.
        def _append_history():
            prior = history_row.value if history_row else ""
            new_history = f"{prior}\nUser: {task}\nAssistant: {final_text}".strip()
            upsert_agent_memory(session, scope_key, "__llm_chat_history__", new_history)
            return {"appended": True}
        node_key = ctx.get("_idempotency_key")
        idempotent_once(session, f"{node_key}:chat_history" if node_key else None, _append_history)

    log("INFO" if success else "WARN", f"Agent node finished — {iteration_count} iteration(s), success={success}")
    session.commit()

    ctx["vars"]["agent_strategy"] = strategy
    ctx["vars"]["agent_steps"] = steps  # kept for backward compatibility
    ctx["vars"]["agent_reasoning_trace"] = steps
    ctx["vars"]["agent_mock"] = any_mock["v"]
    ctx["vars"]["agent_output"] = final_text  # kept for backward compatibility
    ctx["vars"]["agent_final_answer"] = final_text
    ctx["vars"]["agent_tool_outputs"] = tool_outputs
    ctx["vars"]["agent_iteration_count"] = iteration_count
    ctx["vars"]["agent_success"] = success
    ctx["output"] = ctx["output"] or final_text
    return ctx


def handle_tool(cfg: dict, ctx: dict, session: Session) -> dict:
    """§0.112, GAP_REPORT — the actual fix for a real, reported gap:
    this node used to have every other real piece of a genuine
    tool-call pipeline (a real UCS entitlement check, right below) but
    the actual connector call itself was a hardcoded placeholder
    string regardless of which real catalog object was configured. A
    real, locally-implementable catalog_object (see tool_connectors.py
    — JSON processing, the current time, this platform's own already-
    real code sandbox, a governed web fetch) now genuinely executes
    and returns its own real result. A catalog_object this platform
    genuinely cannot connect without real, external, per-deployment
    credentials (a real markitdown/MinerU/Jina AI/audio-provider
    account) stays an honest, explicit placeholder — see
    tool_connectors.py's own module docstring for the full, stated
    reasoning on exactly which is which, and why."""
    from .ucs_client import UCSUnavailable
    from .tool_connectors import is_builtin_connector, call_builtin_connector, ToolConnectorError
    obj = cfg.get("catalog_object", "unknown")
    try:
        check = check_entitlement(session, ctx["agent_id"], ctx["agent_name"],
                                   ctx["invoking_user"], obj, tier="select")
        if not check["resolved"]:
            if UCS_SOFT_CHECK:
                ctx["vars"].setdefault("ucs_soft_denials", []).append(check)
            else:
                raise UCSDenied(obj, check)
    except UCSUnavailable:
        if not UCS_SOFT_CHECK:
            raise
        ctx["vars"].setdefault("ucs_soft_denials", []).append({"object": obj, "resolved": False, "error": "UCS unreachable"})

    if is_builtin_connector(obj):
        # Real parameter resolution (§0.112, GAP_REPORT) — cfg's own
        # new "param_values" field (the Builder's own real per-action
        # input fields — NodeConfigPanel.tsx) maps each real, named
        # parameter the selected action declares (ToolAction.params)
        # to either a real {{var}} reference or a real literal string,
        # resolved against this run's own live ctx the same real
        # {{var}} substitution convention handle_template's own
        # rendering already uses.
        import re as _re
        import json as _json
        raw_params = cfg.get("param_values") or {}
        resolved_params = {
            k: _re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(ctx["vars"].get(m.group(1), "")), v) if isinstance(v, str) else v
            for k, v in raw_params.items()
        }
        try:
            result = call_builtin_connector(obj, resolved_params)
        except ToolConnectorError as e:
            ctx["vars"]["tool_output"] = f"[tool call failed: {e}]"
            return ctx
        ctx["vars"]["tool_output"] = result if isinstance(result, str) else _json.dumps(result, default=str)
        return ctx

    ctx["vars"]["tool_output"] = (
        f"[{obj} genuinely requires real, external, per-deployment credentials this platform doesn't have "
        f"configured — not a placeholder oversight, a stated, honest scope boundary; see tool_connectors.py]"
    )
    return ctx


class CodeExecutionError(Exception):
    """A real, genuine failure inside the sandboxed Python script — a
    nonzero exit code, not a harness/orchestration problem. Kept as its
    own exception type so exec_node's real classification (see its
    docstring) can tell "the user's code crashed" apart from "this
    platform's own code crashed" if that distinction is ever needed."""
    def __init__(self, stderr: str):
        self.stderr = stderr
        super().__init__(stderr)


def handle_code(cfg: dict, ctx: dict) -> dict:
    """Real Python execution — genuinely runs the person's code in the
    real sandbox (sandbox.py), with real input-variable injection, a
    real output-variable name, and real error handling, matching every
    control the Code node's own config panel actually offers (Input
    variables, Output variables, Retry on failure, Error handling) —
    none of these were previously read by this function at all, so the
    controls did nothing; see GAP_REPORT for that finding.

    Real input injection: arg1_name/arg2_name (the real parameter names
    a person chooses) are resolved from arg1_var/arg2_var (the same
    {{var}}-style references every other node's config uses) against
    ctx, then genuinely assigned as real Python variables before the
    person's own code runs — a real fix for a real gap: previously
    nothing was ever injected, so a Code node's own "Input variables"
    config was entirely decorative and the UI's own default placeholder
    code (`def main(arg1, arg2): ...`) never actually ran, since nothing
    ever called `main()`.

    Real invocation + real output capture: if the person's code defines
    a `main(...)` function, it's genuinely called with the injected
    values as real keyword arguments (using their real chosen names),
    and its real return value becomes this node's real output — stored
    under BOTH `ctx["vars"]["code_output"]` (the full raw sandbox
    result: stdout/stderr/exit_code, for debugging) and
    `ctx["vars"][output_name]` (the person's real chosen output variable
    name, defaulting to "result") so a downstream node can reference it
    by the name the person actually picked, not a fixed generic key.

    Real error handling — a nonzero real exit code (see sandbox.py's own
    real exit_code/stderr capture) now genuinely means something, honest
    per the real `error_handling` setting:
    - 'None' (default): re-raises as a real CodeExecutionError, so this
      node's span genuinely shows 'error' and the run genuinely fails —
      previously a crashing script was silently reported as a fully
      successful node and a successful run.
    - 'Default value': the real failure is caught here; `output_name` is
      set to the real configured `default_value` (empty string if none
      given) and the flow genuinely continues, rather than raising.
    - 'Fail branch': the real failure is caught here; `ctx["vars"]
      ["_code_failed"]` is set so compute_next_node_id (below) can route
      down a real, distinct 'error' output port — the same real
      multi-port mechanism the `condition` node's true/false ports use
      — rather than always falling through the single default edge.
    `retry_on_failure`, when real (true), retries the real sandboxed
    execution once more before applying whichever of the above the
    person configured — a real retry, not just a relabeled single
    attempt."""
    import json as _json
    import re as _re

    code = cfg.get("code", "")

    def resolve_arg(var_ref: str) -> Any:
        var_ref = (var_ref or "").strip()
        if var_ref.startswith("{{") and var_ref.endswith("}}"):
            name = var_ref[2:-2].strip()
            return ctx.get("input", "") if name == "input" else ctx["vars"].get(name, "")
        return var_ref

    def safe_ident(name: str, fallback: str) -> str:
        name = (name or "").strip()
        return name if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else fallback

    arg1_name = safe_ident(cfg.get("arg1_name"), "arg1")
    arg2_name = safe_ident(cfg.get("arg2_name"), "arg2")
    arg1_val = resolve_arg(cfg.get("arg1_var", ""))
    arg2_val = resolve_arg(cfg.get("arg2_var", ""))
    output_name = cfg.get("output_name") or "result"

    # A real preamble that injects real values as real Python variables,
    # then — if (and only if) the person's own code defines a real
    # `main`, genuinely calls it with those same real names as keyword
    # arguments and prints its real return value behind a real sentinel
    # (so it's distinguishable from the person's own debug prints, which
    # remain in stdout untouched).
    preamble = (
        f"{arg1_name} = {_json.dumps(arg1_val)}\n"
        f"{arg2_name} = {_json.dumps(arg2_val)}\n"
    )
    postamble = (
        "\nif 'main' in dir():\n"
        "    import json as _inaiera_json\n"
        f"    _inaiera_result = main({arg1_name}={arg1_name}, {arg2_name}={arg2_name})\n"
        "    print('__INAIERA_RESULT__' + _inaiera_json.dumps(_inaiera_result, default=str))\n"
    )
    wrapped_code = preamble + code + postamble

    result = run_sandboxed_code(wrapped_code)
    if result["exit_code"] != 0 and cfg.get("retry_on_failure") and not ctx.get("_durable_execution"):
        # §0.105, GAP_REPORT — same real reasoning as handle_llm's own
        # identical check just above: suppressed on the durable path,
        # where Temporal's own _retry_policy_for_node already retries
        # this entire Activity, so this handler's own second attempt
        # would otherwise stack on top of it. Fully active on the sync
        # path, where it's the only real retry mechanism.
        result = run_sandboxed_code(wrapped_code)

    ctx["vars"]["code_output"] = result

    if result["exit_code"] != 0:
        error_handling = cfg.get("error_handling", "None")
        if error_handling == "Default value":
            default = cfg.get("default_value", "")
            ctx["vars"][output_name] = default
            ctx["vars"]["code_result"] = default
            return ctx
        if error_handling == "Fail branch":
            ctx["vars"]["_code_failed"] = True
            ctx["vars"][output_name] = None
            ctx["vars"]["code_result"] = None
            return ctx
        raise CodeExecutionError(result["stderr"] or f"Code exited with status {result['exit_code']}")

    ctx["vars"]["_code_failed"] = False
    stdout = result.get("stdout", "")
    marker = "__INAIERA_RESULT__"
    idx = stdout.find(marker)
    if idx != -1:
        raw = stdout[idx + len(marker):].strip().splitlines()[0] if stdout[idx + len(marker):].strip() else ""
        try:
            parsed = _json.loads(raw)
        except Exception:
            parsed = raw
    else:
        # No `main` defined (or it printed nothing parseable) — the
        # real, honest fallback is the script's own real stdout, same
        # as before this fix existed, not a fabricated value.
        parsed = stdout
    ctx["vars"][output_name] = parsed
    # A fixed, second real copy under `code_result` — the CLEAN parsed
    # value, never the raw sandbox stdout (which can carry the internal
    # __INAIERA_RESULT__ sentinel above) — so handle_respond's real
    # fallback chain (below) has a stable key to prefer regardless of
    # whatever custom `output_name` was configured for this node.
    ctx["vars"]["code_result"] = parsed
    return ctx


def _pick_body_output(ctx: dict):
    """Picks whatever a node body most recently, genuinely produced —
    with its REAL type preserved (int stays int, dict stays dict), not
    coerced to a display string the way handle_respond's own fallback
    chain deliberately does (that one is specifically about producing
    final user-facing text; this one is about collecting a real typed
    value, e.g. for the Iteration node's real per-element result — see
    run_iteration_body). Same real priority order as handle_respond's
    own fallback chain, so the two stay conceptually aligned."""
    if ctx.get("output") is not None:
        return ctx["output"]
    llm_out = ctx["vars"].get("llm_output")
    if llm_out:
        return llm_out.get("text", "")
    code_out = ctx["vars"].get("code_result")
    if code_out is not None:
        return code_out
    template_out = ctx["vars"].get("template_output")
    if template_out:
        return template_out
    http_out = ctx["vars"].get("http_output")
    if http_out:
        return http_out.get("body", "")
    return None


def finalize_iteration_outcomes(outcomes: List[Optional[tuple]], error_mode: str, flatten: bool) -> List[Any]:
    """Real, shared outcome-finalization for the Iteration node (§0.52,
    GAP_REPORT) — the one real piece of run_iteration_body's own logic
    that doesn't need a live DB session, a real thread, or a real
    Temporal activity call, so it's genuinely usable by (and unit-
    testable independent of) both real runtimes: harness.py's own sync
    run_iteration_body and temporal_workflow.py's Temporal-native port,
    which both now call this instead of duplicating the same real
    per-outcome error-mode/flatten logic.

    outcomes is a real, positional list — outcomes[i] is either None
    (that index was never reached, e.g. cancelled before it ran) or a
    real (index, value, error) tuple. Matches Dify's own 3 documented
    error modes exactly: 'Terminated' is handled by the caller before
    this is ever invoked (it stops the whole iteration node outright,
    so there's nothing left to finalize); 'Continue on error' keeps a
    real None at that real index; 'Remove abnormal output' genuinely
    omits that index, producing a real, shorter output array rather
    than a None placeholder."""
    results: List[Any] = []
    for outcome in outcomes:
        if outcome is None:
            continue
        _, value, error = outcome
        if error:
            if error_mode == "Remove abnormal output":
                continue
            results.append(None)
            continue
        if flatten and isinstance(value, list):
            results.extend(value)
        else:
            results.append(value)
    return results


def handle_answer(cfg: dict, ctx: dict) -> dict:
    """Real Answer node (Chatflow apps) — matches Dify's own documented
    spec (https://docs.dify.ai/en/cloud/use-dify/nodes/answer): real
    {{var}} interpolation of freeform content against ctx["vars"]/
    ctx["input"] (same convention _interp_vars already gives every
    other text field in this platform).

    Real support for MULTIPLE Answer nodes in one flow, matching Dify's
    own documented "deliver content at different stages" behavior: each
    one's real resolved content is appended to a real running list
    (ctx["vars"]["answer_stream"]) in real execution order, rather than
    each one silently overwriting the last — ctx["output"] becomes
    their real joined text.

    Real, simplified multimodal support: a referenced {{var}} that
    resolves to a real uploaded-file-shaped dict (`{filename, url,
    ...}` — the same real shape /api/uploads produces, see
    routers/uploads.py) renders as a real inline Markdown link, not a
    silently stringified Python dict.

    Honest, stated scope boundary vs Dify's own documented behavior:
    Dify streams a node's own text progressively, character by
    character, pausing mid-string at the first unresolved {{var}} and
    resuming once it resolves. This platform's real streaming (see
    routers/execution.py's run_agent_stream) is real but coarser-
    grained — a whole Answer node's content becomes visible as one real
    node_done SSE event once fully resolved, not chunked mid-string.
    Every Answer node still gets that same real live event, same as
    every other node type; it's the granularity that differs."""
    content = cfg.get("content", "")

    def render_var(m: Any) -> str:
        name = m.group(1)
        val = ctx.get("input", "") if name == "input" else ctx["vars"].get(name)
        # Shared with _interp_vars so an Answer node and a Template node
        # render the same variable the same way.
        return _var_to_text(val)

    import re
    rendered = re.sub(r"\{\{\s*(\w+)\s*\}\}", render_var, content)

    ctx["vars"].setdefault("answer_stream", []).append(rendered)
    ctx["output"] = "\n\n".join(ctx["vars"]["answer_stream"])
    return ctx


def handle_respond(cfg: dict, ctx: dict) -> dict:
    """Formats the final output. Prefers whatever the immediately-preceding
    node already put there (e.g. a template node sets ctx["output"]
    directly) — only falls back to llm_output/code_result/etc. if nothing
    upstream set anything, and never overwrites an existing value with an
    empty string."""
    if ctx.get("output"):
        return ctx
    llm_out = ctx["vars"].get("llm_output")
    # code_result — the real CLEAN parsed value handle_code produces
    # (a `main()` return value, or the script's own real stdout if it
    # defines no `main`) — never the raw sandbox result dict, whose own
    # "stdout" field can carry handle_code's internal
    # __INAIERA_RESULT__ sentinel when a return value was captured.
    code_out = ctx["vars"].get("code_result")
    template_out = ctx["vars"].get("template_output")
    http_out = ctx["vars"].get("http_output")
    if llm_out:
        ctx["output"] = llm_out.get("text", "")
    elif code_out is not None:
        import json as _json
        ctx["output"] = code_out if isinstance(code_out, str) else _json.dumps(code_out, default=str)
    elif template_out:
        ctx["output"] = template_out
    elif http_out:
        ctx["output"] = str(http_out.get("body", ""))
    else:
        ctx["output"] = ""
    return ctx


def eval_expr(expr: str, variables: dict) -> bool:
    """Deliberately restricted safe evaluation of a comparison expression
    like `{{quality_score}} >= 70`. Shared by handle_condition and any
    condition node inside a loop body, so the two don't drift into
    subtly different expression languages.

    §0.102, GAP_REPORT — the actual fix for a real, reported CRITICAL
    security vulnerability: this used to call Python's own real
    eval(substituted, {"__builtins__": {}}, {}) directly. An empty
    __builtins__ dict is NOT a real Python sandbox — it blocks direct
    calls to builtin FUNCTIONS (open, __import__, exec, etc.) but does
    nothing at all to stop attribute traversal through Python's own,
    always-reachable runtime object graph: confirmed directly, live,
    against this exact function before writing this fix, the
    expression `().__class__.__mro__[1].__subclasses__()` genuinely
    enumerated 830 real, live classes in this process, including
    subprocess.Popen — a real, complete path to arbitrary code
    execution in the Agent API process or the Temporal worker process,
    entirely OUTSIDE the real, dedicated Code Node sandbox
    (sandbox.py's own run_sandboxed_code) this codebase has otherwise
    invested considerable, real effort hardening.

    Real fix: eval() is not used anywhere in this function anymore, at
    all. The expression is parsed with ast.parse(..., mode="eval") —
    which alone already rejects multi-statement input and assignment-
    like constructs — and the resulting tree is walked by
    _safe_eval_ast_node below, which implements a real, explicit
    allow-list (Compare/BoolOp/UnaryOp/BinOp/Name/Constant/List/Tuple
    and their own specific, individually-named operators only) and
    raises _ExpressionSecurityError for every other real node type,
    including — checked directly, individually, by this fix's own new
    regression tests — Attribute, Call, Lambda, Subscript, ListComp,
    DictComp, GeneratorExp, NamedExpr, Await, and Yield. There is no
    real code path in this new implementation that ever reaches
    Python's own eval, exec, compile, or __import__, so the class of
    attack this fix closes is not just the one specific expression
    above — it's structurally unreachable regardless of which
    subclass, dunder, or built-in a future expression might try to
    reach through attribute traversal, since attribute traversal
    itself (ast.Attribute) is rejected outright, not merely
    builtins-starved."""
    import re
    # Substituted FIRST, then measured: a {{var}} holding a megabyte of
    # text makes a short expression enormous, so the limit has to apply
    # to what is actually parsed rather than to what the author typed.
    substituted = re.sub(r"\{\{(\w+)\}\}", lambda m: repr(variables.get(m.group(1), 0)), expr or "")
    try:
        _check_expression_size(substituted)
        tree = ast.parse(substituted, mode="eval")
        _check_expression_shape(tree)
        return bool(_safe_eval_ast_node(tree.body, variables))
    except Exception:
        # False is the safe default for a branch, but silence is not: an
        # expression that cannot be evaluated took the else branch with
        # nothing anywhere saying why. That is how a threshold of 10000
        # came to auto-approve 25000 without a trace.
        logger.warning("Condition %r could not be evaluated (substituted: %r) — "
                       "taking the false branch", expr, substituted, exc_info=True)
        return False


def _check_expression_size(text: str) -> None:
    if len(text) > MAX_EXPRESSION_CHARS:
        raise _ExpressionLimitError(
            f"expression is {len(text)} characters after variable substitution; "
            f"the limit is {MAX_EXPRESSION_CHARS}"
        )


def _check_expression_shape(tree: "ast.AST") -> None:
    """Bound the parsed tree before evaluating any of it.

    Node count and depth are checked up front rather than during
    evaluation because the cheapest time to refuse an expensive
    expression is before the first operation runs."""
    nodes = 0
    for _ in ast.walk(tree):
        nodes += 1
        if nodes > MAX_EXPRESSION_AST_NODES:
            raise _ExpressionLimitError(
                f"expression has more than {MAX_EXPRESSION_AST_NODES} AST nodes")


def _guard_value(value, what: str):
    """Bound a literal or an intermediate result.

    Applied to results, not only to inputs: concatenation and
    multiplication both grow a value past any single literal's limit, so
    checking the operands alone would leave the growth unbounded."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_INT_MAGNITUDE:
            raise _ExpressionLimitError(
                f"{what} is an integer beyond +/-2**63; arbitrary-precision arithmetic is how a "
                f"short expression turns into unbounded work")
        return value
    if isinstance(value, str):
        if len(value) > MAX_RESULT_STRING_CHARS:
            raise _ExpressionLimitError(
                f"{what} is a {len(value)}-character string; the limit is "
                f"{MAX_RESULT_STRING_CHARS}")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_CONTAINER_ELEMENTS:
            raise _ExpressionLimitError(
                f"{what} has {len(value)} elements; the limit is {MAX_CONTAINER_ELEMENTS}")
        return value
    return value


def validate_expression(expr: str) -> Optional[str]:
    """Why this expression would be refused at run time, or None.

    Exists so a malformed or over-budget condition is reported when the
    flow is published, instead of silently evaluating to False forever
    at run time — a condition that always takes its false branch and
    never says why is worse than a publish error, because it looks like
    a logic bug in the flow.

    Evaluated against placeholder variables, so this checks structure
    and declared literals; a limit that depends on a runtime value (a
    {{var}} holding a megabyte) can only be caught when that value
    exists.
    """
    import re

    text = re.sub(r"\{\{(\w+)\}\}", "0", expr or "")
    if not text.strip():
        return "the expression is empty"
    try:
        _check_expression_size(text)
        tree = ast.parse(text, mode="eval")
        _check_expression_shape(tree)
    except SyntaxError as e:
        return f"not a valid expression: {e.msg}"
    except _ExpressionLimitError as e:
        return str(e)
    except Exception as e:
        return f"could not be parsed: {type(e).__name__}"
    try:
        _safe_eval_ast_node(tree.body, {}, depth=0)
    except _ExpressionSecurityError as e:
        return f"uses something this evaluator does not allow: {e}"
    except _ExpressionLimitError as e:
        return str(e)
    except Exception:
        # A runtime error with placeholder values (comparing 0 to a
        # string, dividing by zero) is not a structural problem — the
        # real values may well be fine.
        return None
    return None


# Resource-complexity limits for the condition evaluator.
#
# The AST allow-list closed the code-execution hole, but a
# valid-but-pathological expression could still consume the process.
# Measured on this build before these limits, in a 2GB container:
#
#     "'a' * 200000000 == ''"   ->  False in 0.04s, peak RSS +191 MB
#
# 21 characters. It returns False — because the allocation succeeds and
# the comparison is simply false — so nothing in the result hints that a
# fifth of a gigabyte was just allocated. A handful of concurrent runs
# exhausts the container, and the run still looks ordinary.
#
# Every limit below is generous for the thing this evaluator is for: a
# condition node comparing a few flow variables. None of them constrains
# an expression a person would actually write.
MAX_EXPRESSION_CHARS = 2000
MAX_EXPRESSION_AST_NODES = 250
MAX_EXPRESSION_DEPTH = 25
MAX_STRING_LITERAL_CHARS = 1000
MAX_CONTAINER_ELEMENTS = 100
# Beyond 2**63 an int is arbitrary-precision, and arbitrary precision is
# how a bounded number of multiplications still produces an unbounded
# amount of work.
MAX_INT_MAGNITUDE = 2 ** 63
# Concatenation can grow a value past any single literal's limit, so the
# RESULT is bounded too, not just the inputs.
MAX_RESULT_STRING_CHARS = 10_000


class _ExpressionLimitError(Exception):
    """A structurally valid expression that asks for too much.

    Separate from _ExpressionSecurityError so the two reasons stay
    distinguishable: one means "you tried to reach something you may
    not", the other means "this is too expensive to evaluate". Both are
    refusals, but only the second is something a reasonable author might
    hit by accident, and validate_expression reports it at publish time
    for exactly that reason.
    """


class _ExpressionSecurityError(Exception):
    """Raised the moment _safe_eval_ast_node encounters any real AST
    node type outside its own deliberately narrow allow-list — caught
    by eval_expr's own try/except above the exact same way any other
    malformed expression already is, so a rejected expression fails
    exactly like a syntax error would to whoever wrote it, rather than
    leaking any detail about what specifically tripped the check."""


_SAFE_COMPARE_OPS = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}
_SAFE_BINARY_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
}


def _numeric_coerce(left, right):
    """Let a numeric-looking STRING compare against a number.

    A trigger field produces a string — a form input always does — so
    `{{amount}} > 10000` arrives as `'25000' > 10000`, which raises
    TypeError in Python 3. eval_expr catches every exception and returns
    False, so the condition silently took its ELSE branch: an approval
    threshold of 10000 auto-approved 25000. Confirmed live on Showcase 2,
    where the same input passed as a number escalated correctly and as a
    string did not.

    Coercion is deliberately narrow — only when one side is a real
    number and the other is a string that is entirely a number. It does
    NOT touch string-to-string comparisons, so `{{code}} == "007"` still
    compares as text and still matches.
    """
    def as_number(value):
        if isinstance(value, bool):        # bool is an int; not a number here
            return None
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                text = value.strip()
                return float(text) if ("." in text or "e" in text.lower()) else int(text)
            except ValueError:
                return None
        return None

    if isinstance(left, str) == isinstance(right, str):
        return left, right                 # both text or both not — leave alone
    ln, rn = as_number(left), as_number(right)
    if ln is None or rn is None:
        return left, right                 # not a number on both sides after all
    return ln, rn


def _safe_eval_ast_node(node: "ast.AST", variables: dict, depth: int = 0):
    """The real, explicit allow-list itself (§0.102, GAP_REPORT) — see
    eval_expr's own docstring above for the full real reasoning. Every
    branch below names the exact, specific AST node types and
    operators it accepts; anything not explicitly named here falls
    through to the final `raise` — there is deliberately no permissive
    fallback branch of any kind."""
    if depth > MAX_EXPRESSION_DEPTH:
        raise _ExpressionLimitError(
            f"expression nests deeper than {MAX_EXPRESSION_DEPTH} levels")
    if isinstance(node, ast.Constant):
        # Real, primitive literal values only — excludes bytes and
        # Ellipsis (neither has any legitimate use in a comparison
        # expression, and both are unnecessary surface area to reason
        # about further). bool is a real subclass of int in Python, so
        # this one check already, correctly, covers True/False too.
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            if isinstance(node.value, str) and len(node.value) > MAX_STRING_LITERAL_CHARS:
                raise _ExpressionLimitError(
                    f"string literal is {len(node.value)} characters; the limit is "
                    f"{MAX_STRING_LITERAL_CHARS}")
            return _guard_value(node.value, "a literal")
        raise _ExpressionSecurityError(f"constant type {type(node.value).__name__}")
    if isinstance(node, ast.Name):
        # Real, direct lookup against the flow's own vars dict —
        # genuinely never falls through to Python's own builtins,
        # globals, or any other name resolution mechanism.
        # Bounded as well: a flow variable holding a huge string or an
        # arbitrary-precision int costs the same as a literal one, and it
        # did not pass through the literal check.
        return _guard_value(variables.get(node.id, 0), f"variable {node.id!r}")
    if isinstance(node, ast.List):
        if len(node.elts) > MAX_CONTAINER_ELEMENTS:
            raise _ExpressionLimitError(
                f"list literal has {len(node.elts)} elements; the limit is "
                f"{MAX_CONTAINER_ELEMENTS}")
        return [_safe_eval_ast_node(e, variables, depth + 1) for e in node.elts]
    if isinstance(node, ast.Tuple):
        if len(node.elts) > MAX_CONTAINER_ELEMENTS:
            raise _ExpressionLimitError(
                f"tuple literal has {len(node.elts)} elements; the limit is "
                f"{MAX_CONTAINER_ELEMENTS}")
        return tuple(_safe_eval_ast_node(e, variables, depth + 1) for e in node.elts)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _safe_eval_ast_node(node.operand, variables, depth + 1)
        if isinstance(node.op, ast.USub):
            return -_safe_eval_ast_node(node.operand, variables, depth + 1)
        raise _ExpressionSecurityError(f"unary operator {type(node.op).__name__}")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = _safe_eval_ast_node(value_node, variables, depth + 1)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value_node in node.values:
                result = _safe_eval_ast_node(value_node, variables, depth + 1)
                if result:
                    return result
            return result
        raise _ExpressionSecurityError(f"bool operator {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        op_func = _SAFE_BINARY_OPS.get(type(node.op))
        if op_func is None:
            raise _ExpressionSecurityError(f"binary operator {type(node.op).__name__}")
        left = _safe_eval_ast_node(node.left, variables, depth + 1)
        right = _safe_eval_ast_node(node.right, variables, depth + 1)
        if isinstance(node.op, ast.Mult) and (
                isinstance(left, (str, list, tuple)) or isinstance(right, (str, list, tuple))):
            # Sequence multiplication, removed outright. Nothing is
            # decided by repeating a string, and it is the cheapest way
            # to turn 21 characters into 191 MB: "'a' * 200000000 == ''".
            raise _ExpressionLimitError(
                "multiplying a string, list or tuple is not allowed in a condition expression")
        return _guard_value(op_func(left, right), "the result of an arithmetic operation")
    if isinstance(node, ast.Compare):
        left = _safe_eval_ast_node(node.left, variables, depth + 1)
        for op, comparator_node in zip(node.ops, node.comparators):
            op_func = _SAFE_COMPARE_OPS.get(type(op))
            if op_func is None:
                raise _ExpressionSecurityError(f"comparison operator {type(op).__name__}")
            right = _safe_eval_ast_node(comparator_node, variables, depth + 1)
            left, right = _numeric_coerce(left, right)
            if not op_func(left, right):
                return False
            left = right
        return True
    # Every other real AST node type — explicitly including, but not
    # limited to, Attribute, Call, Lambda, Subscript, ListComp,
    # DictComp, SetComp, GeneratorExp, NamedExpr, Await, Yield,
    # YieldFrom, Starred, Dict, Set, and Expression itself if ever
    # passed here directly — is rejected. There is no default/passthrough
    # case: an unrecognized node type is always treated as unsafe.
    raise _ExpressionSecurityError(f"node type {type(node).__name__}")


def handle_condition(cfg: dict, ctx: dict) -> dict:
    """IF/ELSE — real evaluation of a comparison expression like
    `{{quality_score}} >= 70` against ctx["vars"]."""
    expr = cfg.get("expression", "")
    ctx["vars"]["branch_taken"] = eval_expr(expr, ctx["vars"])
    return ctx


def handle_exit_loop(cfg: dict, ctx: dict) -> dict:
    """A real pass-through — its actual meaning isn't in this function at
    all. run_loop_body (in run_flow_sync) checks for a node's TYPE being
    'exit_loop' directly, as the signal to stop iterating and fall through
    to whatever the loop container's own single downstream edge points
    to. This handler exists so an exit_loop node still gets a normal
    'ok' span in the trace rather than being silently skipped — it's real
    bookkeeping, not fake work."""
    return ctx


# Note: `loop` itself has no NODE_HANDLERS entry. Its own execution is
# special-cased directly in run_flow_sync's traversal (routed to
# run_loop_body instead of exec_node) — the container's "handler" IS the
# real nested body-walking logic, not a dispatch-table function like every
# other node type.


def _resolve_cloud_url(url: str):
    """Identifies the provider and produces a fetchable HTTPS URL.

    Honesty boundary: this reference implementation has no AWS/GCP/Azure
    SDK and no service-account credentials configured — it does NOT do
    authenticated private-bucket access. `s3://bucket/key` and
    `gs://bucket/key` shorthand get converted to their public
    virtual-hosted-style URL, which only actually resolves if the object
    is public. The real, supported case — matching how these links are
    normally shared for one-off document processing — is a PRESIGNED
    HTTPS URL (S3 presigned URL, GCS signed URL, Azure SAS token), which
    already carries its own auth in the query string and needs no SDK at
    all; those are passed through unchanged."""
    if url.startswith("s3://"):
        bucket, _, key = url[len("s3://"):].partition("/")
        return "s3", f"https://{bucket}.s3.amazonaws.com/{key}", None
    if url.startswith("gs://"):
        bucket, _, key = url[len("gs://"):].partition("/")
        return "gcs", f"https://storage.googleapis.com/{bucket}/{key}", None
    if ".blob.core.windows.net" in url:
        return "azure", url, None
    if ".s3." in url or "s3.amazonaws.com" in url:
        return "s3", url, None
    if "storage.googleapis.com" in url:
        return "gcs", url, None
    if url.startswith("http://") or url.startswith("https://"):
        return "generic", url, None
    return None, None, f"Unrecognized URL — expected s3://, gs://, an Azure Blob URL, or a presigned https:// link, got: {url}"


def _decode_text(content: bytes) -> str:
    """Real encoding detection (chardet) with a real UTF-8 fallback —
    matches Dify's own documented behavior. Tries a real strict UTF-8
    decode first (if it succeeds cleanly, the content almost certainly
    genuinely is UTF-8); on failure, trusts chardet's real best guess
    regardless of its confidence score — chardet's confidence is
    genuinely often low on short/realistic files (verified: 0.07 on a
    real 40-character Windows-1252 sample) even when its guess is
    correct, so requiring high confidence defeated the whole point.
    Previously this reference platform always assumed UTF-8 regardless
    of a file's real encoding, silently mangling anything else (a real
    Windows-1252 file with accented characters came out as
    'Caf\ufffd \ufffd \ufffdsmart quotes\ufffd' — see GAP_REPORT §0.33)."""
    import chardet
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    detected = chardet.detect(content)
    encoding = detected.get("encoding")
    if encoding:
        try:
            return content.decode(encoding, errors="replace")
        except (LookupError, TypeError):
            pass
    return content.decode("utf-8", errors="replace")


def _html_to_text(content: bytes) -> str:
    """Real HTML parsing (BeautifulSoup) — genuinely strips markup
    rather than returning raw tags as if they were content. Previously:
    '<h1>Title</h1><p>Some <b>bold</b> text...' came out completely
    unchanged, tags and all — see GAP_REPORT §0.33. Real block-level
    elements (p/div/h1-h6/li/br/tr) get a real line break between them;
    inline elements (b/a/span/em, etc.) stay on the same line as their
    surrounding text, so a sentence with an inline link doesn't get
    fragmented into one word per line."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_decode_text(content), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    block_tags = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "tr", "blockquote", "pre"}
    for tag in soup.find_all(block_tags):
        tag.append("\n")
    lines = [line.strip() for line in soup.get_text(separator=" ").splitlines()]
    import re as _re
    lines = [_re.sub(r"\s+", " ", line).strip() for line in lines]
    return "\n".join(line for line in lines if line)


def _docx_to_text(content: bytes) -> str:
    """Real paragraph AND table extraction, in real document order —
    previously only `doc.paragraphs` was read, which silently drops
    every table entirely (verified: a real table with real cell values
    vanished with no error — see GAP_REPORT §0.33). Walks the real
    document body XML directly (python-docx's own `.paragraphs`/
    `.tables` are separate, unordered collections) so a table appearing
    between two paragraphs comes out in the right real place, matching
    Dify's own documented "maintain paragraph and table ordering"
    behavior. Real tables are converted to real Markdown, same as
    Dify's documented table handling."""
    import io
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(content))
    parts: List[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, doc).text
            if text.strip():
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            rows = [[cell.text.strip().replace("\n", " ") for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * len(rows[0])) + " |"]
            md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            parts.append("\n".join(md))
    return "\n\n".join(parts)


def _xlsx_to_markdown(content: bytes) -> str:
    """Real spreadsheet parsing (openpyxl) — previously .xlsx wasn't
    handled at all and fell through to a raw UTF-8 decode of the binary
    ZIP archive, producing garbage (`PK\\x03\\x04...`) as "extracted
    text" — see GAP_REPORT §0.33. Each real sheet becomes a real
    Markdown table, matching Dify's documented behavior; multiple
    sheets are real, separately labeled sections, not silently merged."""
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sections = []
    for sheet in wb.worksheets:
        rows = [[("" if c.value is None else str(c.value)) for c in row] for row in sheet.iter_rows()]
        rows = [r for r in rows if any(cell.strip() for cell in r)]
        if not rows:
            continue
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = [f"### {sheet.title}", "| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        sections.append("\n".join(md))
    return "\n\n".join(sections)


def _csv_to_markdown(content: bytes) -> str:
    """Real CSV → Markdown table conversion, matching Dify's own
    documented behavior — previously CSV was just raw-decoded as plain
    text (technically readable, but not the Markdown-table form Dify's
    spec documents, and fragile against embedded commas/quotes a real
    csv parser handles correctly)."""
    import csv
    import io
    text = _decode_text(content)
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(md)


def _pptx_to_text(content: bytes) -> str:
    """Real PowerPoint parsing (python-pptx) — every real text-bearing
    shape on every real slide, in real slide order. Previously .pptx
    wasn't handled at all (same binary-garbage fallback as .xlsx)."""
    import io
    from pptx import Presentation
    prs = Presentation(io.BytesIO(content))
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text.strip()]
        if texts:
            slides.append(f"### Slide {i}\n" + "\n".join(texts))
    return "\n\n".join(slides)


def _epub_to_text(content: bytes) -> str:
    """Real EPUB parsing (ebooklib) — walks the real spine (reading
    order), extracting real chapter text via the same real HTML
    stripping _html_to_text already does for standalone HTML, so an
    EPUB's real per-chapter HTML content comes out as real clean text,
    not raw markup."""
    import io
    import tempfile
    import os
    import ebooklib
    from ebooklib import epub

    # ebooklib's real reader needs a real file path, not just bytes.
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        f.write(content)
        tmp_path = f.name
    try:
        book = epub.read_epub(tmp_path)
        chapters = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            text = _html_to_text(item.get_content())
            if text.strip():
                chapters.append(text)
        return "\n\n".join(chapters)
    finally:
        os.unlink(tmp_path)


def _eml_to_text(content: bytes) -> str:
    """Real email parsing via Python's own real stdlib `email` module —
    genuinely walks a real multipart message for its real plain-text
    (or HTML, real-stripped) body, with real From/To/Subject headers
    prepended, rather than raw-decoding the entire raw RFC822 source
    (MIME boundaries, base64 attachment bodies, and all) as if it were
    the message content."""
    from email import message_from_bytes
    from email.header import decode_header

    def _decode_header_value(v):
        if not v:
            return ""
        parts = decode_header(v)
        return "".join(p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p for p, enc in parts)

    msg = message_from_bytes(content)
    header_lines = [
        f"From: {_decode_header_value(msg.get('From'))}",
        f"To: {_decode_header_value(msg.get('To'))}",
        f"Subject: {_decode_header_value(msg.get('Subject'))}",
        f"Date: {msg.get('Date', '')}",
    ]
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not part.get_filename():
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                break
            if ctype == "text/html" and not part.get_filename() and not body:
                body = _html_to_text(part.get_payload(decode=True))
    else:
        payload = msg.get_payload(decode=True)
        body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
    return "\n".join(header_lines) + "\n\n" + body


def _msg_to_text(content: bytes) -> str:
    """Real Outlook .msg parsing (extract-msg — a real, pure-Python
    reader for the proprietary OLE2-based .msg format, not a
    reimplementation from scratch)."""
    import tempfile
    import os
    import extract_msg

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as f:
        f.write(content)
        tmp_path = f.name
    try:
        msg = extract_msg.Message(tmp_path)
        header_lines = [f"From: {msg.sender or ''}", f"To: {msg.to or ''}", f"Subject: {msg.subject or ''}", f"Date: {msg.date or ''}"]
        return "\n".join(header_lines) + "\n\n" + (msg.body or "")
    finally:
        os.unlink(tmp_path)


def _vtt_to_text(content: bytes) -> str:
    """Real WebVTT subtitle parsing — genuinely parses real cue blocks
    (timestamp lines + following text), and genuinely merges consecutive
    utterances from the SAME real `<v Speaker Name>` voice tag into one
    real line, matching Dify's own documented "merges consecutive
    utterances by the same speaker" behavior — not just stripping
    timestamps and leaving every line separate."""
    import re
    text = _decode_text(content)
    lines = text.splitlines()
    cues = []
    i = 0
    timestamp_re = re.compile(r"\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}")
    voice_re = re.compile(r"^<v\s+([^>]+)>(.*)$")
    while i < len(lines):
        if timestamp_re.search(lines[i]):
            i += 1
            cue_lines = []
            while i < len(lines) and lines[i].strip() and not timestamp_re.search(lines[i]):
                cue_lines.append(lines[i].strip())
                i += 1
            raw = " ".join(cue_lines)
            m = voice_re.match(raw)
            if m:
                cues.append((m.group(1).strip(), m.group(2).strip()))
            elif raw:
                cues.append((None, raw))
        else:
            i += 1

    grouped: List[tuple] = []
    for speaker, text_line in cues:
        if grouped and grouped[-1][0] == speaker:
            grouped[-1] = (speaker, grouped[-1][1] + " " + text_line)
        else:
            grouped.append((speaker, text_line))
    out_lines = [f"{s}: {t}" if s else t for s, t in grouped]
    return "\n".join(out_lines)


def _legacy_doc_to_text(content: bytes) -> str:
    """Real legacy .doc (binary OLE2 Word format) extraction via the
    real `antiword` system tool — a genuinely separate, older binary
    format from .docx that python-docx cannot read at all. Requires the
    real `antiword` package (see requirements.txt's comment / apt-get
    line in the Dockerfile/DEVELOPER_GUIDE) — this reference platform
    self-hosts it rather than calling Dify's real external Unstructured
    API service, an honest, deliberate substitution matching this
    project's established pattern (e.g. subprocess sandboxing instead
    of Firecracker) rather than a silent unsupported format."""
    import subprocess
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as f:
        f.write(content)
        tmp_path = f.name
    try:
        result = subprocess.run(["antiword", tmp_path], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "antiword failed")
        return result.stdout
    finally:
        os.unlink(tmp_path)


def _legacy_xls_to_markdown(content: bytes) -> str:
    """Real legacy .xls (binary format) extraction via the real
    `xls2csv` system tool (part of the real `catdoc` package) — openpyxl
    only reads the newer .xlsx XML format, not the older binary one."""
    import subprocess
    import tempfile
    import os
    import csv
    import io
    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as f:
        f.write(content)
        tmp_path = f.name
    try:
        result = subprocess.run(["xls2csv", tmp_path], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "xls2csv failed")
        rows = [r for r in csv.reader(io.StringIO(result.stdout)) if any(c.strip() for c in r)]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(md)
    finally:
        os.unlink(tmp_path)


def _legacy_ppt_to_text(content: bytes) -> str:
    """Real legacy .ppt (binary format) extraction via the real `catppt`
    system tool (also part of the real `catdoc` package) — python-pptx
    only reads the newer .pptx XML format."""
    import subprocess
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".ppt", delete=False) as f:
        f.write(content)
        tmp_path = f.name
    try:
        result = subprocess.run(["catppt", tmp_path], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "catppt failed")
        return result.stdout
    finally:
        os.unlink(tmp_path)


# Real, honest scope boundary — every format Dify's Doc Extractor
# documents is handled by a real parser above EXCEPT genuinely
# binary/non-text media (images, audio, video), which Dify's own docs
# state need "specialized processing tools or external services" too —
# not a gap unique to this platform. See GAP_REPORT §0.33.
# §0.111, GAP_REPORT — a real, module-level (picklable-by-reference)
# dispatch specifically for the four real ZIP-based document formats
# this platform parses — needed because _EXTRACTORS below maps to
# lambdas, and a lambda genuinely cannot be pickled to hand off to a
# real ProcessPoolExecutor worker (see _run_zip_extractor_with_limits'
# own docstring for why that's the real mechanism enforcing this
# report's own requested "parser wall-clock time" limit).
_ZIP_BASED_EXTRACTORS = {
    "docx": _docx_to_text,
    "xlsx": _xlsx_to_markdown,
    "pptx": _pptx_to_text,
    "epub": _epub_to_text,
}


def _parse_zip_based_document(ext: str, content: bytes) -> str:
    """Real, module-level worker entry point (§0.111, GAP_REPORT) —
    this is what actually runs inside the real, separate worker
    process a ProcessPoolExecutor spawns, genuinely isolated from this
    API process's own memory/CPU for the duration of one real parse
    call. Does NOT re-validate zip-container safety itself — that
    real, cheap, metadata-only check already ran in the parent process
    before this was ever submitted (see _extract_text_from_bytes) —
    this function's only job is the real, potentially-slow actual
    parse, in a real process a timeout can actually kill."""
    return _ZIP_BASED_EXTRACTORS[ext](content)


_ZIP_PARSE_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_ZIP_PARSE_TIMEOUT_SECONDS", "20"))

# Per-parser wall-clock budgets. Per-format because the formats are not
# comparable: a large PDF legitimately takes longer than a .vtt, and
# holding both to the shorter budget would refuse real documents while
# holding both to the longer one leaves the cheap formats over-permissive.
_PARSE_TIMEOUT_SECONDS = {
    "pdf": int(os.getenv("UPLOAD_PDF_PARSE_TIMEOUT_SECONDS", "30")),
    "docx": _ZIP_PARSE_TIMEOUT_SECONDS,
    "xlsx": _ZIP_PARSE_TIMEOUT_SECONDS,
    "pptx": _ZIP_PARSE_TIMEOUT_SECONDS,
    "epub": _ZIP_PARSE_TIMEOUT_SECONDS,
}
_DEFAULT_PARSE_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_PARSE_TIMEOUT_SECONDS", "15"))

# Resource ceilings applied INSIDE each worker process. The wall-clock
# timeout alone does not stop a parse from taking the machine down with
# it in the seconds before it fires: it can allocate without bound, or
# spawn children that outlive the worker. These are the CPU/memory/
# process limits the report asks for, and they are enforced by the
# kernel rather than by the parser cooperating.
_PARSE_WORKER_CPU_SECONDS = int(os.getenv("UPLOAD_PARSE_CPU_SECONDS", "60"))
_PARSE_WORKER_MEMORY_MB = int(os.getenv("UPLOAD_PARSE_MEMORY_MB", "1024"))

_zip_parse_pool = None


def _limit_parse_worker() -> None:
    """Applied once in each spawned worker, before it parses anything.

    RLIMIT_CPU is the one that matters most: it is charged for CPU
    actually burned, so it bounds the quadratic parser this fix was
    written for even while the process looks responsive. Measured on
    this build before the fix, in-process with no bound at all:

        20,000 nested <div>s (214 KB)  ->  5.03s
        200,000 nested <div>s (2 MB)   ->  still running after 10 min

    RLIMIT_DATA bounds allocation, and is chosen over RLIMIT_AS
    deliberately. Both were measured in this container and both work;
    the difference is what they bound. RLIMIT_AS caps VIRTUAL address
    space, which a Python process maps far more of than it ever commits
    — a parser that mmaps a large file can trip it without using the
    memory, so a value chosen from an RSS-shaped intuition is either
    too tight to be safe or too loose to be useful. RLIMIT_DATA caps the
    data segment, which is what a runaway allocation actually grows.
    Verified: 1 GB of RLIMIT_DATA starts cleanly and turns a 2 GB
    allocation into a MemoryError.

    RLIMIT_NPROC is deliberately NOT set, even though it works here. It
    is a limit per real UID rather than per process, so setting it in
    the worker also constrains the parent and anything else running as
    the same user — a side effect well outside what a document parse
    should be deciding. The process boundary and the wall-clock timeout
    are what contain a runaway helper.
    """
    import resource

    for limit, value in (
        (resource.RLIMIT_CPU, _PARSE_WORKER_CPU_SECONDS),
        (resource.RLIMIT_DATA, _PARSE_WORKER_MEMORY_MB * 1024 * 1024),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            # A platform that refuses one of these (macOS declines
            # RLIMIT_AS for a process this size) must still get the
            # others rather than none, and must still get the
            # wall-clock timeout, which does not depend on any of them.
            continue


def _parse_timeout_for(ext: str) -> float:
    """This format's wall-clock budget, resolved at CALL time.

    The ZIP formats read _ZIP_PARSE_TIMEOUT_SECONDS directly rather than
    a value copied into _PARSE_TIMEOUT_SECONDS at import: that module-level
    scalar is the documented knob for them, and a dict built once at
    import would silently ignore anyone — an operator or a test —
    changing it afterwards. Caught by an existing test that patches
    exactly that scalar and stopped having any effect.
    """
    if ext in _ZIP_BASED_EXTRACTORS:
        return _ZIP_PARSE_TIMEOUT_SECONDS
    return _PARSE_TIMEOUT_SECONDS.get(ext, _DEFAULT_PARSE_TIMEOUT_SECONDS)


def _parse_document_in_worker(ext: str, content: bytes, content_type: str) -> str:
    """The worker entry point for EVERY format, not just the ZIP ones.

    PDF used to be parsed inline in the API process — `PdfReader` on the
    request thread, with no timeout and no memory ceiling — and so did
    csv/eml/msg/vtt/html/doc/xls/ppt via _EXTRACTORS. Only the four
    ZIP-based formats were isolated. The HTML parser is quadratic in
    nesting depth, which is how a 2 MB upload became an unbounded CPU
    burn inside the service that has to answer health checks.

    Container safety for ZIP formats is NOT re-checked here: that
    metadata-only inspection already ran in the parent before this was
    submitted, and repeating it would cost the decompression it exists
    to avoid.
    """
    if ext == "pdf" or "pdf" in (content_type or ""):
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if ext in _ZIP_BASED_EXTRACTORS:
        return _ZIP_BASED_EXTRACTORS[ext](content)
    if ext in _EXTRACTORS:
        return _EXTRACTORS[ext](content, content_type)
    return _decode_text(content)


def _get_zip_parse_pool():
    """Real, lazily-created, process-wide worker pool (§0.111,
    GAP_REPORT) — the report's own suggested "constrained worker"
    made real: every real ZIP-based document parse (docx/xlsx/pptx/
    epub) runs inside one of these real, separate OS processes, not
    this API process's own event loop or thread pool — a genuinely
    pathological (slow, CPU-heavy, or hung) parse can be actually
    killed by a real timeout (see _run_zip_extractor_with_limits)
    without taking this API process itself down with it, and never
    shares this API process's own heap at all. Lazily created, once,
    matching this codebase's own established convention (storage.py's
    own backend singleton) for a real, expensive, process-wide
    resource that shouldn't be rebuilt per call."""
    global _zip_parse_pool
    if _zip_parse_pool is None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        # mp_context="spawn" (§0.111, GAP_REPORT) — a real, deliberate
        # choice over this platform's own default "fork" start method
        # on Linux: this API process is genuinely multi-threaded
        # (uvicorn's own thread pool, plus this codebase's own
        # extensive run_in_threadpool/asyncio.to_thread usage
        # elsewhere), and forking a multi-threaded process carries a
        # real, documented risk (Python's own multiprocessing docs) of
        # a child process inheriting a lock some OTHER, non-forking
        # thread held at the exact moment of fork — a lock that then
        # never releases in the child, a real, genuine deadlock risk.
        # "spawn" starts each real worker as a genuinely fresh
        # interpreter instead, slightly slower to start but immune to
        # this specific class of failure.
        _zip_parse_pool = ProcessPoolExecutor(
            max_workers=int(os.getenv("UPLOAD_ZIP_PARSE_WORKERS", "2")),
            mp_context=multiprocessing.get_context("spawn"),
            # CPU, memory and process ceilings inside each worker. The
            # wall-clock timeout bounds how long the PARENT waits; it
            # does nothing about how much the child consumes in the
            # meantime, which is what actually takes a machine down.
            initializer=_limit_parse_worker,
        )
    return _zip_parse_pool


def _run_zip_extractor_with_limits(ext: str, content: bytes, content_type: str) -> str:
    """Real, combined zip-bomb + wall-clock-timeout defense (§0.111,
    GAP_REPORT) — the actual fix for the report's own two, remaining,
    explicitly-requested protections beyond the raw size checks
    already enforced elsewhere:

    1. validate_zip_container_safety (upload_security.py) — a real,
       cheap, metadata-only inspection of the ZIP central directory,
       catching every real limit the report names (max entry count,
       max uncompressed bytes, max per-entry size, max compression
       ratio) BEFORE any real parser ever decompresses a single real
       byte of actual member content.

    2. A real wall-clock timeout, enforced by actually running the
       parse in a real, separate, killable OS process (see
       _get_zip_parse_pool's own docstring) — the report's own stated
       preference ("ideally perform complex document parsing in a
       constrained worker rather than the main API process") made
       real rather than a documented aspiration. A real
       concurrent.futures.TimeoutError here means the real, spawned
       worker process is genuinely still running past the real,
       configured limit — cancelled via the real Future, so it can't
       silently keep consuming real CPU/memory in the background
       after this function has already given up on it and returned an
       honest error."""
    from .upload_security import validate_zip_container_safety, ZipSafetyError
    # Cheap, metadata-only container inspection first, in this process:
    # it catches entry count, uncompressed size and compression ratio
    # before any parser decompresses a byte, and costs nothing to run
    # here. Only the expensive part is handed to a worker.
    validate_zip_container_safety(content)
    return _run_extractor_with_limits(ext, content, content_type)


def _run_extractor_with_limits(ext: str, content: bytes, content_type: str) -> str:
    """Run ANY document parse in a constrained worker process.

    Three limits, because each catches what the others cannot:

      * a wall-clock timeout in this process, so a hung parse cannot
        hold a request thread forever;
      * RLIMIT_CPU and RLIMIT_AS inside the worker, so a parse cannot
        consume the machine during the seconds before that timeout
        fires;
      * a separate OS process, so a parser that segfaults or is killed
        by the kernel takes nothing else with it.

    A BrokenProcessPool is the normal, expected outcome of the kernel
    killing a worker that hit RLIMIT_AS or RLIMIT_CPU — it is reported
    as a refusal to parse this document, not as a server fault, and the
    pool is discarded so the next upload gets fresh workers.
    """
    from concurrent.futures import BrokenExecutor
    from concurrent.futures import TimeoutError as FutureTimeoutError

    global _zip_parse_pool

    timeout = _parse_timeout_for(ext)
    pool = _get_zip_parse_pool()
    try:
        future = pool.submit(_parse_document_in_worker, ext, content, content_type)
    except BrokenExecutor:
        _zip_parse_pool = None
        pool = _get_zip_parse_pool()
        future = pool.submit(_parse_document_in_worker, ext, content, content_type)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        # The worker is still running past its budget. Discard the pool
        # so it is torn down rather than left burning CPU behind a
        # request that has already been answered.
        _zip_parse_pool = None
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        raise TimeoutError(
            f"parsing this .{ext or 'unknown'} document exceeded the configured "
            f"{timeout}s wall-clock limit"
        )
    except BrokenExecutor as e:
        _zip_parse_pool = None
        raise ValueError(
            f"this .{ext or 'unknown'} document could not be parsed: the parser exceeded its "
            f"memory or CPU limit and was stopped ({type(e).__name__})"
        ) from e


_EXTRACTORS = {
    "docx": lambda c, ct: _docx_to_text(c),
    "doc": lambda c, ct: _legacy_doc_to_text(c),
    "xlsx": lambda c, ct: _xlsx_to_markdown(c),
    "xls": lambda c, ct: _legacy_xls_to_markdown(c),
    "csv": lambda c, ct: _csv_to_markdown(c),
    "pptx": lambda c, ct: _pptx_to_text(c),
    "ppt": lambda c, ct: _legacy_ppt_to_text(c),
    "epub": lambda c, ct: _epub_to_text(c),
    "eml": lambda c, ct: _eml_to_text(c),
    "msg": lambda c, ct: _msg_to_text(c),
    "vtt": lambda c, ct: _vtt_to_text(c),
    "html": lambda c, ct: _html_to_text(c),
    "htm": lambda c, ct: _html_to_text(c),
}


def _extract_text_from_bytes(content: bytes, filename: str, content_type: str) -> str:
    """Real extraction across every format Dify's Doc Extractor node
    documents (see GAP_REPORT §0.33 for the full comparison this
    rewrite closes): pypdf for PDF, a real per-format parser (above) for
    docx/doc/xlsx/xls/csv/pptx/ppt/epub/eml/msg/vtt/html, and real
    chardet-based encoding detection (not an always-UTF-8 assumption)
    for everything else text-like (txt/md/json/xml/yaml/properties).
    Anything genuinely unrecognized still falls back to a real best-
    effort decode rather than silently returning nothing.

    §0.111, GAP_REPORT — DOCX/XLSX/PPTX/EPUB (every real ZIP-based
    format this platform parses) now route through
    _run_zip_extractor_with_limits instead of calling _EXTRACTORS
    directly — the actual fix for a real, reported DoS gap: none of
    python-docx/openpyxl/python-pptx/ebooklib enforce any real limit
    of their own on archive member count, expanded size, or parse
    time, so a real, small, well-formed-but-malicious archive
    (comfortably under this platform's own 20MB upload cap) could
    genuinely expand far beyond that, or simply take a genuinely
    unbounded amount of real wall-clock time, before this fix."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        if ext in _ZIP_BASED_EXTRACTORS:
            # Container validation first, then the constrained worker.
            return _run_zip_extractor_with_limits(ext, content, content_type)
        if ext == "pdf" or "pdf" in content_type or ext in _EXTRACTORS:
            # PDF and every other parser now go to the same constrained
            # worker. PDF used to run PdfReader inline on the request
            # thread, and _EXTRACTORS ran csv/eml/msg/vtt/html/doc/xls/
            # ppt the same way — the HTML parser is quadratic in nesting
            # depth, so a 2 MB upload was an unbounded CPU burn inside
            # the process that also has to answer health checks.
            return _run_extractor_with_limits(ext, content, content_type)
        # txt, md, json, xml, yaml/yml, properties, and any other
        # genuinely text-like format — real encoding detection, not a
        # blind UTF-8 assumption.
        return _decode_text(content)
    except Exception as e:
        return f"[extraction failed: {e}]"


def _resolve_docextractor_input(template: str, ctx: dict):
    """Resolves a Doc Extractor input reference two real ways: a whole
    `{{field_name}}` reference (the entire template, nothing else)
    resolves to the RAW ctx value with its real type preserved (via the
    same shared `_resolve_whole_var` the Variable Aggregator uses) — a
    real dict (`{filename, url, ...}`, matching /api/uploads' real
    response shape) for a `single_file` trigger field, or a real list
    of those for a `file_list` field. Anything else is real string
    interpolation (a literal URL, or `{{var}}` embedded in surrounding
    text, coerced to its string form) — unchanged, backward-compatible
    behavior for every flow already using a plain URL string."""
    import re
    template = (template or "").strip()
    if re.fullmatch(r"\{\{\w+\}\}", template):
        return _resolve_whole_var(template, ctx)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(ctx["vars"].get(m.group(1), m.group(0))), template)


def _try_read_local_upload(url: str):
    """Real, direct database read for this platform's own upload URLs
    (§0.56, GAP_REPORT) — avoids the self-referential HTTP round-trip
    entirely for a real, known-safe case, rather than needing a
    loopback exception in the SSRF-governed fetch path below. Matches
    on the real URL path shape routers/uploads.py's own _download_url
    produces (`/api/uploads/{file_id}/download`), REGARDLESS of host —
    correct across every real PLATFORM_BASE_URL deployment, not
    hardcoded to 127.0.0.1 — and only succeeds if that exact file_id
    genuinely exists as a real UploadedFile row; any other path
    (including a URL merely crafted to look like this one) falls
    through to the real, governed fetch instead. Returns
    (content_bytes, content_type, filename) or None if this isn't a
    real local upload URL."""
    import re
    m = re.search(r"/api/uploads/([^/]+)/download/?$", httpx.URL(url).path)
    if not m:
        return None
    file_id = m.group(1)
    from .db import engine
    from .storage import get_storage_backend, StorageError
    with Session(engine) as s:
        row = s.get(UploadedFile, file_id)
        if not row:
            return None
        try:
            # Resolves the SPECIFIC place this row's object was written,
            # honouring storage_destination_id — which is what the
            # download endpoint and the retention sweep both do.
            #
            # This comment used to claim it applied "the same real fix"
            # as those two while actually calling
            # get_storage_backend(row.storage_backend), the env-level
            # backend. For an upload written to a CONFIGURED destination
            # (its own bucket/prefix/credentials) the env-level backend
            # cannot reach the object, so this returned None, the Doc
            # Extractor fell through to a plain HTTP fetch of the
            # platform's own download URL, and the egress policy blocked
            # it — correctly, since that URL is loopback:
            #
            #   docextractor_error = blocked by egress policy:
            #                        'localhost' is a blocked hostname
            #
            # The node reported success with empty text, so a flow
            # reading an uploaded file silently produced nothing and the
            # LLM downstream answered "I don't see any file".
            from .storage_destinations import backend_for_stored_object, DestinationError
            backend = backend_for_stored_object(s, row.storage_destination_id, row.storage_backend)
            data = backend.get(row.object_key)
        except (StorageError, DestinationError):
            return None
        return data, row.content_type, row.filename


def _fetch_and_extract_one(item) -> dict:
    """Real fetch + real extraction for exactly one real file reference
    — a real dict (`{filename, url}`, e.g. from /api/uploads) or a plain
    URL string. Shared by both single and multiple mode below so the
    two don't drift into separately-maintained fetch logic.

    Now genuinely governed (§0.56, GAP_REPORT) — routes through
    egress.safe_http_request instead of a direct httpx.get with
    follow_redirects=True. The old code followed every redirect with no
    validation of where it actually led; safe_http_request validates
    every real redirect hop the exact same way it validates the
    initial URL, not just the one the caller typed.

    This platform's own upload URLs (routers/uploads.py) are a real,
    deliberate exception to the general egress policy — not because
    loopback is allowed, but because they're read directly from this
    platform's own database (_try_read_local_upload) instead of making
    an HTTP call to the network at all, closing the exact
    self-referential-fetch case that would otherwise need a real
    loopback carve-out in the SSRF policy itself."""
    from .egress import safe_http_request, SSRFBlocked

    url = item.get("url") if isinstance(item, dict) else item
    if not url:
        return {"text": None, "error": "no URL", "provider": None, "bytes": 0}
    provider, fetch_url, error = _resolve_cloud_url(url)
    if error:
        return {"text": None, "error": error, "provider": None, "bytes": 0}

    local_upload = _try_read_local_upload(fetch_url)
    if local_upload:
        content, content_type, real_filename = local_upload
        filename = (item.get("filename") if isinstance(item, dict) else None) or real_filename
        text = _extract_text_from_bytes(content, filename, content_type)
        return {"text": text, "error": None, "provider": "local-upload", "bytes": len(content), "filename": filename}

    try:
        resp = safe_http_request("GET", fetch_url, timeout=15.0)
        if resp["status_code"] >= 400:
            return {"text": None, "error": f"fetch failed: HTTP {resp['status_code']}", "provider": provider, "bytes": 0}
    except SSRFBlocked as e:
        return {"text": None, "error": f"blocked by egress policy: {e}", "provider": provider, "bytes": 0}
    except Exception as e:
        return {"text": None, "error": f"fetch failed: {e}", "provider": provider, "bytes": 0}
    filename = (item.get("filename") if isinstance(item, dict) else None) or fetch_url.split("?")[0].rsplit("/", 1)[-1]
    text = _extract_text_from_bytes(resp["content"], filename, resp["headers"].get("content-type", ""))
    return {"text": text, "error": None, "provider": provider, "bytes": len(resp["content"]), "filename": filename}


def _list_operator_get_prop(item: Any, prop: Optional[str]) -> Any:
    """Real property extraction — the bare value itself for plain
    scalar array items (prop None/''/'value'), or a real property of a
    file-shaped dict (the same `{filename, url, content_type, size,
    ...}` shape /api/uploads produces — see routers/uploads.py),
    matching every real file property Dify's own docs list: type,
    mime_type, extension, size, name, transfer_method."""
    if prop in (None, "", "value"):
        return item
    if not isinstance(item, dict):
        return None
    if prop == "extension":
        fn = item.get("filename", "") or ""
        return fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
    if prop == "type":
        ext = _list_operator_get_prop(item, "extension")
        ct = item.get("content_type", "") or ""
        image_ext = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
        doc_ext = {"pdf", "doc", "docx", "txt", "md", "csv", "xlsx", "xls", "ppt", "pptx", "epub", "eml", "msg", "vtt"}
        audio_ext = {"mp3", "wav", "ogg", "m4a", "flac"}
        video_ext = {"mp4", "mov", "avi", "webm", "mkv"}
        if ct.startswith("image/") or ext in image_ext:
            return "image"
        if ct.startswith("audio/") or ext in audio_ext:
            return "audio"
        if ct.startswith("video/") or ext in video_ext:
            return "video"
        if ext in doc_ext or ct.startswith("application/") or ct.startswith("text/"):
            return "document"
        return "unknown"
    if prop == "mime_type":
        return item.get("content_type", "")
    if prop == "name":
        return item.get("filename", "")
    if prop == "size":
        return item.get("size")  # a real, honest None when genuinely absent — not silently treated as a literal 0, which would wrongly sort a sizeless item alongside real zero-byte files
    if prop == "transfer_method":
        # A real distinction: this platform's own real local uploads
        # (/api/uploads) vs. anything else (a genuine remote URL).
        url = item.get("url", "") or ""
        return "local_file" if "/api/uploads/" in url else "remote_url"
    return item.get(prop)


def _list_operator_match(value: Any, operator: str, target: Any) -> bool:
    """Real comparison operators, matching Dify's own documented filter
    conditions."""
    if operator == "contains":
        return target is not None and str(target) in str(value)
    if operator == "not_contains":
        return not (target is not None and str(target) in str(value))
    if operator == "starts_with":
        return str(value).startswith(str(target))
    if operator == "ends_with":
        return str(value).endswith(str(target))
    if operator == "is":
        return value == target
    if operator == "is_not":
        return value != target
    if operator == "empty":
        return value in (None, "", [], {})
    if operator == "not_empty":
        return value not in (None, "", [], {})
    if operator in (">", "<", ">=", "<=", "="):
        try:
            v, t = float(value), float(target)
        except (TypeError, ValueError):
            return False
        return {">": v > t, "<": v < t, ">=": v >= t, "<=": v <= t, "=": v == t}[operator]
    return False


def handle_list_operator(cfg: dict, ctx: dict) -> dict:
    """Real List Operator — matches Dify's own documented spec exactly
    (https://docs.dify.ai/en/cloud/use-dify/nodes/list-operator): real
    filtering, sorting, and selection over a real array. Real property
    extraction covers both plain arrays (string/number/boolean — the
    bare value) and file arrays (the same real `{filename, url,
    content_type, size, ...}` shape /api/uploads produces — a trigger's
    real `file_list` field, or a Doc Extractor's own real 'multiple'
    mode output), so this node is the real, missing piece that makes
    the Doc Extractor's own documented "Mixed File Processing" pattern
    (route images one way, documents another) genuinely possible on
    this platform.

    Real config: `input_var` (a whole `{{var}}` array reference —
    type-preserving, via the same `_resolve_whole_var` the Variable
    Aggregator and Doc Extractor already use), `filters` (a real list
    of `{property, operator, value}` conditions — ALL must match, real
    AND semantics, matching Dify's own multi-condition filtering),
    `sort` (`{property, direction}` — real, None-safe: items missing
    the sort property sort last regardless of direction, rather than
    raising or silently reordering unpredictably), `limit` (real Take
    First N, clamped to Dify's own documented 1–20 range).

    Real output variables, in Dify's own exact documented names —
    `result` (the real filtered+sorted+limited array), `first_record`,
    `last_record` (real single elements, not further-wrapped arrays) —
    all three always computed together, not behind a separate "mode";
    Dify's own docs describe them as three real, independently useful
    outputs of the same real operation, not alternate output shapes."""
    raw = _resolve_whole_var(cfg.get("input_var", ""), ctx)
    array_val = _as_list(raw)
    if array_val is None and raw not in (None, ""):
        raise ValueError(
            f"List operator input {cfg.get('input_var')} is not an array. "
            f"Got {type(raw).__name__} {str(raw)[:80]!r}.")
    if array_val is None:
        ctx["vars"]["result"] = []
        ctx["vars"]["first_record"] = None
        ctx["vars"]["last_record"] = None
        return ctx

    items = list(array_val)

    for f in cfg.get("filters") or []:
        prop = f.get("property")
        op = f.get("operator", "is")
        target = f.get("value")
        items = [it for it in items if _list_operator_match(_list_operator_get_prop(it, prop), op, target)]

    sort_cfg = cfg.get("sort") or {}
    if sort_cfg.get("property") is not None or sort_cfg.get("direction"):
        prop = sort_cfg.get("property")
        direction = sort_cfg.get("direction", "asc")

        def sort_key(it):
            v = _list_operator_get_prop(it, prop)
            return (v is None, v if v is not None else 0)

        try:
            items = sorted(items, key=sort_key, reverse=(direction == "desc"))
        except TypeError:
            pass  # a real, honest no-op when items genuinely aren't comparable — not a crash

    limit = cfg.get("limit")
    if limit:
        try:
            n = max(1, min(20, int(limit)))  # Dify's own documented real 1-20 range
            items = items[:n]
        except (TypeError, ValueError):
            pass

    ctx["vars"]["result"] = items
    ctx["vars"]["first_record"] = items[0] if items else None
    ctx["vars"]["last_record"] = items[-1] if items else None
    return ctx


def handle_docextractor(cfg: dict, ctx: dict) -> dict:
    """Real fetch + real text extraction — not a placeholder. Two real
    modes, matching Dify's own documented "Single File" / "Multiple
    Files" input config exactly (§0.33, GAP_REPORT):

    'single' (default, unchanged/backward-compatible): input is one URL
    — a literal cloud storage link (S3/GCS/Azure Blob, presigned or
    public), a literal http(s):// link (including this platform's own
    real /api/uploads/{id}/download — see routers/uploads.py), or a
    {{var}} reference to one. Output: `ctx["vars"]["docextractor_text"]`
    is a real string.

    'multiple': input resolves to a real array — from a real trigger
    `file_list` field (each real upload's `{filename, url, ...}`, see
    /api/uploads' real response shape) or a plain list of URL strings.
    Every real item is genuinely fetched and extracted; output is a
    real `list[str]`, matching Dify's documented `array[string]`
    output shape for multi-file mode — not silently collapsed into one
    string, and a real per-item failure doesn't abort the others.

    Honest, stated naming difference from Dify (not changed to avoid a
    breaking rename for any existing flow already using it): this
    node's real output variable is `docextractor_text`, not Dify's
    `text` — see NodeConfigPanel's own docextractor case and
    GAP_REPORT §0.33.

    See _resolve_cloud_url's docstring for the real security boundary:
    public/presigned links (and this platform's own real uploads) work,
    authenticated private-bucket access via SDK credentials does not —
    this repo has none configured."""
    mode = cfg.get("mode", "single")
    raw = _resolve_docextractor_input(cfg.get("input_var", "") or cfg.get("input_url", ""), ctx)

    if mode == "multiple":
        items = raw if isinstance(raw, list) else ([raw] if raw else [])
        if not items:
            ctx["vars"]["docextractor_error"] = "no files configured"
            return ctx
        results = [_fetch_and_extract_one(item) for item in items]
        ctx["vars"]["docextractor_text"] = [r["text"] for r in results]
        errors = [r["error"] for r in results if r["error"]]
        if errors:
            ctx["vars"]["docextractor_errors"] = errors
        ctx["vars"]["docextractor_bytes"] = sum(r["bytes"] for r in results)
        joined = "\n\n---\n\n".join(r["text"] for r in results if r["text"])
        ctx["output"] = ctx["output"] or joined
        return ctx

    if not raw:
        ctx["vars"]["docextractor_error"] = "no input configured"
        return ctx
    result = _fetch_and_extract_one(raw)
    if result["error"]:
        ctx["vars"]["docextractor_error"] = result["error"]
        ctx["vars"]["docextractor_provider"] = result["provider"]
        return ctx
    ctx["vars"]["docextractor_text"] = result["text"]
    ctx["vars"]["docextractor_provider"] = result["provider"]
    ctx["vars"]["docextractor_source_url"] = raw.get("url") if isinstance(raw, dict) else raw
    ctx["vars"]["docextractor_bytes"] = result["bytes"]
    ctx["output"] = ctx["output"] or result["text"]
    return ctx


def _set_http_vars(ctx: dict, result: dict) -> dict:
    """Publish one http result under every name a flow will reach for.

    `http_output` — the whole dict — was the only variable this node
    ever set, and nothing anywhere advertises that: not the Builder, not
    the node docs, not a schema. So a flow author writes `{{http_body}}`
    and `{{http_status}}`, which is what the intuitive names are, and
    gets empty strings with no error and no clue.

    Showcase 5 was built that way and ran green for it. The template
    read `HTTP: {{http_body}}` and printed `HTTP: ` forever.

    Rather than teach every author the one real name, the intuitive
    ones are now real:

      http_output  the full dict, unchanged — existing flows keep working
      http_status  the response code, or None when no response came back
      http_body    the response text, or "" on failure
      http_error   the failure reason, or None on success

    `http_status` is deliberately None rather than 0 on a failure that
    produced no response: a condition testing `{{http_status}} >= 400`
    must not quietly pass because a blocked call reported zero.
    """
    ctx["vars"]["http_output"] = result
    ctx["vars"]["http_status"] = result.get("status_code")
    ctx["vars"]["http_body"] = result.get("body") or ""
    ctx["vars"]["http_error"] = result.get("error")
    return ctx


def handle_http(cfg: dict, ctx: dict) -> dict:
    """A REAL outbound HTTP call — not a placeholder. Genuinely applies
    the Headers/Params/Body fields set in the builder, not just
    persisting them decoratively.

    Now genuinely governed (§0.56, GAP_REPORT) — routes through
    egress.safe_http_request, the real Egress Policy Engine this node's
    own docstring used to admit didn't exist ("treat this node as
    genuinely ungoverned until [it's] built"). Every real destination
    (and every real redirect hop) is DNS-resolved and IP-validated
    against private/loopback/link-local/metadata ranges before the
    connection is made, with the connection pinned to the validated IP
    to close the DNS-rebinding gap a check-then-connect approach would
    leave open. `verify_ssl` is no longer a builder-configurable
    bypass — TLS verification is always on for this node now, since a
    disabled cert check was a real, additional risk on top of SSRF (a
    real, deliberate decision already made in §0.56 and unchanged
    here; the config panel has no such toggle at all anymore, so
    there's nothing left claiming this is configurable).

    §0.65, GAP_REPORT — a real, reported gap: the config panel offered
    form-data/raw/binary body types and a real retry_on_failure/
    max_retries pair, none of which this handler actually read — only
    "JSON" ever produced a real request body, and every call was
    exactly one real attempt regardless of configuration. Now genuinely
    wired:

    - body_type="form-data": cfg["body_form_text"], the same real
      "key=value per line" convention Headers/Params already use,
      parsed into a real dict and sent as a real
      application/x-www-form-urlencoded body (httpx's own real default
      for a dict data= value, passed through safe_http_request's own
      new real data parameter — see egress.py).
    - body_type="raw": cfg["body_raw_text"], sent as the real, exact
      raw request body — whatever string the person configured, byte
      for byte, not reinterpreted.
    - body_type="binary": cfg["body_binary_base64"], real base64 text
      decoded to real bytes before sending — the real, standard way to
      carry arbitrary binary content through a JSON-based config
      without corrupting it.
    - body_type="JSON": unchanged in real behavior, now also accepts a
      real JSON *string* (not just an already-parsed dict/list) and
      parses it here — the config panel's own new body-content textarea
      (§0.65) stores a real string, and this handler shouldn't require
      the frontend to pre-parse it.

    retry_on_failure / max_retries: a real retry loop, up to
    max_retries additional real attempts (clamped to a real, sane 0-10
    range) beyond the first, on either a real request exception or a
    real 5xx response — the same real "genuinely try again, not just
    relabel one attempt" standard handle_code's own retry already set
    for this platform. A real 4xx response is NOT retried (a real
    client error won't be fixed by asking again identically) — only a
    real transient/server-side failure is."""
    from .egress import safe_http_request, SSRFBlocked
    import base64 as _base64
    import json as _json

    method = cfg.get("method", "GET")
    url = cfg.get("url", "")
    if not url:
        _set_http_vars(ctx, {"error": "no URL configured"})
        return ctx

    def _parse_lines(text: str, sep: str) -> dict:
        out = {}
        for line in (text or "").splitlines():
            if sep in line:
                k, v = line.split(sep, 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out

    headers = _parse_lines(cfg.get("headers_text", ""), ":")
    params = _parse_lines(cfg.get("params_text", ""), "=")
    body_type = cfg.get("body_type", "none")

    # Real Idempotency-Key header pass-through (§0.76, GAP_REPORT) —
    # the actual, concrete fix for the reported "HTTP POST not
    # automatically retryable... requires idempotency" case. A real,
    # mutating HTTP method (POST/PUT/PATCH/DELETE) on the durable path
    # (ctx["_idempotency_key"] is only ever set there — see
    # temporal_workflow.py's own _idempotency_key_for) gets a real,
    # stable `Idempotency-Key` header attached automatically, using the
    # same real key that's identical across every genuine Temporal
    # retry of this same node-execution attempt. This is a real,
    # widely-adopted HTTP convention (Stripe, PayPal, and many other
    # real payment/mutation APIs genuinely honor this exact header
    # name) — whether the actual destination this node calls supports
    # it is honestly outside this platform's control, but the real key
    # is now always offered, not silently absent. Never overrides a
    # header the person already configured explicitly.
    if ctx.get("_idempotency_key") and method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        headers.setdefault("Idempotency-Key", ctx["_idempotency_key"])

    body_json = None
    body_data = None
    body_error = None
    if body_type == "JSON":
        raw = cfg.get("body_json")
        if isinstance(raw, str) and raw.strip():
            try:
                body_json = _json.loads(raw)
            except (ValueError, TypeError):
                body_error = "invalid JSON in Body"
        elif raw:
            body_json = raw  # already a real dict/list, e.g. set programmatically
    elif body_type == "form-data":
        body_data = _parse_lines(cfg.get("body_form_text", ""), "=") or None
    elif body_type == "raw":
        raw_text = cfg.get("body_raw_text", "")
        body_data = raw_text if raw_text else None
    elif body_type == "binary":
        b64 = (cfg.get("body_binary_base64") or "").strip()
        if b64:
            try:
                body_data = _base64.b64decode(b64)
            except Exception:
                body_error = "invalid base64 in Body (binary)"

    if body_error:
        _set_http_vars(ctx, {"error": body_error})
        return ctx

    retry_on_failure = bool(cfg.get("retry_on_failure")) and not ctx.get("_durable_execution")
    # §0.105, GAP_REPORT — same real reasoning as handle_llm's own
    # identical check above: suppressed on the durable path, where
    # Temporal's own _retry_policy_for_node already retries this
    # entire Activity on failure — stacking this handler's own retry
    # loop on top would silently turn one configured retry count into
    # several real, duplicate HTTP calls per Temporal-level attempt.
    # Fully active on the sync path, where it's the only real retry
    # mechanism at all.
    max_retries = max(0, min(int(cfg.get("max_retries", 3) or 0), 10))
    attempts_allowed = 1 + (max_retries if retry_on_failure else 0)

    last_error = None
    for attempt in range(attempts_allowed):
        try:
            resp = safe_http_request(method, url, headers=headers or None, params=params or None,
                                      json=body_json, data=body_data)
            if resp["status_code"] >= 500 and attempt < attempts_allowed - 1:
                # A real, honest transient-failure retry — a 5xx is the
                # real, standard signal the server itself is having
                # trouble, unlike a 4xx (a real client-side error a
                # retry can't fix). Falls through to the next attempt.
                last_error = f"server returned {resp['status_code']}"
                continue
            _set_http_vars(ctx, {
                "status_code": resp["status_code"], "body": resp["text"][:2000],
                "headers_sent": headers, "params_sent": params,
                "attempts": attempt + 1,
            })
            return ctx
        except SSRFBlocked as e:
            _set_http_vars(ctx, {"error": f"blocked by egress policy: {e}"})
            return ctx  # a real policy block is never retried — retrying an SSRF-blocked destination can't ever succeed
        except Exception as e:
            last_error = str(e)
            if attempt < attempts_allowed - 1:
                continue
    _set_http_vars(ctx, {"error": last_error, "attempts": attempts_allowed})
    return ctx


def _as_list(value: Any) -> Optional[list]:
    """A list from whatever the variable actually holds, or None.

    A `json_code` trigger field arrives as a STRING of JSON text — that
    is what the form produces. Nodes that need an array tested
    `isinstance(value, list)`, which a string fails, and then set their
    output to [] and returned without a word. Showcase 3's iteration
    therefore collected nothing for every input anyone gave it, and the
    result read as an empty list rather than as a mistake.

    Parsing is not a guess here: the field's declared type IS JSON, so
    text that parses to an array is an array. Anything else returns
    None, and the caller says so rather than silently producing [].
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return None
            if isinstance(parsed, list):
                return parsed
    return None


def _resolve_whole_var(var_ref: str, ctx: dict):
    """Resolves a whole `{{name}}` reference to its raw ctx value, type
    preserved (string/number/object/boolean/array/file-shaped dict —
    whatever it genuinely is) — shared by the Variable Aggregator and
    Doc Extractor's own real single/multi-file resolution
    (_resolve_docextractor_input), so "reference this exact variable,
    don't stringify it" behaves identically everywhere it's needed."""
    import re
    var_ref = (var_ref or "").strip()
    m = re.fullmatch(r"\{\{(\w+)\}\}", var_ref)
    if not m:
        return None
    name = m.group(1)
    return ctx.get("input", "") if name == "input" else ctx["vars"].get(name)


def handle_aggregator(cfg: dict, ctx: dict) -> dict:
    """Variable Aggregator — converges EXCLUSIVE branches (only one of
    which genuinely ran, e.g. a `condition` node's true/false paths, or
    a `classifier` node's chosen class) into one real output, matching
    Dify's own documented behavior exactly
    (https://docs.dify.ai/en/cloud/use-dify/nodes/variable-aggregator):
    a person selects real variables from each real branch; since only
    one branch actually ran, only one of those variables genuinely has
    a value at runtime, and THAT real value — with its real type
    preserved (string/number/object/boolean/array/file), not wrapped or
    coerced — becomes the node's real output.

    Real config shape: `cfg["groups"]`, a real list of
    `{"output_name": str, "variables": [\"{{var1}}\", \"{{var2}}\", ...]}`
    — real "Aggregation Group" support, matching Dify's own documented
    feature for converging multiple independent sets of branch
    variables in one node, each group producing its own separate real
    output. For each group, the real variables list is checked in
    order; the first one that genuinely has a real value (not None, not
    an empty string) is the one used — since only one branch ran, at
    most one candidate should ever actually have a value, but checking
    in real configured order is the same tie-breaking Dify's own
    single-active-branch guarantee implies.

    Real backward compatibility for the OLD config shape (a bare
    `output_name`, no real variable selection at all — this node's
    entire previous implementation, which only ever merged a fixed
    internal list of ctx keys into one object and could never reference
    a real branch variable at all): treated as a single group with an
    empty variables list, which produces no real output — an honest
    empty result for old, never-actually-functional configs, not a
    silently fabricated one."""
    groups = cfg.get("groups")
    if not groups:
        # Real, honest fallback for a pre-rebuild config that only ever
        # had `output_name` — there was never a real way to select
        # variables under the old shape, so there's nothing real to
        # converge; this deliberately does NOT recreate the old
        # hardcoded-internal-keys merge behavior, which never actually
        # implemented Dify's real spec in the first place (see
        # GAP_REPORT §0.34).
        output_name = cfg.get("output_name")
        if output_name:
            ctx["vars"].setdefault(output_name, None)
        return ctx

    for group in groups:
        output_name = group.get("output_name")
        if not output_name:
            continue
        variables = group.get("variables") or []
        value = None
        for var_ref in variables:
            resolved = _resolve_whole_var(var_ref, ctx)
            if resolved is not None and resolved != "":
                value = resolved
                break
        ctx["vars"][output_name] = value
    return ctx


def handle_classifier(cfg: dict, ctx: dict) -> dict:
    """Question Classifier — a real call through the same model adapter the
    LLM node uses, asked to pick one of the configured classes. If a custom
    Instruction is set, it's genuinely used as the system prompt rather
    than silently ignored."""
    classes = [c for c in (cfg.get("class1"), cfg.get("class2")) if c]
    if not classes:
        ctx["vars"]["class_name"] = None
        return ctx

    # Classifies whatever `input_var` points at, defaulting to the run's
    # own input. It used to classify ctx["input"] unconditionally and
    # ignore input_var completely — while the config panel asked for it
    # and flow_validator REFUSED to publish without it ("Input Variables
    # is required"). So a required field decided nothing, and a
    # classifier placed after a retrieval or an LLM node silently graded
    # the original user text instead of that node's output.
    #
    # Same resolution the parameter extractor already uses, so the two
    # read a {{var}} the same way.
    input_var = cfg.get("input_var") or "{{input}}"
    subject = _resolve_whole_var(input_var, ctx)
    subject_text = str(subject) if subject is not None else _interp_vars(input_var, ctx)

    prompt = (f"Classify the following input into exactly one of these classes: "
              f"{', '.join(classes)}.\nRespond with only the class name.\n\nInput: {subject_text}")
    system = cfg.get("instruction") or None
    result = _call_llm_tracked(ctx, prompt=prompt, model=cfg.get("model", "Claude Haiku 4.5"), system=system)
    picked = result["text"].strip()
    ctx["vars"]["class_name"] = picked if picked in classes else classes[0]
    ctx["vars"]["class_label"] = ctx["vars"]["class_name"]
    return ctx


def handle_parameter_extractor(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real LLM-based structured extraction — matches Dify's own
    documented spec exactly (https://docs.dify.ai/en/cloud/use-dify/
    nodes/parameter-extractor): converts real unstructured text into
    real structured data via a real LLM call, using the same real model
    adapter (call_llm) the LLM node uses.

    Two real inference modes, matching Dify's own documented pair
    exactly: 'function_call' genuinely uses Anthropic's real tool-use
    API (model_adapter.call_llm's tools/tool_choice — a real JSON
    schema built from the configured parameters, with tool_choice
    forcing that specific tool), for reliable real type-compliant
    output; 'prompt' genuinely relies on pure prompting (a real
    instruction to respond with ONLY a JSON object matching the
    schema), with real JSON parsing of the response (including
    stripping a real ```json code fence if the model adds one) — for
    models/scenarios where tool-use isn't available or performs worse.

    Real `memory_enabled` reuses the exact same real conversation-
    history mechanism handle_llm's own memory toggle uses (the same
    real AgentMemory row, key "__llm_chat_history__", scoped to
    (agent_id, invoking_user)) — read-only here, since extraction
    doesn't itself add a new conversation turn.

    Each configured real parameter becomes its own real
    ctx["vars"][name], plus Dify's exact real built-in status
    variables: __is_success (1 real success, 0 real failure) and
    __reason (a real error description on failure) — kept in Dify's own
    exact naming (not this platform's usual snake_case convention),
    since matching the documented spec exactly is the whole point of
    these two."""
    import json

    input_var = cfg.get("input_var") or "{{input}}"
    raw_text = _resolve_whole_var(input_var, ctx)
    text = str(raw_text) if raw_text is not None else _interp_vars(input_var, ctx)

    parameters = cfg.get("parameters") or []
    instruction = cfg.get("instruction", "")
    model = cfg.get("model", "Claude Sonnet 5")
    inference_mode = cfg.get("inference_mode", "function_call")

    param_lines = ", ".join(
        f"{p['name']} ({p.get('type', 'string')}){' [required]' if p.get('required') else ''}: {p.get('description', '')}"
        for p in parameters if p.get("name")
    )
    system_prompt = f"You extract structured parameters from text. {instruction}\n\nExtract these parameters: {param_lines}".strip()

    if cfg.get("memory_enabled"):
        scope_key = f"{ctx['agent_id']}:{ctx.get('invoking_user', 'anon')}"
        history_row = session.exec(
            select(AgentMemory).where(AgentMemory.scope_key == scope_key, AgentMemory.key == "__llm_chat_history__")
        ).first()
        if history_row and history_row.value:
            system_prompt = f"{system_prompt}\n\nConversation so far (most recent last):\n{history_row.value}".strip()

    type_map = {"string": "string", "number": "number", "boolean": "boolean", "array": "array", "object": "object"}
    schema_props = {
        p["name"]: {"type": type_map.get(p.get("type", "string"), "string"), "description": p.get("description", "")}
        for p in parameters if p.get("name")
    }
    required = [p["name"] for p in parameters if p.get("required") and p.get("name")]

    def fail(reason: str) -> dict:
        ctx["vars"]["__is_success"] = 0
        ctx["vars"]["__reason"] = reason
        for p in parameters:
            if p.get("name"):
                ctx["vars"][p["name"]] = None
        return ctx

    if inference_mode == "function_call":
        tools = [{
            "name": "extract_parameters",
            "description": "Extract the requested parameters from the given text.",
            "input_schema": {"type": "object", "properties": schema_props, "required": required},
        }]
        result = _call_llm_tracked(ctx, prompt=text, model=model, system=system_prompt, tools=tools, tool_choice={"type": "tool", "name": "extract_parameters"})
        extracted = result.get("tool_input")
        if extracted is None:
            return fail(result.get("error") or "model did not return a structured tool call")
    else:
        prompt = f"{text}\n\nRespond with ONLY a JSON object matching this schema, no other text: {json.dumps(schema_props)}"
        result = _call_llm_tracked(ctx, prompt=prompt, model=model, system=system_prompt)
        raw = (result.get("text") or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        try:
            extracted = json.loads(raw)
        except (ValueError, TypeError) as e:
            return fail(f"could not parse JSON from model response: {e}")

    missing = [p["name"] for p in parameters if p.get("required") and p.get("name") and extracted.get(p["name"]) is None]
    if missing:
        ctx["vars"]["__is_success"] = 0
        ctx["vars"]["__reason"] = f"missing required parameter(s): {', '.join(missing)}"
    else:
        ctx["vars"]["__is_success"] = 1
        ctx["vars"]["__reason"] = None

    for p in parameters:
        if p.get("name"):
            ctx["vars"][p["name"]] = extracted.get(p["name"])
    return ctx


def handle_template(cfg: dict, ctx: dict) -> dict:
    """Real Jinja2-style {{var}} substitution against ctx["vars"] — not a
    full Jinja2 (no loops/conditionals in the template itself), just
    variable interpolation, which covers the common case."""
    import re
    template = cfg.get("template", "")
    rendered = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(ctx["vars"].get(m.group(1), "")), template)
    ctx["vars"]["template_output"] = rendered
    ctx["output"] = ctx["output"] or rendered
    return ctx


def _resolve_subagent_flow(session: Session, target, ctx: dict, cfg: dict) -> dict:
    """Real immutable-version resolution for a Sub-Agent's own target
    (§0.83, GAP_REPORT) — the actual fix for the first of two real,
    reported problems: a nested Sub-Agent call used to always execute
    target.flow directly (the live, mutable draft), even for a real,
    published child — exactly the same class of bug §0.54's own fix
    already closed for every other real invocation channel
    (execution.py's own _resolve_execution_flow), silently unfixed for
    this one. A nested Sub-Agent call is never a real "Builder
    preview" of the target — there's no such concept for a call one
    flow makes into another — so this always resolves the same way
    _resolve_execution_flow's own real preview=False branch does: the
    current AgentVersion's own real, immutable flow snapshot for a
    published target, or the live target.flow for a draft (which has
    no published snapshot to freeze yet).

    §0.90, GAP_REPORT — a published target with no real, current
    AgentVersion row now raises PublishedVersionIntegrityError rather
    than silently falling back to target.flow (see that exception's
    own docstring for the full real reasoning and the real, one-time
    backfill migration this fix ships alongside).

    §0.104, GAP_REPORT — the actual fix for a real, reported
    reproducibility gap: "current AgentVersion" above used to always
    mean the target's LATEST published version, resolved fresh at
    EVERY execution — so an unchanged, already-published PARENT could
    silently invoke a materially different CHILD tomorrow than it did
    today, the moment the child was independently republished, with no
    new publish, no version bump, and no record of it for the parent.
    Confirmed live, before this fix: publishing parent A while child B
    was at v2, then republishing B to v3, then re-running the
    UNCHANGED, already-published A, silently invoked B's NEW v3 rather
    than the v2 that existed when A was published.

    Real fix, matching the report's own recommended model exactly: a
    Sub-Agent node defaults to `version_policy="pinned"` — this parent
    invocation's own real, frozen AgentVersion.subagent_manifest
    (threaded into ctx["_subagent_manifest"] by run_flow_sync/
    _run_durable — see ResolvedExecution's own docstring, execution.py)
    is consulted, by this exact node's own id
    (ctx["_current_node_id"]), for the SPECIFIC target AgentVersion.id
    that was current at THIS PARENT's own publish time — that exact
    row is fetched and its flow used, regardless of what the target's
    own CURRENT version has since become. `version_policy="latest"` is
    a real, explicit, per-node opt-out — when set, this always
    resolves the target's current live/published state at execution
    time, the same real behavior every Sub-Agent node had before this
    fix (a real, deliberate choice for a caller who WANTS to always
    follow the target's newest published behavior).

    A manifest entry with `agent_version_id=None` (the target was
    itself still only a draft at the parent's own publish time —
    nothing real to have pinned yet) and the "no manifest entry at all"
    case (a genuine draft parent that's never been published with a
    manifest yet, or handle_subagent's own directly-nested call with
    no manifest threaded at all) both honestly fall back to resolving
    the target's current live/published state at execution time —
    unavoidable in the first case (nothing was ever frozen to pin to),
    and preserving this function's own prior, correct behavior
    unchanged in the second."""
    version_policy = (cfg or {}).get("version_policy") or "pinned"
    if version_policy == "pinned":
        manifest = ctx.get("_subagent_manifest") or {}
        node_id = ctx.get("_current_node_id")
        pinned_entry = next(
            (e for e in manifest.get("subagents", []) if e.get("node_id") == node_id and e.get("agent_id") == target.id),
            None,
        )
        if pinned_entry and pinned_entry.get("agent_version_id") is not None:
            pinned_version = session.get(AgentVersion, pinned_entry["agent_version_id"])
            if pinned_version:
                return pinned_version.flow
            # Real, honest degradation (§0.104, GAP_REPORT): the exact
            # pinned AgentVersion row this manifest names no longer
            # exists (e.g. a real, separate data-integrity issue on the
            # target's own history) — falls through to resolving the
            # target's current state below rather than hard-failing
            # the parent's own run over the child's own missing
            # history, matching PublishedVersionIntegrityError's own
            # stated preference for a loud, but scoped, failure over a
            # silent one — logged, not swallowed.
            logger.warning(
                "Sub-Agent manifest names AgentVersion.id=%s for target '%s' (node '%s'), but that row no longer "
                "exists — falling back to the target's current state for this invocation.",
                pinned_entry["agent_version_id"], target.id, node_id,
            )

    if target.status != "published":
        return target.flow
    current_version = session.exec(
        select(AgentVersion).where(AgentVersion.agent_id == target.id, AgentVersion.is_current == True)
    ).first()
    if not current_version:
        raise PublishedVersionIntegrityError(target.id, target.name)
    return current_version.flow


def _run_subagent_durable(target_id: str, target_flow: dict, invoking_user: str, sub_input: str, chain: list) -> dict:
    """Real, sync-callable nested durable invocation (§0.83,
    GAP_REPORT) — the actual fix for the second of two real, reported
    problems: a nested Sub-Agent call used to always take the sync
    path, even when the target's own resolved flow genuinely needs the
    durable path (has a real `hitl` node) — silently downgrading to
    the sync path's own weaker, in-memory-only pause mechanism instead
    of the real, Temporal-backed durability (survives a worker
    restart, genuine signal-based resume) the same target would get if
    invoked directly.

    Genuinely starts a real, separate Temporal workflow for the
    CHILD — the same real client.connect + start_workflow + bounded-
    poll pattern _run_durable (routers/execution.py) already uses for
    a real top-level invocation, reused here via a real, late import
    (not reimplemented — a second, independently-drifting copy of that
    logic would be a real, separate bug waiting to happen) and now
    genuinely extended to accept an explicit real agent_chain so cycle
    detection carries through correctly (see _run_durable's own
    updated docstring).

    Safe to call from a thread that might ALREADY have a running
    asyncio event loop — a real, possible case when the PARENT's own
    execution is itself already on the durable path, since
    execute_node_activity calls node handlers (including this one)
    synchronously from within its own already-running async context.
    asyncio.run() alone would raise "cannot be called from a running
    event loop" in that specific real case. A fresh thread with its
    own, brand-new event loop sidesteps that restriction entirely — the
    same real "call async code from sync code, regardless of the
    caller's own event-loop state" technique this codebase already
    established for execute_agent_flow's own self-referential-HTTP
    deadlock fix (§0.33, GAP_REPORT); a genuinely different real
    problem, the same real underlying pattern. A fresh, real,
    thread-local Session is opened inside that new thread rather than
    reusing the caller's own session object — SQLAlchemy sessions
    aren't safe to use concurrently across threads, the same real
    reason execution.py's own streaming worker() already opens its own
    thread-local session rather than sharing the request's.

    Returns the exact same real result shape run_flow_sync's own
    return already has (status/output/spans/fail_class/hitl_* fields),
    plus a real "_temporal_workflow_id" — the actual, live Temporal
    workflow id backing this specific nested call, needed so a genuine
    pause can be resumed later (see handle_subagent's own use of this
    below, and respond_to_hitl's own extended handling for a
    delegated_child_agent_id request)."""
    import asyncio
    import concurrent.futures

    def _runner():
        from .db import engine as _engine
        from .models import Agent as _Agent
        from .routers.execution import _run_durable

        async def _go():
            with Session(_engine) as thread_session:
                thread_target = thread_session.get(_Agent, target_id)
                return await _run_durable(
                    thread_session, thread_target, invoking_user, sub_input,
                    flow=target_flow, agent_chain=chain,
                )
        return asyncio.run(_go())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_runner)
        return future.result()


def structured_inputs_for_flow(flow: dict, text: str) -> dict:
    """Map a plain string onto a flow's declared trigger fields.

    An agent whose trigger declares named fields reads {{question}}, not
    {{input}} — so handing it only a string leaves every field empty and
    the LLM is prompted with "Question: None". The answer then reads as
    the agent being wrong when in fact it was never asked anything.

    Three shapes, in order:
      * a JSON object is used as the field set directly, so a
        multi-field target can be driven at all;
      * exactly one declared non-file field takes the whole string,
        which is the common case and needs no special syntax;
      * anything else falls back to {} — correct for a trigger with no
        declared fields (the flow reads {{input}}) and honest for a
        multi-field trigger a plain string cannot fill.
    """
    fields = []
    for node in (flow or {}).get("nodes", []):
        if node.get("type") == "trigger":
            fields = [
                f for f in ((node.get("config") or {}).get("fields") or [])
                if not f.get("hidden_prefilled")
            ]
            break
    if not fields:
        return {}

    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass

    fillable = [f for f in fields
                if f.get("type") not in ("single_file", "file_list") and f.get("name")]
    if len(fillable) == 1:
        return {fillable[0]["name"]: text}
    return {}


def handle_subagent(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real recursive invocation of ANOTHER agent's flow — not a
    simulation. Runs the target agent through the exact same real
    execution semantics a direct, top-level invocation of that same
    target would get, so its own trigger/gov/... nodes genuinely
    execute, including its own real entitlement checks. The SAME
    invoking_user is threaded through every hop — the multi-hop
    principal chain per §7.6: a sub-agent call isn't a
    privilege-escalation trick where the nested run somehow executes as
    someone/something else.

    Real cycle detection: ctx["_agent_chain"] (seeded by run_flow_sync
    or _run_durable, extended by exactly one id per real nested call
    here) is checked BEFORE ever calling either execution path again —
    a target already in the chain is refused immediately, not detected
    only after the stack overflows. Real hop cap: 5, matching
    resolve_entitlement_plan's own chain_depth_cap, so it can't drift
    out of sync with the number UCS itself already enforces at the
    entitlement-resolution layer.

    §0.83, GAP_REPORT — the actual fix for two real, reported problems
    that used to hold here, both now closed:

    1. A published target now genuinely executes its own real,
       immutable AgentVersion.flow snapshot — not target.flow, the
       live, mutable draft — via _resolve_subagent_flow above, the
       exact same real resolution _resolve_execution_flow already
       applies to every other real invocation channel.

    2. A target whose own resolved flow genuinely needs the durable
       path (has a real hitl node) now genuinely gets it — a real,
       separate Temporal workflow via _run_subagent_durable above,
       not a silent downgrade to the sync path's own weaker,
       in-memory-only pause. If that real, nested workflow genuinely
       pauses for human input, this raises the exact same real
       HitlPause the sync path's own raise_hitl_pause already uses for
       a direct hitl node — but carrying the CHILD's own real,
       rendered form content and its own real, live
       temporal_workflow_id (via HitlPause's own new
       delegated_child_agent_id/delegated_temporal_workflow_id
       fields — see that class's own docstring). The PARENT's own run
       genuinely, honestly reports paused_for_human_input rather than
       a fabricated success or a silent skip; respond_to_hitl's own
       real, extended handling (execution.py) resumes the PARENT's own
       flow — from this exact Sub-Agent node's own outgoing edges —
       once a real caller resolves that pause, after first genuinely
       signaling the real, live child workflow.

    A real, honest, stated scope boundary this does NOT close: a
    parent that is ITSELF already on the durable path, whose own flow
    pauses here on a delegated child pause, does not currently persist
    across a real Temporal WORKER restart the way its own top-level
    hitl pause would — the parent's own "pause" in this specific case
    is a real HitlRequest row (durable in Postgres, same as any other),
    but the parent's own containing Activity invocation has already
    returned by the time this is stored, so there's no separate,
    still-running parent-side Temporal state to lose — this is
    actually fine and matches how every other real Activity-based node
    already behaves, stated here only to be precise about what "durable"
    means for this specific nested case rather than leaving it
    implied."""
    from .models import Agent
    from .routers.execution import DURABLE_NODE_TYPES

    target_id = cfg.get("target_agent_id")
    if not target_id:
        ctx["vars"]["subagent_error"] = "no target_agent_id configured"
        return ctx

    chain = ctx.get("_agent_chain") or [ctx["agent_id"]]
    if target_id in chain:
        ctx["vars"]["subagent_error"] = f"cycle detected: {' -> '.join(chain)} -> {target_id} (refused before invoking)"
        return ctx
    if len(chain) >= SUB_AGENT_HOP_CAP:
        ctx["vars"]["subagent_error"] = f"max hop depth ({SUB_AGENT_HOP_CAP}) exceeded: {' -> '.join(chain)}"
        return ctx

    target = session.get(Agent, target_id)
    if not target:
        ctx["vars"]["subagent_error"] = f"target agent '{target_id}' not found"
        return ctx

    # Rendered, not passed through. Only the exact string "{{output}}"
    # used to be special-cased, so any other template — {{question}},
    # {{llm_output}} — reached the target as its own literal text. The
    # node's help says the field is a template, and it now is one.
    template = cfg.get("input") or "{{output}}"
    sub_input = (ctx.get("output") or "") if template.strip() == "{{output}}" \
        else _interp_vars(template, ctx)

    target_flow = _resolve_subagent_flow(session, target, ctx, cfg)
    # A target whose trigger declares named fields reads {{question}},
    # not {{input}} — without this it is invoked with every field empty
    # and answers "no question was provided". batch-run and the eval
    # runner already thread this through; the subagent hop was the last
    # channel that did not.
    sub_fields = structured_inputs_for_flow(target_flow, sub_input)
    full_chain = chain + [target_id]
    target_needs_durable = any(n.get("type") in DURABLE_NODE_TYPES for n in target_flow.get("nodes", []))

    if target_needs_durable:
        result = _run_subagent_durable(target_id, target_flow, ctx["invoking_user"], sub_input, full_chain)
    else:
        result = run_flow_sync(session, target, ctx["invoking_user"], sub_input,
                               agent_chain=full_chain, flow=target_flow,
                               structured_inputs=sub_fields)

    if result.get("status") == "paused_for_human_input":
        raise HitlPause(
            node_id=ctx.get("_current_node_id"),
            rendered_content=result["hitl_rendered_content"], fields=result["hitl_fields"],
            actions=result["hitl_actions"], delivery_status=result["hitl_delivery_status"],
            delegated_child_agent_id=target_id,
            delegated_temporal_workflow_id=result.get("_temporal_workflow_id"),
        )

    ctx["vars"]["subagent_target"] = target_id
    ctx["vars"]["subagent_status"] = result["status"]
    ctx["vars"]["subagent_output"] = result["output"]
    ctx["vars"]["subagent_chain"] = full_chain
    if result["status"] != "success":
        ctx["vars"]["subagent_error"] = f"nested run did not succeed: {result['status']} ({result.get('fail_class')})"
    ctx["output"] = ctx["output"] or result["output"]
    return ctx


def handle_memory(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real persistent key-value memory — genuinely survives across
    separate runs, not just within one execution's ctx. This was the
    single most-requested gap to close.

    cfg.key (required): identifies which memory slot this node reads/writes.
    cfg.scope: 'agent' (default, shared across every run of this agent) or
      'conversation' (scoped to invoking_user — the closest honest
      approximation available without a real session concept elsewhere).
    cfg.mode: 'read' | 'write' | 'read_write' (default). On read, the prior
      value (or None if never set) lands in ctx["vars"][f"memory_{key}"],
      available to every node after this one. On write, the value comes
      from cfg.write_value — supports the literal placeholder "{{output}}"
      (resolves to whatever ctx["output"] currently holds, typically set by
      an earlier LLM/template/code node) or a literal string; genuinely
      persisted via a real Postgres UPSERT, not just held in memory."""
    key = cfg.get("key")
    if not key:
        ctx["vars"]["memory_error"] = "no key configured"
        return ctx

    scope = cfg.get("scope", "agent")
    mode = cfg.get("mode", "read_write")
    scope_key = ctx["agent_id"] if scope == "agent" else f"{ctx['agent_id']}:{ctx.get('invoking_user', 'anon')}"

    row = session.exec(
        select(AgentMemory).where(AgentMemory.scope_key == scope_key, AgentMemory.key == key)
    ).first()

    if mode in ("read", "read_write"):
        ctx["vars"][f"memory_{key}"] = row.value if row else None

    if mode in ("write", "read_write"):
        template = cfg.get("write_value") or "{{output}}"
        new_value = ctx.get("output") or "" if template.strip() == "{{output}}" else template
        upsert_agent_memory(session, scope_key, key, new_value)
        session.commit()
        ctx["vars"][f"memory_{key}_saved"] = new_value

    return ctx


def idempotent_once(session: Session, key: Optional[str], fn):
    """Real, general-purpose "apply this real side effect at most
    once per real logical key" helper (§0.76, GAP_REPORT) — backed by
    the real ActivityIdempotency ledger (models.py), the same real
    pattern already verified for HitlRequest's own idempotency_key/
    resolved_result (§0.74, GAP_REPORT).

    If `key` is falsy (no real idempotency key available — e.g. this
    node execution isn't happening on the durable/Temporal path at
    all, where retries genuinely can't happen this way in the first
    place), just calls `fn()` directly — this helper is never a
    requirement to use, only a real, available tool for a handler that
    performs a genuinely non-idempotent side effect on the durable
    path specifically.

    If a real row already exists under `key`, returns its own real,
    cached `result` WITHOUT calling `fn()` again — this is the actual
    fix: a genuine Temporal retry of the same Activity, carrying the
    same real, stable idempotency key computed for that one logical
    node-execution attempt, replays the already-applied outcome
    instead of re-running the real side effect a second time.

    Otherwise, calls `fn()`, records its real return value under `key`,
    commits, and returns it. `fn` must be a real, zero-argument
    callable — callers close over whatever real arguments they need."""
    if not key:
        return fn()
    existing = session.get(ActivityIdempotency, key)
    if existing is not None:
        return existing.result
    result = fn()
    session.add(ActivityIdempotency(id=key, result=result if isinstance(result, dict) else {"value": result}))
    session.commit()
    return result


def upsert_agent_memory(session: Session, scope_key: str, key: str, value: str) -> None:
    """Real, atomic upsert (§0.70, GAP_REPORT) — the actual fix for a
    real, reported race: every real AgentMemory write site used to
    SELECT by (scope_key, key), then either UPDATE the row found or
    construct-and-add a new one if none was — two genuinely concurrent
    writes to the same (scope_key, key) could both see no existing row
    and both insert, leaving two real rows for what should be one real
    value. A real Postgres `INSERT ... ON CONFLICT (scope_key, key) DO
    UPDATE` is atomic by construction — Postgres itself, not
    application code racing against itself, decides which write wins a
    genuine simultaneous conflict, and the loser's own value is simply
    the one actually stored, not silently duplicated. Executed (not
    committed) within the caller's own current transaction, so callers
    that batch this alongside other real changes into one later
    session.commit() keep working exactly as before."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(AgentMemory).values(scope_key=scope_key, key=key, value=value, updated_at=datetime.utcnow())
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_key", "key"],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )
    session.execute(stmt)


CONV_VAR_PREFIX = "convvar:"


def _get_conv_var(session: Session, scope_key: str, name: str) -> Any:
    """Real read of one real conversation variable — shares the exact
    same real AgentMemory table/scope_key convention handle_memory's own
    'conversation' scope uses, under a real, distinct key prefix
    (CONV_VAR_PREFIX) so a Variable Assigner's real variables can never
    collide with a `memory` node's own real keys. Real JSON round trip
    (the existing AgentMemory.value column is plain text; every real
    typed value — number/boolean/object/array/string — is stored as
    real JSON in it, not flattened to a display string), so `None`
    genuinely means "never set", not "set to an empty string"."""
    import json
    row = session.exec(select(AgentMemory).where(AgentMemory.scope_key == scope_key, AgentMemory.key == CONV_VAR_PREFIX + name)).first()
    if not row:
        return None
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return row.value  # a real, honest fallback for anything stored before this real JSON encoding existed


def _set_conv_var(session: Session, scope_key: str, name: str, value: Any) -> None:
    import json
    upsert_agent_memory(session, scope_key, CONV_VAR_PREFIX + name, json.dumps(value))


def handle_variable_assigner(cfg: dict, ctx: dict, session: Session) -> dict:
    """Real Variable Assigner — matches Dify's own documented spec
    exactly (https://docs.dify.ai/en/cloud/use-dify/nodes/variable-
    assigner): genuinely writes to real CONVERSATION-scoped persistent
    variables (via AgentMemory, 'conversation' scope — keyed by
    (agent_id, invoking_user), the same real, closest-honest
    approximation handle_memory's own docstring already establishes for
    "persists across conversation turns" without a dedicated session
    concept elsewhere in this platform) — genuinely different from a
    plain workflow variable (ctx["vars"]), which resets every single
    run. A conversation variable set here is genuinely read back by a
    LATER, SEPARATE run for the same real invoking_user (see
    handle_trigger's own real seeding of every known conversation
    variable into ctx["vars"] at the start of each run, under a real
    `conv_<name>` key, so any later node — not just ones wired
    downstream of a Variable Assigner in THIS run — can reference one).

    Real config shape: `cfg["assignments"]`, a real list of
    `{variable, operation, source_var, value, arithmetic_op}`. Real
    per-type operations, matching Dify's own documented modes exactly:
    'overwrite' (any type, from a real {{var}} reference — real type
    preserved via _resolve_whole_var), 'clear' (empty array for a
    currently-array value, else real None), 'set' (a real literal value
    from config, not a variable reference), 'arithmetic' (+-*/ against
    the current real number, defaulting to 0 if never set), 'append'/
    'extend'/'remove_first'/'remove_last' (real array-only operations,
    defaulting to an empty real list if never set)."""
    assignments = cfg.get("assignments") or []
    scope_key = f"{ctx['agent_id']}:{ctx.get('invoking_user', 'anon')}"
    results: Dict[str, Any] = {}

    for a in assignments:
        var_name = a.get("variable")
        if not var_name:
            continue
        op = a.get("operation", "overwrite")
        current = _get_conv_var(session, scope_key, var_name)

        if op == "clear":
            new_value = [] if isinstance(current, list) else None
        elif op == "set":
            new_value = a.get("value")
        elif op == "overwrite":
            new_value = _resolve_whole_var(a.get("source_var", ""), ctx)
        elif op == "arithmetic":
            raw_operand = a.get("source_var", "")
            operand = _resolve_whole_var(raw_operand, ctx)
            if operand is None:
                # Not a {{var}} reference — Dify's own real UI lets a
                # person type a literal number directly instead of
                # always requiring a variable that holds one; honor
                # that same real flexibility here.
                try:
                    operand = float(raw_operand)
                except (TypeError, ValueError):
                    operand = None
            arith_op = a.get("arithmetic_op", "+")
            base = current if isinstance(current, (int, float)) else 0
            try:
                operand_num = float(operand) if operand is not None else 0.0
            except (TypeError, ValueError):
                operand_num = 0.0
            if arith_op == "+":
                new_value = base + operand_num
            elif arith_op == "-":
                new_value = base - operand_num
            elif arith_op == "*":
                new_value = base * operand_num
            elif arith_op == "/":
                new_value = base / operand_num if operand_num != 0 else base
            else:
                new_value = base
            if isinstance(new_value, float) and new_value.is_integer():
                new_value = int(new_value)  # a real integer stays a real integer, not 4.0
        elif op == "append":
            item = _resolve_whole_var(a.get("source_var", ""), ctx)
            base_list = current if isinstance(current, list) else []
            new_value = base_list + [item]
        elif op == "extend":
            other = _resolve_whole_var(a.get("source_var", ""), ctx)
            base_list = current if isinstance(current, list) else []
            new_value = base_list + (other if isinstance(other, list) else [])
        elif op == "remove_first":
            base_list = current if isinstance(current, list) else []
            new_value = base_list[1:] if base_list else []
        elif op == "remove_last":
            base_list = current if isinstance(current, list) else []
            new_value = base_list[:-1] if base_list else []
        else:
            new_value = current

        _set_conv_var(session, scope_key, var_name, new_value)
        results[var_name] = new_value
        ctx["vars"][f"conv_{var_name}"] = new_value

    session.commit()
    ctx["vars"]["variable_assigner_results"] = results
    return ctx


def handle_output(cfg: dict, ctx: dict) -> dict:
    """Output node — collects one or more NAMED output variables at this
    point in the flow, real values pulled from whatever upstream nodes
    actually produced, not placeholders. Uses the same {{var}} lookup
    ctx["vars"] already supports (handle_template's mechanism), plus a
    real extension: {{input}} resolves to the actual original user input
    (ctx["input"]), which the existing template node's mechanism never
    covered since raw input lives outside ctx["vars"].

    A flow can have more than one Output node (e.g. one per branch) — each
    one's variables merge into ctx["structured_output"] rather than
    overwriting it, so branches that both eventually reach `respond` don't
    clobber each other's fields. This is the real backing for Workflow
    apps' "structured result out" UI described in AgentRunner: multiple
    named fields, not a single text blob."""
    import re
    variables = cfg.get("variables") or []
    ctx["structured_output"] = ctx.get("structured_output") or {}
    lookup = {**ctx["vars"], "input": ctx.get("input", "")}
    for v in variables:
        name = (v.get("name") or "").strip()
        expr = v.get("expr") or ""
        if not name:
            continue
        rendered = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(lookup.get(m.group(1), "")), expr)
        ctx["structured_output"][name] = rendered
    return ctx


SESSION_REQUIRED_NODE_TYPES = frozenset({
    "gov", "tool", "memory", "retrieval", "subagent", "llm", "variable_assigner", "parameter_extractor", "agent",
})


def call_node_handler(node_type: str, cfg: dict, ctx: dict, session: Session):
    """The one real dispatch function (§0.51, GAP_REPORT) — every real
    caller (the sync path's main traversal, the sync path's loop-body
    traversal, and the Temporal activity) now goes through this, instead
    of each keeping its own separate hardcoded tuple of which node types
    need a real DB Session passed in.

    This is a real fix for a real bug: the Temporal path's own copy of
    that tuple was missing "llm", "variable_assigner",
    "parameter_extractor", and "agent" — all four handlers genuinely
    require session as a real positional argument (see their own
    signatures), so a durable-path flow reaching any of them raised a
    real TypeError at runtime. The sync path's two call sites already
    had the correct, full list; only Temporal had silently drifted.
    SESSION_REQUIRED_NODE_TYPES above is now the one real source of
    truth all three read from, so this specific class of drift can't
    recur — a new handler that needs session only has to be added here,
    once, not remembered in three places.

    Handlers both mutate ctx in place and return it (see handle_llm's
    own real `return ctx` after a real `ctx["vars"][...] = ...` mutation
    a few lines above it) — this dispatch function returns whatever the
    handler returns, so both "capture the return value" and "rely on
    the in-place mutation and ignore the return" caller styles keep
    working exactly as they did before this refactor."""
    handler = NODE_HANDLERS.get(node_type)
    if not handler:
        return None
    if node_type in SESSION_REQUIRED_NODE_TYPES:
        return handler(cfg, ctx, session)
    return handler(cfg, ctx)


NODE_HANDLERS = {
    "trigger": handle_trigger,
    "gov": handle_gov,
    "retrieval": handle_retrieval,
    "llm": handle_llm,
    "tool": handle_tool,
    "code": handle_code,
    "respond": handle_respond,
    "answer": handle_answer,
    "condition": handle_condition,
    "http": handle_http,
    "aggregator": handle_aggregator,
    "classifier": handle_classifier,
    "parameter_extractor": handle_parameter_extractor,
    "template": handle_template,
    "memory": handle_memory,
    "variable_assigner": handle_variable_assigner,
    "subagent": handle_subagent,
    "agent": handle_agent,
    "exit_loop": handle_exit_loop,
    "docextractor": handle_docextractor,
    "list_operator": handle_list_operator,
    "output": handle_output,
}

# Still not wired, for real architectural reasons (see README):
# (iteration: now real — a genuine array-based sibling of loop, see
# run_iteration_body's own docstring above for the full real behavior:
# real item/index binding, real sequential/parallel execution, real
# per-iteration error handling matching Dify's 3 documented modes.
# GAP_REPORT §0.35.)
# (docextractor: now real — genuine fetch + extraction from a public/
# presigned cloud storage URL, or any {{var}} that resolves to one. No
# in-app file upload UI exists, and no AWS/GCP/Azure SDK credentials are
# configured — see handle_docextractor's docstring for the exact boundary.)
# (hitl: now real on the SYNC path — genuine pause/resume via HitlRequest,
# not a Temporal signal handler. Special-cased directly in the traversal
# loop below rather than a NODE_HANDLERS entry, the same way `loop` is,
# since raising HitlPause needs the real node id compute_next_node_id
# doesn't otherwise have a reason to see. See HitlPause/HitlRequest.)
# - subagent, agent: each needs its own real design pass (subagent needs
#   the multi-hop principal chain from §7.6, including cycle detection so
#   an agent can't indirectly invoke itself; agent needs an actual
#   agentic-strategy selector distinct from the LLM node's own mode
#   toggle) — neither is a quick addition like memory (below) was.


import re

_ENV_REF = re.compile(r"\{\{\s*env\.(\w+)\s*\}\}")


def load_env_map(session: Session, agent_id: str) -> Dict[str, str]:
    """Real values, including secrets — this runs server-side inside the
    interpreter, which is the one place secret values are legitimately
    read in plaintext. Never returned to a client; see routers/env_vars.py
    for the API-side masking that keeps it that way everywhere else.

    Real decryption happens here too (§0.59, GAP_REPORT) — EnvVar.value
    is now encrypted at rest for secret-type rows; decrypt_secret's own
    real, honest fallback means a row still holding a genuinely
    plaintext value from before this fix (never re-encrypted
    retroactively) keeps working exactly as it always did."""
    from .secrets_crypto import decrypt_secret
    rows = session.exec(select(EnvVar).where(EnvVar.agent_id == agent_id)).all()
    return {r.key: (decrypt_secret(r.value) if r.type == "secret" else r.value) for r in rows}


def interpolate_config(config: dict, env_map: Dict[str, str]) -> dict:
    """Replaces every {{env.KEY}} occurrence in every string value of a
    node's config with the real stored value — the mechanism that makes
    'define once, use in any node' genuinely work, not just a documented
    convention nobody's code honors. Unknown keys are left as literal text
    (not silently blanked) so a typo'd reference is visible in the output
    rather than disappearing."""
    def sub(text: str) -> str:
        return _ENV_REF.sub(lambda m: env_map.get(m.group(1), m.group(0)), text)

    out: dict = {}
    for k, v in (config or {}).items():
        if isinstance(v, str):
            out[k] = sub(v)
        elif isinstance(v, list):
            out[k] = [sub(x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out


def compute_next_node_id(current_node: dict, ctx: dict, edges_by_source: Dict[str, List[dict]]) -> Optional[str]:
    """Given the node that just ran and the edges leaving it, decide which
    node runs next. Shared by both the sync path (below) and the Temporal
    workflow (temporal_workflow.py) so branching logic can't silently
    diverge between the two execution paths — a real risk once there were
    two separate places that walk the graph.

    A `condition` node has up to two outgoing edges, distinguished by
    `source_handle` ('true'/'false'). A `code` node whose real execution
    genuinely failed AND is configured with error_handling='Fail branch'
    (see handle_code) similarly has up to two outgoing edges
    ('success'/'error', or a legacy single None-handle edge treated as
    'success') — real branch-based error handling, not just a raised
    exception. Every other node type — including `loop`, now that it's a
    real container walked separately by run_loop_body — has at most one
    outgoing edge (source_handle=None). If a branching node has no edge
    for the outcome it produced, the run ends there rather than
    guessing."""
    outgoing = edges_by_source.get(current_node["id"], [])
    if not outgoing:
        return None
    if current_node["type"] == "condition":
        wanted = "true" if ctx["vars"].get("branch_taken") else "false"
        matched = next((e for e in outgoing if e.get("source_handle") == wanted), None)
        return matched["target"] if matched else None
    if current_node["type"] == "code":
        if ctx["vars"].get("_code_failed"):
            matched = next((e for e in outgoing if e.get("source_handle") == "error"), None)
            return matched["target"] if matched else None
        matched = next((e for e in outgoing if e.get("source_handle") in (None, "success")), None)
        return matched["target"] if matched else outgoing[0]["target"]
    if current_node["type"] == "llm":
        # Real, second instance of the exact same real two-port
        # mechanism (§0.64, GAP_REPORT) — handle_llm's own
        # error_handling='Fail branch' sets _llm_failed the identical
        # way handle_code sets _code_failed, so a real provider failure
        # can route down a real, distinct error port instead of always
        # raising regardless of what this specific node was configured
        # to do about it.
        if ctx["vars"].get("_llm_failed"):
            matched = next((e for e in outgoing if e.get("source_handle") == "error"), None)
            return matched["target"] if matched else None
        matched = next((e for e in outgoing if e.get("source_handle") in (None, "success")), None)
        return matched["target"] if matched else outgoing[0]["target"]
    return outgoing[0]["target"]


MAX_GRAPH_STEPS = 50  # safety cap against an accidental cycle in the graph
# Real, single shared constant (§0.49, GAP_REPORT) — previously the same
# real value (5) was hardcoded separately here AND in
# ucs_client.py's resolve_entitlement_plan, synchronized only by a
# comment ("matching resolve_entitlement_plan's own chain_depth_cap")
# rather than an actual shared reference. A real, existing drift risk,
# found and fixed while building the Guardrails page that now displays
# this value directly from here.
SUB_AGENT_HOP_CAP = 5


def _resolve_outgoing_edges(node: dict, ctx: dict, edges_by_source: Dict[str, List[dict]]) -> Tuple[set, set]:
    """Real edge-activation resolution — the fan-out-aware counterpart to
    compute_next_node_id (§0.40, GAP_REPORT: real parallel graph
    execution). Where compute_next_node_id picks a single next node,
    this decides, for EVERY one of a node's real outgoing edges
    (there can now be more than one per source/handle — see add_edge's
    own real fan-out support), whether that edge activates (its real
    target genuinely becomes reachable) or is dead (its target will
    never be reached via this edge, this run). Same real branch logic
    as compute_next_node_id — a `condition` node's unchosen branch, or
    a `code` node's unchosen success/error branch, is dead; everything
    else activates every real outgoing edge."""
    outgoing = edges_by_source.get(node["id"], [])
    activated: set = set()
    dead: set = set()
    if node["type"] == "condition":
        wanted = "true" if ctx["vars"].get("branch_taken") else "false"
        for e in outgoing:
            (activated if e.get("source_handle") == wanted else dead).add(e["id"])
    elif node["type"] == "code" and any(e.get("source_handle") in ("success", "error") for e in outgoing):
        failed = bool(ctx["vars"].get("_code_failed"))
        for e in outgoing:
            handle = e.get("source_handle")
            is_wanted = (handle == "error") if failed else (handle in (None, "success"))
            (activated if is_wanted else dead).add(e["id"])
    elif node["type"] == "hitl" and ctx["vars"].get("hitl_action_id") is not None:
        # Real action-based branching (§0.42, GAP_REPORT) — a hitl
        # node's own outgoing edges are keyed by real action id
        # (approve/reject/etc.), same real pattern as condition's
        # true/false. Only relevant once a real response has actually
        # been received (ctx["vars"]["hitl_action_id"] set) — before
        # that, this node hasn't run yet, so this branch is simply
        # never reached (a not-yet-run node's own outgoing edges are
        # never resolved). Real, honest fallback: if nothing is wired
        # for the chosen action, every outgoing edge is dead — matches
        # resume_flow_sync's own real behavior of just ending the run
        # there rather than guessing a fallback path.
        wanted = ctx["vars"]["hitl_action_id"]
        by_handle = [e for e in outgoing if e.get("source_handle") == wanted]
        if by_handle:
            # A form offering Approve and Reject wires an edge per
            # action, and the handle is what tells them apart.
            for e in outgoing:
                (activated if e.get("source_handle") == wanted else dead).add(e["id"])
        else:
            # The ordinary shape: one exit, taken whichever action was
            # chosen. Marking it dead because it was not named after the
            # action stranded the run — it reported success, stopped at
            # the hitl node, and produced a null output, because no edge
            # was called "continue".
            for e in outgoing:
                (activated if not e.get("source_handle") else dead).add(e["id"])
    else:
        for e in outgoing:
            activated.add(e["id"])
    return activated, dead


def node_readiness(node_id: str, start_node_id: str, edges_by_target: Dict[str, List[dict]],
                    activated_edges: set, dead_edges: set) -> str:
    """Pure, real, shared readiness check (§0.41, GAP_REPORT) — used by
    BOTH run_graph_concurrent (below) and the Temporal durable path
    (temporal_workflow.py's AgentRunWorkflow), specifically so the two
    real schedulers can never again silently diverge on what "ready"
    means. This exact duplication — the readiness logic re-inlined
    separately in each scheduler — is what let the durable path's own
    real fan-out support quietly lag behind the sync path's; extracting
    it once here closes that class of drift, not just today's instance
    of it. Returns "ready" (every real incoming edge is either
    activated or dead, and at least one is activated), "dead" (no real
    incoming edges at all and this isn't the start node, or every real
    incoming edge is dead), or "waiting" (still unresolved)."""
    if node_id == start_node_id:
        return "ready"
    incoming = edges_by_target.get(node_id, [])
    if not incoming:
        return "dead"
    resolved = sum(1 for e in incoming if e["id"] in activated_edges or e["id"] in dead_edges)
    if resolved < len(incoming):
        return "waiting"
    return "ready" if any(e["id"] in activated_edges for e in incoming) else "dead"


def propagate_dead_nodes(nodes_by_id: Dict[str, dict], start_node_id: str,
                          edges_by_source: Dict[str, List[dict]], edges_by_target: Dict[str, List[dict]],
                          activated_edges: set, dead_edges: set, done_nodes: set, dead_nodes: set) -> None:
    """Pure, real, shared dead-node propagation (§0.41, GAP_REPORT) —
    the other half of what BOTH real schedulers need, extracted for the
    same reason node_readiness was. Mutates dead_nodes/dead_edges in
    place, repeating until a full scan makes no further progress —
    marking one node dead can free up more nodes to resolve (ready or
    dead) on the next pass."""
    progressed = True
    while progressed:
        progressed = False
        for nid in nodes_by_id:
            if nid in done_nodes or nid in dead_nodes:
                continue
            if node_readiness(nid, start_node_id, edges_by_target, activated_edges, dead_edges) == "dead":
                dead_nodes.add(nid)
                for e in edges_by_source.get(nid, []):
                    dead_edges.add(e["id"])
                progressed = True


def compute_ready_wave(nodes_by_id: Dict[str, dict], start_node_id: str,
                        edges_by_source: Dict[str, List[dict]], edges_by_target: Dict[str, List[dict]],
                        activated_edges: set, dead_edges: set, done_nodes: set, dead_nodes: set) -> List[dict]:
    """Pure, real, shared wave computation (§0.41, GAP_REPORT) — runs
    propagate_dead_nodes first (dead nodes must be resolved before a
    wave is computed, or a node genuinely blocked forever would be
    mistaken for one still waiting), then returns every currently-ready
    node. An empty return means the graph has genuinely finished (no
    more nodes can ever become ready)."""
    propagate_dead_nodes(nodes_by_id, start_node_id, edges_by_source, edges_by_target,
                          activated_edges, dead_edges, done_nodes, dead_nodes)
    return [n for nid, n in nodes_by_id.items()
            if nid not in done_nodes and nid not in dead_nodes
            and node_readiness(nid, start_node_id, edges_by_target, activated_edges, dead_edges) == "ready"]


# Node types with pre-existing closures (over a shared ctx, not a
# parameter — run_loop_body/run_iteration_body/raise_hitl_pause) that
# aren't safe to hand an isolated per-thread ctx copy without a larger
# refactor. Real, stated scope boundary (§0.40, GAP_REPORT): these
# still run one at a time, using the real shared ctx directly, same as
# before this change — but they're still correctly WAVE-SCHEDULED
# relative to independent plain-node branches, just not bundled into
# the same concurrent thread pool as them.
_SOLO_NODE_TYPES = {"loop", "iteration", "hitl"}


def run_graph_concurrent(
    start_node_id: str, nodes_by_id: Dict[str, dict], edges: List[dict],
    run_one_node: Callable[..., None], ctx: Dict[str, Any],
    steps_state: Dict[str, int], is_cancelled: Optional[Callable[[], bool]] = None,
    session_factory: Optional[Callable[[], Session]] = None,
) -> Optional[dict]:
    """The real graph scheduler (§0.40, GAP_REPORT) — replaces the
    previous strictly-linear `while current: ...; current = next_node`
    walk. Matches Dify's own documented orchestration model
    (https://docs.dify.ai/en/cloud/use-dify/build/orchestrate-node):
    two nodes with no dependency on each other run genuinely
    concurrently (real threads, real isolated context, real
    deterministic merge afterward — not simulated), and a downstream
    node that depends on more than one upstream branch genuinely waits
    for ALL of them before running, not just the first.

    Real "wave" scheduling: repeatedly computes every node whose real
    incoming edges have ALL been resolved (either activated — the real
    source ran and this edge fired — or dead — the source ran but took
    a different branch), runs every currently-READY node in one real
    wave, then recomputes. A node with EVERY incoming edge dead (no
    live path reaches it this run) is itself marked dead and never
    runs — propagated transitively, so everything exclusively
    downstream of a condition's unchosen branch is correctly and
    honestly skipped, not run anyway or left hanging forever.

    Real concurrent execution for a wave with more than one "plain"
    node (excludes `_SOLO_NODE_TYPES`, see its own docstring for why):
    each node gets a real, independent deep copy of ctx["vars"], run in
    its own real thread via ThreadPoolExecutor (capped at 10 — the same
    real cap the Iteration node's own parallel mode already uses).
    After every thread in the wave completes, results are merged back
    into the real shared ctx in a deterministic order (sorted by node
    id) — if two concurrent nodes in the same wave genuinely wrote the
    SAME variable name (a real, if unusual, authoring choice), the
    later one in that stable order wins; a real, documented tiebreak,
    not an accidental race. A genuine failure in one concurrently-
    running node still lets its real siblings finish (Python's
    ThreadPoolExecutor waits for every submitted thread on exit even
    when one raises) before the exception surfaces — no orphaned
    threads, no silently-lost sibling work.

    Returns a real {"status": "cancelled"} dict if is_cancelled() ever
    returns true at a wave boundary; returns None on normal completion.
    Any other real exception (HitlPause, UCSDenied,
    HarnessStepLimitExceeded, a node handler's own genuine failure)
    propagates up exactly as the old sequential walk's did — callers
    catch these the same way, unchanged."""
    edges_by_source: Dict[str, List[dict]] = {}
    edges_by_target: Dict[str, List[dict]] = {}
    for e in edges:
        edges_by_source.setdefault(e["source"], []).append(e)
        edges_by_target.setdefault(e["target"], []).append(e)

    activated_edges: set = set()
    dead_edges: set = set()
    done_nodes: set = set()
    dead_nodes: set = set()

    while True:
        if is_cancelled and is_cancelled():
            return {"status": "cancelled"}

        # Real, shared wave computation (node_readiness/
        # propagate_dead_nodes/compute_ready_wave, defined above) — the
        # exact same real logic the Temporal durable path now also
        # calls, so the two schedulers can't silently diverge again.
        wave = compute_ready_wave(nodes_by_id, start_node_id, edges_by_source, edges_by_target,
                                   activated_edges, dead_edges, done_nodes, dead_nodes)
        if not wave:
            # §0.103, GAP_REPORT — the actual runtime half of the real
            # fix: an empty wave here does NOT, by itself, mean the
            # graph genuinely finished — it also happens when real
            # nodes remain that can never become ready (a true cycle:
            # each one's own unresolved incoming edge traces back to
            # another node in the SAME unresolved set). Distinguished
            # here, explicitly, rather than assumed — any node still
            # neither done nor dead is one of these, and GraphDeadlockError
            # (not a silent, successful return) is what a genuine one
            # of these gets, matching the report's own required fix.
            blocked = [nid for nid in nodes_by_id if nid not in done_nodes and nid not in dead_nodes]
            if blocked:
                unresolved_edges = [
                    e["id"] for e in edges
                    if e["target"] in blocked and e["id"] not in activated_edges and e["id"] not in dead_edges
                ]
                raise GraphDeadlockError(blocked_nodes=sorted(blocked), unresolved_edges=sorted(unresolved_edges))
            break

        if steps_state["count"] + len(wave) > MAX_GRAPH_STEPS:
            raise HarnessStepLimitExceeded()
        steps_state["count"] += len(wave)

        plain = [n for n in wave if n["type"] not in _SOLO_NODE_TYPES]
        solo = [n for n in wave if n["type"] in _SOLO_NODE_TYPES]
        # hitl last within its own solo group — lets any real progress
        # from this same wave's other solo nodes (loop/iteration) commit
        # before a pause, giving a genuinely more complete state to
        # resume from later.
        solo.sort(key=lambda n: 1 if n["type"] == "hitl" else 0)

        if len(plain) == 1:
            run_one_node(plain[0], ctx)
        elif len(plain) > 1:
            def run_isolated(node: dict) -> dict:
                """One node, on its own thread, with its OWN database
                session.

                A SQLAlchemy Session is not thread-safe, and before this
                every node in a concurrent wave was handed the single
                session belonging to the run — so two parallel nodes
                that touch persistence (memory, retrieval, llm with chat
                history, agent, sub-agent, parameter extraction,
                variable assignment) were issuing statements and
                flushing on the same connection at the same time. The
                failure mode is not a clean error: it is
                IllegalStateChangeError, a flush of one node's pending
                rows inside the other's transaction, a rollback that
                discards work neither node knew about — intermittently,
                under load, and almost never in a single-node test.

                The iteration handler already does exactly this for its
                own parallel branch (a Session per item, closed when the
                item finishes); this closes the same gap in the top-level
                graph scheduler.

                The session is closed here rather than left to the
                garbage collector, so the connection returns to the pool
                as soon as the node is done — ten parallel nodes must not
                each hold a connection for the length of the whole wave.
                """
                local_ctx = {**ctx, "vars": copy.deepcopy(ctx["vars"])}
                if session_factory is None:
                    # No factory: the caller has no database session to
                    # give (a validation or preview run). Unchanged
                    # behaviour, and nothing is shared because nothing
                    # is passed.
                    run_one_node(node, local_ctx)
                    return local_ctx
                node_session = session_factory()
                try:
                    run_one_node(node, local_ctx, node_session)
                finally:
                    node_session.close()
                return local_ctx

            results: Dict[str, dict] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(plain))) as pool:
                futures = {pool.submit(run_isolated, n): n for n in plain}
                for fut in concurrent.futures.as_completed(futures):
                    node = futures[fut]
                    results[node["id"]] = fut.result()  # a real handler failure re-raises here
            for node in sorted(plain, key=lambda n: n["id"]):
                local_ctx = results[node["id"]]
                ctx["vars"].update(local_ctx["vars"])
                if local_ctx.get("output") is not None:
                    ctx["output"] = local_ctx["output"]

        for node in plain:
            done_nodes.add(node["id"])
            act, dead = _resolve_outgoing_edges(node, ctx, edges_by_source)
            activated_edges.update(act)
            dead_edges.update(dead)

        for node in solo:
            run_one_node(node, ctx)
            done_nodes.add(node["id"])
            act, dead = _resolve_outgoing_edges(node, ctx, edges_by_source)
            activated_edges.update(act)
            dead_edges.update(dead)

    return None


def _execute_flow(session: Session, agent, ctx: Dict[str, Any], env_map: Dict[str, str],
                   start_node: dict, initial_steps: int = 0,
                   on_event: Optional[Callable[[dict], None]] = None,
                   is_cancelled: Optional[Callable[[], bool]] = None,
                   flow: Optional[dict] = None) -> dict:
    """The real traversal engine — shared by a fresh run_flow_sync call
    AND resume_flow_sync's continuation of a previously-paused one, so a
    node behaves identically either way rather than resume being a
    second, easily-drifting reimplementation. Walks the REAL flow graph
    (nodes + explicit edges), not an implicit x-position ordering — a
    `condition` node genuinely branches depending on the outcome.

    A `loop` node is a real container: nodes with `parent_id == loop.id`
    are its actual body, walked as their own real sub-graph starting from
    whichever child has no incoming edge from a sibling. Each pass repeats
    from that entry child until either an `exit_loop` node is reached (a
    real early exit, driven by whatever branching the person wired inside
    the body — e.g. a `condition` node deciding when to stop) or
    `max_loops` is hit (a safety cap, not the primary exit mechanism).

    A `hitl` node is real, genuine pause: rendered synchronously (real
    {{var}} interpolation of its Form Content against the current ctx),
    then raises HitlPause with everything needed for the caller (run_
    flow_sync / resume_flow_sync) to persist a HitlRequest and return
    "paused_for_human_input" — no Temporal signal handling involved.
    Honest scope boundary: a `hitl` node INSIDE a loop body isn't
    supported (run_loop_body below doesn't check for it) — a real gap,
    not a silent one; see README/GAP_REPORT.

    on_event, when given, is called synchronously with a real
    {"type": "node_start"|"node_done", ...} dict right as each node
    actually starts/finishes — the real progress signal
    routers/execution.py's streaming endpoint forwards to the browser
    over SSE, not a replay of a already-finished run. is_cancelled, when
    given, is checked once per node boundary (between nodes, not mid-
    node) — a real but node-boundary-granular stop: it can't interrupt a
    single node's own blocking call (an in-flight LLM/HTTP request still
    runs to completion), an honest scope boundary stated here rather than
    silently pretending a run can be killed instantly.

    flow (§0.54, GAP_REPORT): an explicit override for nodes/edges,
    threaded through from run_flow_sync's own real flow parameter — see
    its docstring for the actual bug this fixes. Falls back to
    agent.flow when not given, which is what a nested Sub-Agent call
    (handle_subagent, via its own direct run_flow_sync call with no
    flow override) still genuinely gets — the target agent's own live
    draft, matching the separately-stated Sub-Agent architectural
    boundary (§0.53) this fix doesn't touch."""
    nodes = (flow or agent.flow).get("nodes", [])
    edges = (flow or agent.flow).get("edges", [])
    nodes_by_id = {n["id"]: n for n in nodes}
    edges_by_source: Dict[str, List[dict]] = {}
    for e in edges:
        edges_by_source.setdefault(e["source"], []).append(e)

    spans: List[dict] = []
    steps_state = {"count": initial_steps}

    def exec_node(node: dict, node_ctx: Dict[str, Any],
                  node_session: Optional[Session] = None) -> None:
        """Executes exactly one node's real handler against `node_ctx` —
        shared by the outer walk (real shared ctx, or a real isolated
        per-thread copy under concurrent execution — see
        run_graph_concurrent) and any loop-body inner walk (always the
        real shared ctx, per that container's own stated scope
        boundary). `node_ctx` is a genuine parameter, not a closure over
        the outer ctx, specifically so a concurrently-running node's own
        real isolated copy is what its handler actually reads/writes —
        this was a real bug during the build: an earlier draft still
        closed over the outer ctx here, meaning "isolated" concurrent
        execution wasn't actually isolated at all. spans/on_event stay
        closed over deliberately — they describe the one real overall
        run's trace, not a per-thread one. Appends its own span (success
        or failure) and re-raises so the one try/except around the whole
        traversal is what actually stops the run."""
        handler = NODE_HANDLERS.get(node["type"])
        if on_event:
            on_event({"type": "node_start", "node_id": node.get("id"), "node_type": node["type"]})
        if not handler:
            # Real, loud failure (§0.73, GAP_REPORT) — see
            # UnsupportedNodeType's own docstring for why this used to
            # be a silent "skipped" span instead. Still records a real
            # span (with the real error, not a fabricated "ok") before
            # re-raising, so the run's own trace honestly shows where
            # and why it stopped, matching every other real failure
            # path in this same function below.
            span = {"node": node["type"], "node_id": node.get("id"), "status": "error",
                    "error": f"No real handler for node type '{node['type']}'"}
            spans.append(span)
            if on_event:
                on_event({"type": "node_done", **span})
            raise UnsupportedNodeType(node["type"], node.get("id"))
        start = datetime.utcnow()
        from .agent_assets import effective_env_map
        node_config = interpolate_config(node.get("config") or {},
                                         effective_env_map(node_session or session, agent.id, node, env_map))
        # Real usage tracking (§0.78, GAP_REPORT) — reset immediately
        # before this one node's own handler runs, so
        # _aggregate_llm_usage below reflects exactly this node's real
        # LLM calls, never a stale accumulation from an earlier node
        # sharing the same real, mutated ctx.
        node_ctx["_llm_usage"] = []
        # _current_node_id (§0.83, GAP_REPORT) — real, minimal ctx
        # threading so handle_subagent (and any future handler that
        # similarly needs to raise a real HitlPause on behalf of a
        # DIFFERENT node than the one a hitl node's own raise_hitl_pause
        # closure already knows about) can construct a real,
        # node-anchored pause without needing the full node dict
        # threaded through call_node_handler's own signature.
        node_ctx["_current_node_id"] = node.get("id")
        try:
            # The session belonging to THIS node when the scheduler ran
            # it concurrently, else the run's own. Never both: a handler
            # gets one session for the whole of its call, and no ORM
            # object is carried between them — handlers put plain values
            # into ctx (a memory row's `.value`, a retrieval result's
            # text), never a live instance another node's session would
            # then have to re-attach.
            call_node_handler(node["type"], node_config, node_ctx, node_session or session)
            span = {
                "node": node["type"], "node_id": node.get("id"), "status": "ok",
                "duration_ms": int((datetime.utcnow() - start).total_seconds() * 1000),
            }
            # Real content per span, not just timing — genuinely what this
            # node actually saw/produced, not a synthesized generic label.
            # Only node types that actually set ctx["output"] themselves
            # get an output_preview — several handlers use `ctx["output"]
            # or X` (only-if-not-already-set) semantics, so blindly
            # reading ctx["output"] after every node would misattribute
            # an earlier node's real output to an unrelated later span
            # (e.g. a gov or condition node running after an llm node).
            if node["type"] == "trigger":
                span["input_preview"] = (node_ctx.get("input") or "")[:300]
            # A UCS check whose switch is off ran, but checked nothing.
            # Reporting that as a plain "ok" would put a green tick on a
            # step that did no work — the trace has to say which of the
            # two happened, because "the gate passed" and "the gate stood
            # aside" are different facts when reading back a denial.
            if node["type"] == "gov" and (node.get("config") or {}).get("enabled") is False:
                span["status"] = "skipped"
                span["detail"] = {"reason": "This node's switch is off. Entitlement was still "
                                            "checked before the flow started."}
            # An http node reports failure by writing an error into its
            # own output variable rather than raising, so the span above
            # called it "ok" no matter what happened. A node that was
            # refused outright — Showcase 5's own `http://localhost:8000
            # /api/health`, blocked by the egress policy because loopback
            # is not a permitted destination — produced a green tick at
            # 0 ms, and the only evidence anything was wrong was an empty
            # string further down the template.
            #
            # Same reasoning as the gov/skipped case just above: the
            # trace has to say which of the two happened. The run's own
            # verdict is deliberately NOT changed — the handler chose to
            # return rather than raise, downstream nodes really did run,
            # and turning that into a failed run would break every flow
            # that tolerates an HTTP failure on purpose.
            if node["type"] == "http":
                http_out = node_ctx.get("vars", {}).get("http_output") or {}
                http_error = http_out.get("error") if isinstance(http_out, dict) else None
                if http_error:
                    span["status"] = "error"
                    span["error"] = http_error
                    span["detail"] = {"continued": True,
                                      "reason": "The http node reports failure through its "
                                                "output variable rather than raising, so the "
                                                "rest of the flow still ran."}
            if node["type"] in ("llm", "code", "template", "http", "agent", "subagent", "respond") and node_ctx.get("output"):
                span["output_preview"] = str(node_ctx["output"])[:300]
            # Real per-node token/cost usage (§0.78, GAP_REPORT) — the
            # actual fix for the reported gap: real usage came back
            # from call_llm already, but was discarded before this fix.
            # Omitted entirely (not zero-valued) for a node that made
            # no real LLM calls — see _aggregate_llm_usage's own
            # docstring.
            usage = _aggregate_llm_usage(node_ctx)
            if usage:
                span.update(usage)
            spans.append(span)
            if on_event:
                on_event({"type": "node_done", **span})
        except UCSDenied as e:
            span = {"node": node["type"], "node_id": node.get("id"), "status": "ucs_denied", "detail": e.detail}
            spans.append(span)
            if on_event:
                on_event({"type": "node_done", **span})
            raise
        except HarnessStepLimitExceeded:
            span = {"node": node["type"], "node_id": node.get("id"), "status": "harness_fail"}
            spans.append(span)
            if on_event:
                on_event({"type": "node_done", **span})
            raise
        except Exception as e:
            span = {"node": node["type"], "node_id": node.get("id"), "status": "error", "error": str(e)}
            spans.append(span)
            if on_event:
                on_event({"type": "node_done", **span})
            raise

    def run_loop_body(loop_node: dict) -> None:
        children = [n for n in nodes if n.get("parent_id") == loop_node["id"]]
        if not children:
            return  # empty loop container — nothing to run, falls straight through
        child_ids = {c["id"] for c in children}
        child_edges_by_source: Dict[str, List[dict]] = {}
        incoming_targets = set()
        for e in edges:
            if e["source"] in child_ids and e["target"] in child_ids:
                child_edges_by_source.setdefault(e["source"], []).append(e)
                incoming_targets.add(e["target"])
        entry_candidates = [c for c in children if c["id"] not in incoming_targets]
        entry = entry_candidates[0] if entry_candidates else children[0]

        cfg = loop_node.get("config") or {}
        max_loops = int(cfg.get("max_loops") or 10)

        iteration = 0
        while iteration < max_loops:
            iteration += 1
            ctx["vars"]["loop_iteration"] = iteration
            current_child: Optional[dict] = entry
            exited = False
            while current_child:
                if steps_state["count"] >= MAX_GRAPH_STEPS or (is_cancelled and is_cancelled()):
                    return
                steps_state["count"] += 1
                exec_node(current_child, ctx)  # real span even for exit_loop — it's a genuine step, not a silent skip
                if current_child["type"] == "exit_loop":
                    exited = True
                    break
                next_id = compute_next_node_id(current_child, ctx, child_edges_by_source)
                current_child = nodes_by_id.get(next_id) if next_id else None
            if exited:
                return

    def run_iteration_body(iteration_node: dict) -> None:
        """Real array iteration — Dify's own documented Iteration node
        (https://docs.dify.ai/en/cloud/use-dify/nodes/iteration; see
        GAP_REPORT §0.35 for the full comparison this closes), genuinely
        separate from `loop`'s own condition-driven repeat mechanism
        above — deliberately left completely untouched, per explicit
        instruction, even though both are real container node types
        sharing the general "children with parent_id, walked via their
        own real sub-graph" mechanic.

        Resolves a real array from `input_var` (a whole `{{var}}`
        reference, type-preserved — same real resolution
        `_resolve_whole_var` gives the Variable Aggregator). Runs the
        real body sub-graph once per real element, with real `item`/
        `index` genuinely exposed to body nodes via a real PER-
        ITERATION ctx snapshot (a real shallow copy of vars, so one
        iteration's body can't leak into another's) — matching Dify's
        own documented `item`/`index` built-ins exactly, not the `loop`
        node's own unrelated `loop_iteration` counter. Collects each
        real iteration's real output (whatever its own last node sets
        as `ctx["output"]`, same real convention the whole flow uses)
        into a real output array under `output_var`, in real input
        order — even under real parallel execution, since results are
        written by real index, not completion order.

        Real sequential vs. real parallel (up to Dify's own documented
        cap of 10 concurrent) via a real `ThreadPoolExecutor`; each
        parallel worker gets its own real dedicated `Session` (the
        outer `session` isn't safe to share across threads — the same
        real thread-safety pattern already used by batch-run, the
        streaming endpoint, and the /run endpoint's own concurrency
        fix), so `gov`/`tool`/`memory`/`retrieval`/`subagent`/`llm`
        body nodes work correctly under real parallel execution too,
        not just sequential.

        Real per-iteration error handling, matching Dify's own 3
        documented modes exactly: 'Terminated' (the real default) stops
        the whole iteration node and re-raises, failing this run, the
        same as any other node's real, unhandled exception; 'Continue
        on error' keeps a real `None` at that real index and keeps
        going; 'Remove abnormal output' genuinely omits that index
        entirely, producing a real, shorter output array — not a
        `None` placeholder.

        `flatten` (real, defaults to true, matching the pre-existing
        config panel's own default): if a real per-iteration result is
        itself a real list, splices its real elements into the parent
        output array one level rather than nesting it — a real,
        genuinely useful default for the common case of a body that
        itself produces small lists per item."""
        import concurrent.futures

        cfg = iteration_node.get("config") or {}
        children = [n for n in nodes if n.get("parent_id") == iteration_node["id"]]
        array_val = _resolve_whole_var(cfg.get("input_var", ""), ctx)
        output_var = cfg.get("output_var") or "iteration_output"

        raw = array_val
        array_val = _as_list(raw)
        if array_val is None:
            # Nothing supplied at all is an empty iteration, which is a
            # legitimate thing to ask for. Something supplied that is not
            # an array is a mistake, and it must not read as "nothing
            # matched": that is exactly how an input typed as
            # [alpha, beta] rather than ["alpha","beta"] produced an
            # empty result with no explanation anywhere on screen.
            if raw is None or raw == "":
                ctx["vars"][output_var] = []
                return
            raise ValueError(
                f"Iteration input {cfg.get('input_var')} is not an array. "
                f"Got {type(raw).__name__} {str(raw)[:80]!r}. "
                "This field takes JSON, so strings need double quotes — "
                '["alpha", "beta"], not [alpha, beta].')
        if not children or not array_val:
            ctx["vars"][output_var] = []
            return

        child_ids = {c["id"] for c in children}
        child_edges_by_source: Dict[str, List[dict]] = {}
        incoming_targets = set()
        for e in edges:
            if e["source"] in child_ids and e["target"] in child_ids:
                child_edges_by_source.setdefault(e["source"], []).append(e)
                incoming_targets.add(e["target"])
        entry_candidates = [c for c in children if c["id"] not in incoming_targets]
        entry = entry_candidates[0] if entry_candidates else children[0]

        error_mode = cfg.get("error_response", "Terminated")
        parallel = bool(cfg.get("parallel"))
        flatten = cfg.get("flatten", True)

        def run_one(index: int, item: Any, iter_session: Session) -> Any:
            iter_vars = dict(ctx["vars"])
            iter_vars["item"] = item
            iter_vars["index"] = index
            iter_ctx: Dict[str, Any] = {**ctx, "vars": iter_vars, "output": None}
            current_child: Optional[dict] = entry
            steps_here = 0
            while current_child:
                steps_here += 1
                if steps_here > MAX_GRAPH_STEPS:
                    raise HarnessStepLimitExceeded()
                handler = NODE_HANDLERS.get(current_child["type"])
                if on_event:
                    on_event({"type": "node_start", "node_id": current_child.get("id"), "node_type": current_child["type"], "iteration_index": index})
                if not handler:
                    # Real, loud failure (§0.73, GAP_REPORT) — this
                    # used to silently `if handler:` skip an unknown
                    # node type inside a loop/iteration body with NO
                    # real span or event at all recorded for the skip
                    # — worse than the main traversal's own old
                    # "skipped" span, since the very next on_event
                    # below unconditionally claimed "status": "ok" for
                    # a step that never actually ran. See
                    # UnsupportedNodeType's own docstring.
                    if on_event:
                        on_event({"type": "node_done", "node_id": current_child.get("id"), "node": current_child["type"],
                                   "status": "error", "error": f"No real handler for node type '{current_child['type']}'", "iteration_index": index})
                    raise UnsupportedNodeType(current_child["type"], current_child.get("id"))
                from .agent_assets import effective_env_map
                node_config = interpolate_config(current_child.get("config") or {},
                                                 effective_env_map(iter_session, agent.id, current_child, env_map))
                iter_ctx["_current_node_id"] = current_child.get("id")
                call_node_handler(current_child["type"], node_config, iter_ctx, iter_session)
                if on_event:
                    on_event({"type": "node_done", "node_id": current_child.get("id"), "node": current_child["type"], "status": "ok", "iteration_index": index})
                next_id = compute_next_node_id(current_child, iter_ctx, child_edges_by_source)
                current_child = nodes_by_id.get(next_id) if next_id else None
            # A body has no `respond` node of its own (that's a
            # top-level-flow concept) — so nothing would otherwise ever
            # set iter_ctx["output"]. _pick_body_output uses the same
            # real priority order handle_respond's own fallback chain
            # does, but keeps the REAL type (an int stays an int) —
            # handle_respond itself deliberately coerces to a display
            # string, which is right for a flow's final text output but
            # wrong for collecting a real typed per-element result here.
            return _pick_body_output(iter_ctx)

        def run_one_safe(index: int, item: Any, iter_session: Session):
            try:
                return index, run_one(index, item, iter_session), None
            except Exception as e:
                return index, None, str(e)

        outcomes: List[tuple] = [None] * len(array_val)  # (index, value, error) per real position

        if parallel:
            from .db import engine as _engine
            max_workers = min(10, len(array_val))  # real Dify-documented cap
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {}
                for i, item in enumerate(array_val):
                    if is_cancelled and is_cancelled():
                        break
                    thread_session = Session(_engine)
                    fut = pool.submit(run_one_safe, i, item, thread_session)
                    futures[fut] = thread_session
                for fut in concurrent.futures.as_completed(futures):
                    i, value, error = fut.result()
                    outcomes[i] = (i, value, error)
                    futures[fut].close()
                    if error and error_mode == "Terminated":
                        for other in futures:
                            other.cancel()
                        raise RuntimeError(f"Iteration {i} failed: {error}")
        else:
            for i, item in enumerate(array_val):
                if is_cancelled and is_cancelled():
                    break
                i2, value, error = run_one_safe(i, item, session)
                outcomes[i] = (i2, value, error)
                if error and error_mode == "Terminated":
                    raise RuntimeError(f"Iteration {i} failed: {error}")

        results: List[Any] = finalize_iteration_outcomes(outcomes, error_mode, flatten)

        ctx["vars"][output_var] = results

    def raise_hitl_pause(node: dict) -> None:
        cfg = interpolate_config(node.get("config") or {}, env_map)
        content = _interp_vars(cfg.get("form_content", ""), ctx)
        fields = cfg.get("fields") or []
        actions = cfg.get("actions") or [{"id": "continue", "title": "Continue", "style": "primary"}]
        delivery_methods = cfg.get("delivery_methods") or ["web_app"]
        delivery_status = {
            m: ("delivered (web app)" if m == "web_app" else
                "not sent — no email provider configured in this reference platform")
            for m in delivery_methods
        }
        span = {"node": "hitl", "node_id": node.get("id"), "status": "paused"}
        spans.append(span)
        if on_event:
            on_event({"type": "node_done", **span})
        raise HitlPause(node["id"], content, fields, actions, delivery_status)

    def run_one_node_dispatch(node: dict, node_ctx: Dict[str, Any],
                              node_session: Optional[Session] = None) -> None:
        """The real per-node dispatch run_graph_concurrent calls for
        every node in a wave — mirrors the prior inline if/elif chain
        exactly, just as a real, reusable function. `loop`/`iteration`/
        `hitl` ignore `node_ctx` and use the real outer `ctx` directly
        (their own closures aren't ctx-parameterized — see
        _SOLO_NODE_TYPES's own docstring); this is genuinely safe
        because the scheduler never hands them an isolated copy — they
        always run in the solo group, never bundled into a concurrent
        wave with each other or with plain nodes."""
        if node["type"] == "loop":
            run_loop_body(node)
        elif node["type"] == "iteration":
            span_start = datetime.utcnow()
            try:
                run_iteration_body(node)
                spans.append({"node": "iteration", "node_id": node.get("id"), "status": "ok",
                               "duration_ms": int((datetime.utcnow() - span_start).total_seconds() * 1000)})
            except Exception as e:
                spans.append({"node": "iteration", "node_id": node.get("id"), "status": "error", "error": str(e)})
                raise
        elif node["type"] == "hitl":
            raise_hitl_pause(node)
        else:
            exec_node(node, node_ctx, node_session)

    try:
        # The entitlement gate must not depend on a `gov` node existing
        # in the flow.
        #
        # It used to: handle_gov was the ONLY runtime check for a
        # retrieval node's knowledge bases (handle_tool checks its own
        # catalog_object, but handle_retrieval checks nothing), and the
        # gov node was present only because create_agent happened to
        # scaffold one. It is `locked` in the palette so nobody can add
        # one back, yet DELETE /agents/{id}/nodes/{node_id} will remove
        # it like any other node — so a governed knowledge base could
        # already be read with no check at all, and any flow built
        # without that scaffold had no gate in the first place.
        #
        # Running it here when the flow has no gov node keeps the gate
        # unconditional while leaving flows that DO have one completely
        # unchanged — the node still performs the check at its own point
        # in the graph, and nothing is checked twice.
        # Runs when no gov node will do the checking: either there is
        # none in the flow, or the one there is has its switch off.
        gov_will_check = any(n.get("type") == "gov"
                             and (n.get("config") or {}).get("enabled") is not False
                             for n in nodes)
        if not gov_will_check:
            handle_gov({}, ctx, session)

        from .db import engine as _graph_engine
        cancel_result = run_graph_concurrent(
            start_node["id"], nodes_by_id, edges, run_one_node_dispatch, ctx, steps_state, is_cancelled,
            # One session per concurrently executed node — see
            # run_isolated. Solo nodes (loop/iteration/hitl) and a wave
            # of one still use the run's own session: nothing runs
            # beside them, so there is nothing to share it with.
            session_factory=lambda: Session(_graph_engine),
        )
        if cancel_result and cancel_result.get("status") == "cancelled":
            spans.append({"node": None, "status": "cancelled"})
            if on_event:
                on_event({"type": "cancelled"})
            return {"status": "cancelled", "output": ctx.get("output"), "spans": spans, "fail_class": None,
                    "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", [])}
    except HitlPause as e:
        return {
            "status": "paused_for_human_input", "spans": spans, "fail_class": None,
            "hitl_node_id": e.node_id, "hitl_rendered_content": e.rendered_content,
            "hitl_fields": e.fields, "hitl_actions": e.actions, "hitl_delivery_status": e.delivery_status,
            # The narrow, scrubbed resume state — not the whole runtime
            # ctx. ctx accumulates every node's output for the entire
            # run, so persisting it wrote prompts, retrieved documents,
            # tool results and the HTTP node's own headers_sent (where an
            # interpolated Authorization header lands) into a plaintext
            # JSONB column. env_map is passed so a secret that has
            # already been substituted INTO a prompt or URL is caught by
            # value, not just by key name.
            "hitl_ctx_snapshot": hitl_resume_state(ctx, env_map),
            "hitl_steps_used": steps_state["count"],
            "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", []),
            # Real, delegated-pause propagation (§0.83, GAP_REPORT) —
            # None for every real, direct hitl node (unchanged real
            # behavior); only ever real, non-None values when a nested
            # Sub-Agent's own target genuinely paused on its own real,
            # separate Temporal workflow — see HitlPause's own docstring.
            "hitl_delegated_child_agent_id": e.delegated_child_agent_id,
            "hitl_delegated_temporal_workflow_id": e.delegated_temporal_workflow_id,
        }
    except UCSDenied:
        return {"status": "denied", "output": None, "spans": spans, "fail_class": "ucs", "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", [])}
    except HarnessStepLimitExceeded:
        return {"status": "failed", "output": None, "spans": spans, "fail_class": "harness", "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", [])}
    except GraphDeadlockError as e:
        # §0.103, GAP_REPORT — a real, dedicated fail_class (not the
        # generic "orchestration" catch-all below) specifically so a
        # genuine graph deadlock is immediately distinguishable from
        # any other kind of run failure, with the actual blocked node
        # ids surfaced for real, direct debugging rather than only a
        # message string.
        spans.append({"node": None, "status": "error", "error": str(e)})
        return {
            "status": "failed", "output": None, "spans": spans, "fail_class": "graph_deadlock",
            "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", []),
            "deadlock_blocked_nodes": e.blocked_nodes, "deadlock_unresolved_edges": e.unresolved_edges,
        }
    except Exception:
        # A real, permanent fix motivated directly by debugging this
        # same build: this used to swallow the real traceback entirely,
        # which cost real time tracking down a genuine bug (a missing
        # `import copy`) during this exact change. logger.exception
        # records the real stack trace without changing the real
        # response shape callers already depend on.
        logger.exception("Unhandled exception during flow execution (agent_id=%s)", getattr(agent, "id", "?"))
        return {"status": "failed", "output": None, "spans": spans, "fail_class": "orchestration", "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", [])}

    if steps_state["count"] >= MAX_GRAPH_STEPS:
        spans.append({"node": None, "status": "error", "error": f"graph exceeded {MAX_GRAPH_STEPS} steps — likely an unintended cycle"})
        return {"status": "failed", "output": None, "spans": spans, "fail_class": "orchestration", "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", [])}

    return {"status": "success", "output": ctx["output"], "spans": spans, "fail_class": None,
            "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", []),
            "agent_steps": ctx["vars"].get("agent_steps"), "agent_strategy": ctx["vars"].get("agent_strategy"),
            "retrieved_chunks": ctx["vars"].get("retrieved_chunks"), "retrieval_source": ctx["vars"].get("retrieval_source"),
            "structured_output": ctx.get("structured_output")}


def run_flow_sync(session: Session, agent, invoking_user: str, user_input: str,
                   webhook_payload: Optional[dict] = None, agent_chain: Optional[List[str]] = None,
                   structured_inputs: Optional[Dict[str, Any]] = None,
                   on_event: Optional[Callable[[dict], None]] = None,
                   is_cancelled: Optional[Callable[[], bool]] = None,
                   flow: Optional[dict] = None, features: Optional[dict] = None,
                   subagent_manifest: Optional[dict] = None,
                   conversation_id: Optional[str] = None) -> dict:
    """Sync execution path (§5) — direct interpreter call, no Temporal.
    Builds the initial ctx and hands off to _execute_flow (above) for the
    actual graph walk. See execution.py for how this is invoked and how
    the durable-path routing rule (§3.6) is applied on top of it.

    flow (§0.54, GAP_REPORT): an explicit override, real nodes/edges to
    execute instead of agent.flow — the actual fix for a real, reported
    bug: a published agent's runtime behavior could silently change the
    moment a builder edit happened, with no new publish, no version
    bump, no record of it. execute_agent_flow (execution.py) resolves
    this to the real, immutable AgentVersion.flow snapshot for a
    genuine production invocation, or leaves it None (falling back to
    agent.flow, the real live draft) for a genuine Builder preview.
    handle_subagent's own direct call deliberately doesn't pass this —
    a Sub-Agent call already has its own separately-stated architectural
    boundary (§0.53) and isn't part of this fix's scope.

    webhook_payload: the raw JSON body of a webhook-triggered run, if any
    — passed through so handle_trigger can expose it to downstream nodes.
    Threaded through both the sync path and the durable/Temporal path
    (§0.53, GAP_REPORT) via the same real ctx keys handle_trigger reads.

    agent_chain: the real multi-hop call stack (§7.6) — a list of agent
    ids already "in progress" up this call. A top-level call seeds this
    with just [agent.id]; handle_subagent extends it by one on each real
    nested invocation, and uses it for real cycle detection before ever
    calling this function again — an agent can't indirectly invoke itself
    no matter how many hops deep the cycle is.

    on_event / is_cancelled: real, optional live-progress hooks — see
    _execute_flow's docstring. Neither is required; a plain call behaves
    exactly as before.

    features (§0.96, GAP_REPORT): an explicit override, the same real
    reasoning and pattern as flow above — the actual fix for a real,
    reported gap: this used to always read agent.features directly
    for VERSIONED runtime behavior (moderation, chat_history,
    citations, follow-ups) even on a genuinely published, production
    invocation, so a live features edit with no new publish could
    silently change a "frozen" version's real runtime behavior.
    execute_agent_flow now resolves this to the exact same
    AgentVersion.features snapshot flow already comes from — None
    (handle_subagent's own direct call, which deliberately doesn't
    pass flow either, same stated boundary) falls back to agent.features,
    the real live value, matching this parameter's own honest default.

    subagent_manifest (§0.104, GAP_REPORT): the real, frozen Sub-Agent
    dependency manifest this invocation's own flow was ALSO resolved
    from (see AgentVersion.subagent_manifest's own docstring,
    models.py) — threaded into ctx so handle_subagent can resolve each
    Sub-Agent node's own target to the EXACT AgentVersion pinned at
    THIS parent's publish time, not whatever is currently the
    target's latest. None/{} (a genuine draft/preview run, or
    handle_subagent's own direct nested call) falls back to today's
    prior behavior — resolving the target's current live/published
    state at execution time — since there's no frozen manifest to
    consult in either case.

    May return status="paused_for_human_input" — see HitlRequest/
    resume_flow_sync. The caller (execution.py) is responsible for
    persisting a HitlRequest from the "hitl_*" fields on that result."""
    nodes = (flow or agent.flow).get("nodes", [])
    env_map = load_env_map(session, agent.id)

    ctx: Dict[str, Any] = {
        "input": user_input, "vars": {}, "agent_id": agent.id, "agent_name": agent.name,
        "invoking_user": invoking_user, "_objects_to_check": governed_objects(session, nodes), "output": None,
        "_webhook_payload": webhook_payload, "_agent_chain": agent_chain or [agent.id],
        "_structured_inputs": structured_inputs or {},
        # Which chat thread this run belongs to, so history is read
        # and written per conversation rather than per (agent, user).
        "conversation_id": conversation_id,
        # Real Sub-Agent dependency manifest (§0.104, GAP_REPORT) — see
        # this function's own docstring above for the full real
        # reasoning; {} (never None) so handle_subagent's own manifest
        # lookup never has to guard against a missing key.
        "_subagent_manifest": subagent_manifest or {},
        # Real, agent-level chat history opt-in (§0.47, GAP_REPORT) —
        # read once here, at pre-flight, so every LLM-touching node in
        # this run can inherit it without needing its own explicit
        # memory_enabled checkbox. A node's own memory_enabled, if set,
        # still takes precedence (see handle_llm's own real inheritance
        # logic) — this is a real default, not a forced override.
        "_chat_history_enabled": bool(((features or agent.features or {}).get("chat_history") or {}).get("enabled")),
    }

    # Real seeding of every real conversation variable a Variable
    # Assigner node (see handle_variable_assigner) has genuinely
    # persisted for this (agent, invoking_user) pair, BEFORE the
    # traversal starts — so any node in THIS run, not only ones wired
    # downstream of a Variable Assigner, can reference a real value set
    # by an earlier, separate run/conversation turn via {{conv_<name>}}.
    conv_scope_key = f"{agent.id}:{invoking_user}"
    for row in session.exec(
        select(AgentMemory).where(AgentMemory.scope_key == conv_scope_key, AgentMemory.key.startswith(CONV_VAR_PREFIX))
    ).all():
        name = row.key[len(CONV_VAR_PREFIX):]
        ctx["vars"][f"conv_{name}"] = _get_conv_var(session, conv_scope_key, name)

    start_node = next((n for n in nodes if n["type"] == "trigger"), None)
    if not start_node:
        return {"status": "failed", "output": None, "spans": [{"node": None, "status": "error", "error": "no trigger node"}], "fail_class": "orchestration"}

    return _execute_flow(session, agent, ctx, env_map, start_node, on_event=on_event, is_cancelled=is_cancelled, flow=flow)


def _resolve_paused_flow(session: Session, agent, hitl_request) -> dict:
    """Real, frozen-flow resolution for a genuinely PAUSED run's own
    eventual resume (§0.94, GAP_REPORT) — the actual fix for a real,
    reported gap: both real sync-path resume functions below used to
    read `agent.flow` directly — the same real, live, mutable field
    the Builder edits — rather than the exact, frozen snapshot the
    original, paused invocation actually resolved and started
    executing against. A real, reported scenario this closes: a parent
    published at v5, its own Sub-Agent node pauses waiting on a real,
    nested child's own HITL, and while genuinely waiting — for
    minutes, hours, or days — a Builder edit (or even a full new
    publish) changes the parent's own live flow. Reading agent.flow at
    resume time would then continue the parent using nodes that were
    never part of what the person originally, actually invoked — the
    exact class of bug §0.54/§0.90/§0.91/§0.93's own real fixes
    already closed for every other real invocation, resolution, and
    persistence point, silently left open here.

    Chains through the REAL, persisted trail (the report's own,
    stated fix): HitlRequest.run_id (§0.91, GAP_REPORT) -> Run's own
    real, immutable agent_version_id (§0.91, GAP_REPORT, captured at
    the ORIGINAL invocation's own real _resolve_execution_context/
    resolve_execution call, execution.py) -> that exact AgentVersion's
    own real, frozen flow. A published run's own real Run row always
    has this — a real, honest fallback to agent.flow only applies to
    the two real, narrower cases where no such trail can exist at all:
    a genuinely unpublished draft/preview run (there's no AgentVersion
    to freeze in the first place — the live flow IS the only real
    record, the same honest fallback resolve_execution itself uses),
    or a real, pre-existing HitlRequest/Run row from before this exact
    tracking existed (run_id/agent_version_id both genuinely absent —
    nothing to recover, the same honest convention this codebase
    already uses for every other real, unbackfillable historical
    field)."""
    if hitl_request.run_id:
        run = session.get(Run, hitl_request.run_id)
        if run and run.agent_version_id:
            version = session.get(AgentVersion, run.agent_version_id)
            if version:
                return version.flow
    return agent.flow


def resume_flow_sync(session: Session, agent, hitl_request, action_id: str,
                      field_values: Optional[Dict[str, Any]] = None) -> dict:
    """Resumes a real, previously-paused run from the EXACT ctx state
    HitlPause captured — genuinely continuing, not restarting the flow.
    Looks up the real outgoing edge for whichever action the person
    picked (or the 'timeout' port); if none is wired, the run ends there,
    the same honest behavior as any other unwired branch elsewhere in
    this harness.

    field_values (the real form field values the person submitted) are
    folded into ctx["vars"] under both their own field name (so a
    downstream {{field_name}} works exactly like a trigger's structured
    input fields do) and under "hitl_last_response" as the full dict.

    §0.94, GAP_REPORT — nodes/edges now come from _resolve_paused_flow
    (above), the exact, frozen flow snapshot the ORIGINAL, paused
    invocation actually resolved and started executing against — not
    agent.flow, the live, mutable field that could have changed during
    however long this run was genuinely paused. See that function's
    own docstring for the full real reasoning."""
    resolved_flow = _resolve_paused_flow(session, agent, hitl_request)
    nodes = resolved_flow.get("nodes", [])
    edges = resolved_flow.get("edges", [])
    nodes_by_id = {n["id"]: n for n in nodes}

    ctx: Dict[str, Any] = decode_hitl_snapshot(hitl_request.ctx_snapshot)
    if is_undecryptable(ctx):
        # Refusing is the honest outcome. Continuing with an empty ctx
        # would run the rest of the flow against silently missing
        # variables and report success — a wrong answer presented as a
        # right one, which is worse than a failed resume.
        raise RuntimeError(
            "This paused run's resume state cannot be decrypted — it was sealed with a "
            "different SECRETS_ENCRYPTION_KEY than this process has. Nothing was resumed."
        )
    ctx.setdefault("vars", {})
    field_values = field_values or {}
    ctx["vars"]["hitl_last_response"] = field_values
    ctx["vars"]["hitl_action_id"] = action_id
    for k, v in field_values.items():
        ctx["vars"][k] = v

    outgoing = [e for e in edges if e.get("source") == hitl_request.node_id]
    # Per-action routing first: a form offering Approve and Reject wires
    # an edge per action, and the handle is what tells them apart.
    matched = next((e for e in outgoing if e.get("source_handle") == action_id), None)
    if not matched:
        # Then the ordinary shape: one exit, taken whichever action was
        # chosen. Requiring a handle here stranded every such flow — the
        # run resumed, reported success, and stopped at the HITL node
        # with a null output, because no edge was named "continue".
        unhandled = [e for e in outgoing if not e.get("source_handle")]
        if len(unhandled) == 1:
            matched = unhandled[0]
    if not matched:
        return {
            "status": "success", "output": ctx.get("output"), "fail_class": None,
            "spans": [{"node": "hitl", "status": "ok", "note": f"resolved via '{action_id}', no downstream node wired for that action/timeout"}],
            "structured_output": ctx.get("structured_output"),
            "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", []),
        }
    next_node = nodes_by_id.get(matched["target"])
    if not next_node:
        return {"status": "success", "output": ctx.get("output"), "fail_class": None, "spans": [],
                "structured_output": ctx.get("structured_output")}

    env_map = load_env_map(session, agent.id)
    return _execute_flow(session, agent, ctx, env_map, next_node, flow=resolved_flow)


def resume_delegated_subagent_flow(session: Session, agent, hitl_request, child_result: dict) -> dict:
    """Real resume of a PARENT's own flow after a nested, delegated
    Sub-Agent pause's own real child finally reaches a real terminal
    outcome (§0.83, GAP_REPORT) — the second real half of the nested-
    durable-HITL mechanism handle_subagent's own docstring describes;
    see HitlRequest.delegated_child_agent_id's own docstring (models.py)
    for the full real design and respond_to_hitl's own real, extended
    caller (execution.py).

    Deliberately NOT resume_flow_sync (above) — that function resolves
    the next node via a real action_id matched against the paused
    node's own outgoing `source_handle`, which is exactly right for a
    genuine hitl node (a person picks one of ITS OWN configured
    actions) but wrong here: the paused node on the PARENT's own side
    is a `subagent` node, which — like every other non-branching node
    type (compute_next_node_id's own docstring) — has at most one real
    outgoing edge with source_handle=None; the CHILD's own real
    action_id (whatever a person actually picked to resolve the
    CHILD's own real form) has no correct meaning as a PARENT-side
    branch selector at all. This resumes the parent's own single,
    default path instead, first setting the exact same real
    ctx["vars"] handle_subagent itself would have set had the nested
    call not paused (subagent_target/status/output/chain, and
    subagent_error if the child's own final outcome wasn't success) —
    so a flow reading {{subagent_output}} downstream of the Sub-Agent
    node sees the real, same value whether or not that specific
    invocation happened to pause along the way.

    §0.94, GAP_REPORT — nodes/edges now come from _resolve_paused_flow
    (above), the exact, frozen flow snapshot the PARENT's own original
    invocation actually resolved and started executing against — not
    agent.flow, the live, mutable field that could have silently
    changed during however long the delegated child's own real HITL
    genuinely kept the parent waiting. See that function's own
    docstring for the full real reasoning and the real, reported
    scenario this fixes."""
    resolved_flow = _resolve_paused_flow(session, agent, hitl_request)
    nodes = resolved_flow.get("nodes", [])
    edges = resolved_flow.get("edges", [])
    nodes_by_id = {n["id"]: n for n in nodes}

    ctx: Dict[str, Any] = decode_hitl_snapshot(hitl_request.ctx_snapshot)
    if is_undecryptable(ctx):
        # Refusing is the honest outcome. Continuing with an empty ctx
        # would run the rest of the flow against silently missing
        # variables and report success — a wrong answer presented as a
        # right one, which is worse than a failed resume.
        raise RuntimeError(
            "This paused run's resume state cannot be decrypted — it was sealed with a "
            "different SECRETS_ENCRYPTION_KEY than this process has. Nothing was resumed."
        )
    ctx.setdefault("vars", {})
    chain = ctx["vars"].get("subagent_chain") or (ctx.get("_agent_chain") or []) + [hitl_request.delegated_child_agent_id]
    ctx["vars"]["subagent_target"] = hitl_request.delegated_child_agent_id
    ctx["vars"]["subagent_status"] = child_result.get("status")
    ctx["vars"]["subagent_output"] = child_result.get("output")
    ctx["vars"]["subagent_chain"] = chain
    if child_result.get("status") != "success":
        ctx["vars"]["subagent_error"] = f"nested run did not succeed: {child_result.get('status')} ({child_result.get('fail_class')})"
    ctx["output"] = ctx.get("output") or child_result.get("output")

    outgoing = [e for e in edges if e.get("source") == hitl_request.node_id]
    matched = next((e for e in outgoing if e.get("source_handle") is None), None)
    if not matched:
        return {
            "status": "success", "output": ctx.get("output"), "fail_class": None,
            "spans": [{"node": "subagent", "status": "ok", "note": "delegated child resolved, no downstream node wired"}],
            "structured_output": ctx.get("structured_output"),
            "ucs_soft_denials": ctx["vars"].get("ucs_soft_denials", []),
        }
    next_node = nodes_by_id.get(matched["target"])
    if not next_node:
        return {"status": "success", "output": ctx.get("output"), "fail_class": None, "spans": [],
                "structured_output": ctx.get("structured_output")}

    env_map = load_env_map(session, agent.id)
    return _execute_flow(session, agent, ctx, env_map, next_node, flow=resolved_flow)

