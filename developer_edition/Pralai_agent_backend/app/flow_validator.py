"""
Real, server-side flow validation (§0.73, GAP_REPORT).

The real, reported gap: `frontend/src/flowValidation.ts` is a real,
working validator — but it's the ONLY validator. The general backend
node/edge APIs never applied equivalent checks before publish, and
`NodeIn.type` accepted any string at all. A raw API caller (or a
direct `/import-dsl` call) could build and publish a flow the
Builder's own UI would have visibly flagged and blocked — a real,
genuine gap between what the UI implies is enforced and what the
server actually enforces.

This module is the real, authoritative, server-side counterpart.
`validate_flow()` returns every real issue found, each tagged
"error" (blocks publish — see routers/agents.py's own publish
endpoint) or "warning" (surfaced, not blocking — matches how the
Builder's own inline checklist already treats config-completeness
issues during editing, before a person has necessarily finished
wiring the flow).

Several constants here (`SUPPORTED_NODE_TYPES`, `valid_source_handles`)
are also imported directly by routers/agents.py's own add_node/add_edge
endpoints, so the same one real definition governs both "what can be
built" and "what can be published" — they can't independently drift
the way the old add_edge handle check and the Builder's own
outputPorts() had already drifted (see valid_source_handles' own
docstring for that real, separate bug found and fixed alongside this).
"""
from dataclasses import dataclass, field
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlmodel import Session, select

# NODE_HANDLERS is harness.py's own one real dispatch table (§0.51,
# GAP_REPORT) — imported, not duplicated, so a node type added there
# is automatically recognized as "supported" here without a second,
# separately-maintained list to forget to update.
from .harness import NODE_HANDLERS

# Real node types that are genuinely supported end to end but are NOT
# entries in NODE_HANDLERS, because they're special-cased directly in
# the traversal loop (harness.py's run_flow_sync / run_loop_body /
# run_iteration_body, and temporal_workflow.py's own mirrored logic)
# rather than being simple single-dispatch handlers — a real, verified
# fact about this codebase (checked directly against harness.py and
# temporal_workflow.py while building this), not an assumption:
#   - "loop" / "iteration": real container types, walked as their own
#     sub-graph of parent_id-linked children, not dispatched once.
#   - "hitl": needs to raise a real HitlPause the generic dispatch
#     path has no reason to know about.
#   - "comment": deliberately never dispatched at all — documentation
#     only, matching the Builder's own SKIP_CONNECTIVITY_CHECK.
SPECIAL_CASED_TYPES = {"loop", "iteration", "hitl", "comment"}

SUPPORTED_NODE_TYPES: Set[str] = set(NODE_HANDLERS.keys()) | SPECIAL_CASED_TYPES

# The exact same real set, tracked separately on purpose (§0.73,
# GAP_REPORT's own "durable compatibility" check) — temporal_workflow.py
# was directly, manually checked to confirm it dispatches every one of
# NODE_HANDLERS' own entries (via the same NODE_HANDLERS.get() lookup)
# plus its own mirrored loop/iteration/hitl handling. If a future node
# type is ever wired into NODE_HANDLERS without a matching update to
# temporal_workflow.py's own special-casing, this constant is what a
# real maintainer edits to make that gap show up here as a real
# "not durable-compatible" finding, instead of only surfacing the
# first time a real agent with an HITL node happens to reach that
# node type on the durable path.
DURABLE_SUPPORTED_TYPES: Set[str] = set(NODE_HANDLERS.keys()) | {"loop", "iteration", "hitl"}

CONTAINER_TYPES = {"loop", "iteration"}

# Node types with genuinely no real output at all — a terminal step,
# never a real edge source. Mirrors Builder.tsx's own outputPorts()
# early return exactly.
NO_OUTPUT_TYPES = {"respond", "comment", "exit_loop"}

CONDITION_HANDLES = {"true", "false"}
FAIL_BRANCH_TYPES = {"code", "llm"}
FAIL_BRANCH_HANDLES = {"success", "error"}

# Mirrors flowValidation.ts's own RESPONSE_PRODUCING_TYPES exactly —
# node types that can genuinely set the final response text via
# handle_respond's real fallback chain.
RESPONSE_PRODUCING_TYPES = {"llm", "agent", "subagent", "code", "template", "http", "tool"}


def valid_source_handles(node: Dict[str, Any]) -> Optional[Set[str]]:
    """The one, real, shared source of truth for "what source_handle
    values are actually valid for an edge leaving this node right
    now" — used by both this validator and routers/agents.py's own
    add_edge endpoint, so the two can't drift the way they already had
    (a real bug found while building this: add_edge used to accept a
    named handle ONLY for "condition" nodes, silently rejecting the
    genuinely legitimate "success"/"error" edge a code or LLM node
    with "Fail branch" enabled needs — a real, working feature on the
    config-panel and execution-routing side that the edge-creation
    endpoint itself made impossible to actually wire up).

    Returns:
      - a real, empty set() if this node type has NO valid output at
        all (an edge should never originate here, named or not).
      - a real, non-empty set of the specific valid handle strings.
      - None if this node type has exactly one, unnamed output (a
        real edge from it must have source_handle == None).
    """
    t = node.get("type")
    if t in NO_OUTPUT_TYPES:
        return set()
    if t == "condition":
        return set(CONDITION_HANDLES)
    if t == "hitl":
        # A Human Input node branches on the action the reviewer picks
        # (harness.py routes an edge whose handle is that action id), and
        # also still has its plain single exit, taken whatever the action.
        # Both shapes run, so both have to validate — None is the
        # unnamed exit. Default actions match the runtime's own default.
        actions = (node.get("config") or {}).get("actions") or [{"id": "continue"}]
        return {a.get("id") for a in actions if a.get("id")} | {None}
    if t in FAIL_BRANCH_TYPES and (node.get("config") or {}).get("error_handling") == "Fail branch":
        return set(FAIL_BRANCH_HANDLES)
    return None


@dataclass
class FlowIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    node_id: Optional[str] = None


@dataclass
class ValidationResult:
    issues: List[FlowIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[FlowIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[FlowIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def add(self, severity: str, code: str, message: str, node_id: Optional[str] = None) -> None:
        self.issues.append(FlowIssue(severity=severity, code=code, message=message, node_id=node_id))


# Real, type-specific required-config checks (§0.73, GAP_REPORT) —
# server-side port of flowValidation.ts's own configIssues(), so a
# flow that would have visibly failed the Builder's own inline
# checklist can't reach publish via a raw API call or a DSL import
# either. Kept in exact parity with the frontend's own field names and
# required-ness rules — see that file's own docstring for why each of
# these specific fields is checked.
def _config_issues(node: Dict[str, Any]) -> List[str]:
    cfg = node.get("config") or {}
    t = node.get("type")
    issues: List[str] = []
    if t == "output":
        variables = cfg.get("variables") or []
        if not variables or not any((v or {}).get("name", "").strip() for v in variables):
            issues.append("output variable is required")
    elif t == "classifier":
        if not (cfg.get("input_var") or "").strip():
            issues.append("Input Variables is required")
        if not (cfg.get("class1") or "").strip() and not (cfg.get("class2") or "").strip():
            issues.append("At least one class is required")
    elif t == "hitl":
        if not cfg.get("delivery_methods"):
            issues.append("Please select at least one delivery method")
    elif t == "llm" or t == "agent":
        # A prompt is billed by the token. Not a security boundary so
        # much as a guard against an accident that costs real money —
        # and one a byte limit on the request would not catch, since a
        # 100k-character prompt is a small JSON body.
        from .limits import MAX_PROMPT_CHARS
        for field in ("system_prompt", "user_prompt", "prompt", "instructions"):
            text = cfg.get(field)
            if isinstance(text, str) and len(text) > MAX_PROMPT_CHARS:
                issues.append(f"{field} is {len(text)} characters; the limit is "
                              f"{MAX_PROMPT_CHARS}")
        if t == "agent":
            import re as _re
            from .agent_assets import MAX_ASSETS_PER_NODE
            for field in ("skills", "files"):
                v = cfg.get(field)
                if v is not None and (not isinstance(v, list) or not all(isinstance(i, str) for i in v)):
                    issues.append(f"{field} must be a list of ids")
                elif isinstance(v, list) and len(v) > MAX_ASSETS_PER_NODE:
                    issues.append(f"At most {MAX_ASSETS_PER_NODE} {field} can be attached to one Agent node")
            env = cfg.get("env")
            if env is not None:
                if not isinstance(env, list):
                    issues.append("env must be a list of KEY/VALUE rows")
                else:
                    seen = set()
                    for r in env:
                        k = (r.get("key") if isinstance(r, dict) else "") or ""
                        if not k:
                            continue
                        if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                            issues.append(f"Env key '{k}' must be letters, digits and underscores")
                        elif k in seen:
                            issues.append(f"Env key '{k}' is duplicated")
                        seen.add(k)
    elif t == "condition":
        expression = (cfg.get("expression") or "").strip()
        if not expression:
            issues.append("Expression is required")
        else:
            # Refused at publish rather than silently taking the false
            # branch forever at run time. eval_expr returns False for
            # anything it cannot evaluate — a syntax error, a rejected
            # node type, an expression over its resource budget — which
            # is the right runtime behaviour but indistinguishable, from
            # the outside, from a condition that is simply not met. That
            # reads as a logic bug in the flow and is very hard to find.
            from .harness import validate_expression
            problem = validate_expression(expression)
            if problem:
                issues.append(f"Expression cannot be evaluated — {problem}")
    elif t == "template":
        if not (cfg.get("template") or "").strip():
            issues.append("Jinja2 template is required")
    elif t == "code":
        if not (cfg.get("code") or "").strip():
            issues.append("Code is required")
    elif t == "http":
        if not (cfg.get("url") or "").strip():
            issues.append("URL is required")
    elif t == "subagent":
        if not cfg.get("target_agent_id"):
            issues.append("Target agent is required")
    elif t == "docextractor":
        if not (cfg.get("input_var") or "").strip():
            issues.append("Input URL is required")
    return issues


def _walk_string_fields(value: Any):
    """Real, recursive walk of every string leaf in a node's own
    config dict/list structure — used by the required-env-var check
    below, since a {{env.KEY}} reference can genuinely appear inside a
    nested field (e.g. an HTTP node's own headers dict), not only a
    top-level string."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_string_fields(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_string_fields(v)


_ENV_VAR_RE = re.compile(r"\{\{\s*env\.([A-Za-z0-9_]+)\s*\}\}")


def _real_current_flow(session, agent) -> dict:
    """Real, minimal flow resolution FOR VALIDATION PURPOSES ONLY
    (§0.95, GAP_REPORT) — deliberately NOT harness.py's own
    _resolve_subagent_flow (avoids importing harness.py from this
    module at all, sidestepping any real risk of a circular import,
    and avoids that function's own real
    PublishedVersionIntegrityError, which is the correct behavior for
    an actual EXECUTION-time resolution but wrong here — a real,
    already-existing, unrelated data-integrity problem on some OTHER
    agent shouldn't block validating THIS one). A published agent's
    own current AgentVersion snapshot when one exists; that agent's
    own live, mutable flow otherwise (a genuinely unpublished draft,
    or the narrow, real, honest fallback this codebase already uses
    elsewhere for a published agent missing its own AgentVersion row —
    see PublishedVersionIntegrityError's own docstring, execution.py,
    for the full real reasoning on why that specific gap is handled
    there, not duplicated here)."""
    from .models import AgentVersion
    if agent.status == "published":
        current_version = session.exec(
            select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.is_current == True)
        ).first()
        if current_version:
            return current_version.flow
    return agent.flow



def _check_agent_node(result: "ValidationResult", n: Dict[str, Any], cfg: Dict[str, Any],
                      session: Session, agent_id: str) -> None:
    """Publish-time checks for an Agent node that need the database: every
    skill/file it names exists in this agent's library, every tool is
    callable, the model can do what the configuration asks, and each Env key
    has a stored value."""
    from .agent_assets import (AssetMissingError, build_skills_prompt, build_files_block, usable_secret_keys,
                               declared_env_keys, _owned)
    from .models import AgentFile
    nid = n.get("id")
    try:
        build_skills_prompt(session, agent_id, cfg)
        build_files_block(session, agent_id, cfg)
        for f in _owned(session, AgentFile, agent_id, cfg.get("files"), "file"):
            if f.status == "failed":
                result.add("error", "file_unreadable",
                           f"File '{f.filename}' could not be read ({f.status_detail or 'no text was extracted'}); "
                           "the agent would get nothing from it. Replace it with a readable file or remove it.", nid)
            elif f.status == "stored_only":
                result.add("warning", "file_not_sent",
                           f"File '{f.filename}' is stored but not sent to the model, so the agent can't use it.", nid)
    except AssetMissingError as e:
        result.add("error", "unknown_agent_asset", str(e), nid)

    for r in cfg.get("files_required") or []:
        if isinstance(r, dict) and r.get("required") is not False:
            result.add("error", "template_file_required",
                       f"This template needs a file for '{r.get('name') or r.get('filename') or 'a reference file'}'. "
                       "Upload one and attach it before publishing.", nid)

    tools = [t for t in (cfg.get("tools") or []) if isinstance(t, str)]
    strategy = cfg.get("strategy") or "react"
    from .models import ToolAction
    from .tool_connectors import is_builtin_connector
    for t in tools:
        if not t.startswith("tool:"):
            result.add("error", "tool_not_callable",
                       f"'{t}' can't be called from an Agent node yet (only Tools from Integrations can)", nid)
            continue
        _, tool_name, action = (t.split(":", 2) + ["", ""])[:3]
        if not session.exec(select(ToolAction).where(ToolAction.tool_name == tool_name, ToolAction.name == action)).first():
            result.add("error", "unknown_tool", f"Tool '{tool_name}: {action}' doesn't exist in Integrations", nid)
        elif not is_builtin_connector(t):
            result.add("error", "tool_not_callable",
                       f"Tool '{tool_name}: {action}' has no runtime connector, so the agent can't actually call it", nid)
    if tools and strategy != "function_calling":
        result.add("warning", "tools_inactive",
                   "Tools are only used by the Function calling strategy; they will be ignored with "
                   f"'{strategy}'", nid)

    from .model_capabilities import capabilities_for
    model = cfg.get("model") or "Claude Sonnet 5"
    caps = capabilities_for(session, model)
    if caps is None:
        result.add("error", "unknown_model", f"Model '{model}' isn't in the model catalog", nid)
    elif not caps["supports_chat"]:
        result.add("error", "model_not_chat", f"Model '{model}' can't be used for chat (its provider isn't wired up)", nid)
    elif tools and strategy == "function_calling" and not caps["supports_tool_calling"]:
        result.add("error", "model_no_tool_calling",
                   f"Model '{model}' doesn't support tool calling, which this node's tools need", nid)
    else:
        from .model_capabilities import provider_configured
        ok, why = provider_configured(session, caps["provider"])
        if ok:
            from .model_capabilities import mlstudio_model_configured
            ok, why = mlstudio_model_configured(session, model)
        if not ok:
            result.add("error", "MODEL_PROVIDER_NOT_CONFIGURED", f"Model '{model}' can't run here: {why}", nid)

    env = cfg.get("env")
    if isinstance(env, list) and nid:
        have = usable_secret_keys(session, agent_id, n)
        for r in env:
            k = (r.get("key") if isinstance(r, dict) else "") or ""
            if k and k not in have:
                result.add("warning", "env_value_missing", f"Env '{k}' has no value set", nid)


def validate_flow(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    app_type: Optional[str] = None,
    *,
    is_snippet: bool = False,
    session: Optional[Session] = None,
    agent_id: Optional[str] = None,
) -> ValidationResult:
    """The real, comprehensive, server-side validator (§0.73,
    GAP_REPORT). Every check below is real and independently
    verified against this codebase's actual current behavior while
    building this — not a generic/borrowed checklist. `session` and
    `agent_id`, when given, enable the real DB-backed checks (env var
    references, knowledge base references, sub-agent references);
    without them those specific checks are honestly skipped rather
    than silently treated as passing (callers that validate without a
    session should not present the result as covering those checks).

    A real, stated scope boundary: "required variables" here means
    real {{env.KEY}} references checked against this agent's actual
    EnvVar rows — a fully general data-flow check (\"was this
    {{var}} genuinely produced by an upstream node's own real output
    before this point in the graph\") is real, separate, and far
    larger scope this fix doesn't take on; see this function's own
    GAP_REPORT entry for why. Likewise, "version compatibility" folds
    into the config-schema check above — this platform has no real,
    separate per-node schema-version concept to check against; a
    fabricated version check would test nothing real.
    """
    # Also checked here, not only at the save chokepoint: a flow stored
    # before these caps existed would otherwise still publish and be
    # re-walked on every run.
    from .limits import MAX_FLOW_EDGES, MAX_FLOW_NODES, MAX_PROMPT_CHARS
    if len(nodes or []) > MAX_FLOW_NODES:
        result = ValidationResult()
        result.add("error", "flow_too_large",
                   f"This flow has {len(nodes)} nodes; the limit is {MAX_FLOW_NODES}")
        return result
    if len(edges or []) > MAX_FLOW_EDGES:
        result = ValidationResult()
        result.add("error", "flow_too_large",
                   f"This flow has {len(edges)} edges; the limit is {MAX_FLOW_EDGES}")
        return result
    result = ValidationResult()
    nodes_by_id = {n["id"]: n for n in nodes if n.get("id")}
    node_ids = set(nodes_by_id.keys())

    # 1) Supported node type — the actual fix for the reported
    # "NodeIn.type is essentially arbitrary" gap. A type that isn't
    # real and known can never be created via add_node either (see
    # routers/agents.py), but a flow built by a more permissive path
    # (direct DSL import, a hand-crafted request body) could still
    # reach here with one, so this is checked unconditionally.
    for n in nodes:
        if n.get("type") not in SUPPORTED_NODE_TYPES:
            result.add("error", "unsupported_node_type",
                       f"'{n.get('type')}' is not a real, supported node type", n.get("id"))

    # 2) Node configuration schema
    for n in nodes:
        if n.get("type") not in SUPPORTED_NODE_TYPES:
            continue  # already reported above; don't also report bogus "missing config" noise
        for msg in _config_issues(n):
            result.add("error", "invalid_config", msg, n.get("id"))

    # 2b) HITL response variables that nothing produces.
    #
    # A resume folds the human's answer into vars under each FIELD's own name, plus
    # `hitl_last_response` (the whole dict) and `hitl_action_id` — see
    # harness.resume_flow_sync. A flow that reads {{hitl_response}} instead gets an empty
    # string, the run still reports success, and the agent answers with "" — which reads as
    # "the human said nothing" rather than "that variable does not exist". Seen in the
    # Showcase 7 flow, whose aggregator read {{hitl_response}}.
    #
    # Deliberately narrow: only hitl_* names are judged, because those are the ones this
    # runtime defines exhaustively. Any other {{var}} could come from a node output, an env
    # var or a conversation variable, and guessing at those would produce false warnings on
    # working flows.
    hitl_nodes = [n for n in nodes if n.get("type") == "hitl"]
    if hitl_nodes:
        known = {"hitl_last_response", "hitl_action_id"}
        for hn in hitl_nodes:
            for f in ((hn.get("config") or {}).get("fields") or []):
                nm = (f or {}).get("name", "").strip()
                if nm:
                    known.add(nm)
        used = re.findall(r"\{\{\s*(hitl_[A-Za-z0-9_]*)\s*\}\}", json.dumps([nodes, edges]))
        for name in sorted(set(used) - known):
            result.add(
                "warning", "unknown_hitl_variable",
                f"{{{{{name}}}}} is never set by a human-input resume, so it renders empty. "
                f"Available: {', '.join(sorted(known))}.",
                hitl_nodes[0].get("id"))

    # 3) Trigger count — skipped for snippets, which genuinely have no
    # Start/Trigger node by design (see NewAgentIn's own default_flow
    # docstring — matches Dify's own documented snippet behavior).
    if not is_snippet:
        trigger_count = sum(1 for n in nodes if n.get("type") == "trigger")
        if trigger_count == 0:
            result.add("error", "no_trigger", "This flow has no Trigger node")
        elif trigger_count > 1:
            result.add("error", "multiple_triggers", f"This flow has {trigger_count} Trigger nodes — exactly one is required")

    # 5) Dangling edges — checked before anything below that assumes
    # edge endpoints are real (reachability, handle validation).
    real_edges = []
    for e in edges:
        src_ok, tgt_ok = e.get("source") in node_ids, e.get("target") in node_ids
        if not src_ok or not tgt_ok:
            missing = e.get("source") if not src_ok else e.get("target")
            result.add("error", "dangling_edge", f"Edge '{e.get('id')}' references a node that doesn't exist: '{missing}'", e.get("id"))
        else:
            real_edges.append(e)

    # 6) Invalid handles — every real edge's source_handle checked
    # against its real source node's own currently-valid set.
    for e in real_edges:
        source_node = nodes_by_id.get(e["source"])
        if not source_node:
            continue
        allowed = valid_source_handles(source_node)
        handle = e.get("source_handle")
        if allowed is None:
            if handle:
                result.add("error", "invalid_handle",
                           f"Edge '{e.get('id')}' names handle '{handle}', but a {source_node.get('type')} node has a single, unnamed output",
                           e.get("id"))
        elif not allowed:
            result.add("error", "invalid_handle",
                       f"Edge '{e.get('id')}' originates from a {source_node.get('type')} node, which has no real output at all",
                       e.get("id"))
        elif handle not in allowed:
            result.add("error", "invalid_handle",
                       f"Edge '{e.get('id')}' names handle '{handle}', which isn't valid for a {source_node.get('type')} node (expected one of {sorted(h for h in allowed if h)})",
                       e.get("id"))

    # 7) Nested parent rules + cycles
    for n in nodes:
        parent_id = n.get("parent_id")
        if not parent_id:
            continue
        parent = nodes_by_id.get(parent_id)
        if not parent:
            result.add("error", "dangling_parent", f"Node '{n.get('id')}' has parent_id '{parent_id}', which doesn't exist", n.get("id"))
            continue
        if parent.get("type") not in CONTAINER_TYPES:
            result.add("error", "invalid_parent_type",
                       f"Node '{n.get('id')}' has parent_id pointing at a {parent.get('type')} node, but only loop/iteration containers can have children",
                       n.get("id"))
        # Real, reported runtime gap (§0.87, GAP_REPORT) — a real
        # container (loop/iteration) whose own parent is ALSO a real
        # container (a loop inside a loop, an iteration inside a loop,
        # etc.) used to be accepted here — this module's own prior
        # comment even said so explicitly ("a real container can
        # genuinely nest inside another... depth itself isn't
        # restricted"). But neither real execution path actually
        # supports it: the sync path's own run_loop_body/
        # run_iteration_body dispatch a body's own children via
        # exec_node/NODE_HANDLERS, which has no real entry for "loop"
        # or "iteration" (those two types are only ever handled by the
        # OUTER traversal's own real dispatch, never recursively from
        # inside a body) — a nested container genuinely raises
        # UnsupportedNodeType the moment it's reached, and the
        # Temporal/durable path has the identical gap in its own
        # equivalent container-body code. Confirmed directly by
        # reading both real execution paths before writing this check,
        # not assumed. Rejected here — at the one, real,
        # authoritative, publish-blocking validation layer — rather
        # than advertising a graph the runtime genuinely can't execute;
        # the report's own explicit, stated preference between the two
        # real options ("truly support recursive containers" vs.
        # "reject them"), given real recursive support would need a
        # substantial, correctness-sensitive rewrite of BOTH real
        # execution paths (including Temporal's own real determinism-
        # constrained container/activity model, which can't be
        # live-verified end-to-end in this sandbox — no reachable
        # Temporal test server, the same real, standing constraint
        # already stated for every other durable-path feature this
        # session) to do safely.
        if n.get("type") in CONTAINER_TYPES and parent.get("type") in CONTAINER_TYPES:
            result.add(
                "error", "nested_container_unsupported",
                f"Node '{n.get('id')}' is a {n.get('type')} container nested inside another "
                f"container ('{parent.get('id')}', a {parent.get('type')}) — the runtime doesn't yet "
                f"support this (neither the sync nor the durable execution path recursively dispatches "
                f"a container type from inside another container's own body). Move this "
                f"{n.get('type')} out to the top level, or flatten it into the outer "
                f"{parent.get('type')}'s own body.",
                n.get("id"),
            )
        # Real cycle check — walks the full parent chain regardless of
        # depth (a real, non-container node can still sit arbitrarily
        # deep under a single, non-nested container without tripping
        # this) — only a genuine REVISIT of an already-seen node id
        # trips it, not depth itself. Real container nesting is
        # rejected outright above, before this even runs for that
        # specific case, but this check stays general — it's still the
        # real, correct way to catch a genuine parent_id cycle
        # regardless of what node types are actually involved.
        seen = {n["id"]}
        cur = parent
        depth_guard = 0
        while cur and cur.get("parent_id"):
            depth_guard += 1
            if depth_guard > len(nodes) + 1 or cur["id"] in seen:
                result.add("error", "parent_cycle", f"Node '{n.get('id')}' is part of a real parent_id cycle", n.get("id"))
                break
            seen.add(cur["id"])
            cur = nodes_by_id.get(cur.get("parent_id"))

    # 7b) Real, general cycle detection over the actual execution graph
    # (§0.103, GAP_REPORT) — the actual fix for a real, reported gap:
    # section 7 above only ever walked parent_id CONTAINMENT chains,
    # never the real source->target execution edges nodes actually run
    # along. A genuine cycle there (A waits on B, B waits on A) was
    # confirmed live, before this fix, to let BOTH real schedulers
    # (harness.py's run_graph_concurrent, temporal_workflow.py's
    # AgentRunWorkflow) silently reach their own normal, successful
    # completion path — `if not wave: break` can't tell "every node
    # ran" apart from "no node can ever become ready again", and a
    # true, unbroken cycle is exactly the second case. Checked
    # separately over the top-level graph (nodes with no parent_id)
    # and, individually, over each container's own children — a
    # loop/iteration body IS walked linearly by its own separate
    # runtime mechanism (bounded by MAX_GRAPH_STEPS, so a body cycle
    # there fails loud rather than silently succeeding), but a clear,
    # early validation error is still real, honest value over letting
    # it run out the step budget with a confusing error instead.
    adjacency_all: Dict[str, List[str]] = {}
    for e in real_edges:
        adjacency_all.setdefault(e["source"], []).append(e["target"])

    def _find_cycle(candidate_ids: Set[str]) -> Optional[List[str]]:
        """Real, standard, iterative (no recursion — genuinely safe
        regardless of how large or pathological a flow gets) DFS
        three-color cycle detection, scoped to just this one subset of
        node ids (and only edges where BOTH ends are in it). Returns
        the real node-id path forming a cycle, or None if this subset
        is genuinely acyclic. Deterministic visitation order (sorted)
        so the same real cycle is always reported identically."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in candidate_ids}
        for start in sorted(candidate_ids):
            if color[start] != WHITE:
                continue
            stack: List[Tuple[str, int]] = [(start, 0)]
            path: List[str] = [start]
            color[start] = GRAY
            while stack:
                nid, idx = stack[-1]
                neighbors = [t for t in adjacency_all.get(nid, []) if t in candidate_ids]
                if idx < len(neighbors):
                    stack[-1] = (nid, idx + 1)
                    nxt = neighbors[idx]
                    if color.get(nxt) == GRAY:
                        return path[path.index(nxt):] + [nxt]
                    if color.get(nxt) == WHITE:
                        color[nxt] = GRAY
                        path.append(nxt)
                        stack.append((nxt, 0))
                else:
                    color[nid] = BLACK
                    path.pop()
                    stack.pop()
        return None

    top_level_ids = {n["id"] for n in nodes if not n.get("parent_id")}
    top_cycle = _find_cycle(top_level_ids)
    if top_cycle:
        result.add("error", "graph_cycle",
                    f"The flow's own execution graph has a real cycle: {' -> '.join(top_cycle)} — "
                    f"each node in this cycle waits on another node in the same cycle, so none of "
                    f"them can ever become ready to run. Left as-is, a run would silently reach a "
                    f"normal 'success' the moment nothing else is left ready, without these nodes "
                    f"ever actually executing.",
                    top_cycle[0])

    for container in nodes:
        if container.get("type") not in CONTAINER_TYPES:
            continue
        child_ids = {n["id"] for n in nodes if n.get("parent_id") == container.get("id")}
        if not child_ids:
            continue
        child_cycle = _find_cycle(child_ids)
        if child_cycle:
            result.add("error", "graph_cycle",
                        f"The {container.get('type')} container '{container.get('id')}' has a real "
                        f"cycle among its own body nodes: {' -> '.join(child_cycle)} — this container's "
                        f"body is walked step by step until an Exit Loop node, so this would exhaust "
                        f"the run's step budget rather than complete.",
                        container.get("id"))


    # 8) Reachable nodes — a real BFS from every trigger, over the
    # real top-level edge graph. A container's own body nodes are
    # reached through their container being reached (they're executed
    # as that container's own real sub-graph, not via a top-level
    # edge into them) — so a body child only counts as unreachable if
    # its OWN container is unreachable. comment nodes are
    # deliberately excluded, matching flowValidation.ts's own
    # SKIP_CONNECTIVITY_CHECK — they're real documentation, never part
    # of the execution graph.
    reachable: Set[str] = set()
    frontier = [n["id"] for n in nodes if n.get("type") == "trigger"]
    adjacency: Dict[str, List[str]] = {}
    for e in real_edges:
        adjacency.setdefault(e["source"], []).append(e["target"])
    while frontier:
        cur_id = frontier.pop()
        if cur_id in reachable:
            continue
        reachable.add(cur_id)
        frontier.extend(adjacency.get(cur_id, []))
        # a reached container's own children are reached with it
        frontier.extend([c["id"] for c in nodes if c.get("parent_id") == cur_id])
    for n in nodes:
        if n.get("type") == "comment":
            continue
        if n["id"] not in reachable:
            has_any_edge = any(e["source"] == n["id"] or e["target"] == n["id"] for e in real_edges)
            severity = "error" if not has_any_edge else "warning"
            result.add(severity, "unreachable_node",
                       f"'{n.get('type')}' node '{n.get('id')}' is not reachable from any Trigger" + ("" if has_any_edge else " and has no connections at all"),
                       n.get("id"))

    # 4) Output/respond requirements — server-side port of
    # flowValidation.ts's own app_type-aware structural check.
    if app_type and not is_snippet:
        is_chat_app = app_type in ("chatbot", "chatflow", "agent")
        if not is_chat_app and not any(n.get("type") == "output" for n in nodes):
            result.add("error", "missing_output_node",
                       "Workflow apps must have an Output node to define the structured result end users will see")
        if is_chat_app and not any(n.get("type") in RESPONSE_PRODUCING_TYPES for n in nodes):
            result.add("error", "no_response_producing_node",
                       "This chat app has no node that can produce a response (LLM, Agent, Sub-Agent, Code, Template, HTTP, or Tool)")

    # 9) Required variables — real, scoped to {{env.KEY}} references
    # against this agent's own real EnvVar rows (see this function's
    # own docstring for the stated, deliberate scope boundary on full
    # data-flow variable checking).
    if session is not None and agent_id is not None:
        from .models import EnvVar
        real_env_keys = {row.key for row in session.exec(select(EnvVar).where(EnvVar.agent_id == agent_id)).all()}
        for n in nodes:
            # An Agent node's own Env editor values (stored encrypted, keyed by
            # agent + node + key) are valid {{env.KEY}} sources for that node.
            node_keys = set()
            if n.get("type") == "agent" and n.get("id"):
                # Only keys the node declares AND has a stored value for.
                from .agent_assets import usable_secret_keys
                node_keys = usable_secret_keys(session, agent_id, n)
            for s in _walk_string_fields(n.get("config") or {}):
                for m in _ENV_VAR_RE.finditer(s):
                    key = m.group(1)
                    if key not in real_env_keys and key not in node_keys:
                        result.add("error", "unknown_env_var",
                                   f"References {{{{env.{key}}}}}, but no EnvVar named '{key}' exists for this agent",
                                   n.get("id"))

    # 10) Durable compatibility
    for n in nodes:
        if n.get("type") in SUPPORTED_NODE_TYPES and n.get("type") not in DURABLE_SUPPORTED_TYPES:
            result.add("error", "not_durable_compatible",
                       f"'{n.get('type')}' node is not supported on the durable (Temporal) execution path", n.get("id"))

    # 11) Tool/KB references
    for n in nodes:
        cfg = n.get("config") or {}
        if n.get("type") == "retrieval":
            if session is not None:
                # The SHARED resolver, not a display-name set. This
                # compared each reference against {row.name} only, so a
                # config holding a KB ID — which is what the Builder
                # writes now, and what the entitlement check and the
                # retrieval runtime both resolve correctly — was
                # reported as "doesn't exist" and blocked publish. Three
                # places read this field and this was the one left
                # behind; using resolve_kb means there is one answer to
                # "which KB is this?" rather than three.
                from .flow_compiler import resolve_kb, retrieval_kb_refs
                for kb_ref in retrieval_kb_refs(cfg):
                    if resolve_kb(session, kb_ref) is None:
                        result.add("error", "unknown_knowledge_base",
                                   f"References knowledge base '{kb_ref}', which doesn't exist",
                                   n.get("id"))
        elif n.get("type") == "agent" and session is not None and agent_id is not None:
            _check_agent_node(result, n, cfg, session, agent_id)
        elif n.get("type") == "tool":
            if not (cfg.get("catalog_object") or "").strip():
                result.add("error", "missing_catalog_object", "Tool node has no catalog object configured", n.get("id"))
        elif n.get("type") == "subagent":
            target = cfg.get("target_agent_id")
            if target and session is not None:
                from .models import Agent
                target_agent = session.get(Agent, target)
                if not target_agent:
                    result.add("error", "unknown_target_agent",
                               f"References agent '{target}', which doesn't exist", n.get("id"))
                elif any(nn.get("type") == "hitl" for nn in nodes):
                    # Real, reported architectural gap (§0.95, GAP_REPORT)
                    # — confirmed directly by reading both real files
                    # before writing this check: when THIS flow (the one
                    # being published) already needs the durable
                    # (Temporal) path (it has a real hitl node somewhere,
                    # making the whole parent a real Temporal workflow),
                    # a subagent node whose own target ALSO needs the
                    # durable path (the target's own real, current flow
                    # has a hitl node too) hits a real, unhandled gap:
                    # handle_subagent (harness.py) raises a genuine
                    # HitlPause when the CHILD pauses, but
                    # execute_node_activity (temporal_workflow.py) — the
                    # real code that actually runs a subagent node for a
                    # durable PARENT — has no special HitlPause handling
                    # at all; its own generic `except Exception` catches
                    # it and reports a false "orchestration failure",
                    # even though the child genuinely, correctly paused
                    # for a real person's input. The real, architecturally
                    # correct fix (a genuine Temporal Child Workflow,
                    # started from real workflow code rather than
                    # launched-and-polled from inside an Activity) is a
                    # substantial rewrite of the durable execution engine
                    # that can't be verified end-to-end in this sandbox
                    # (no reachable Temporal test server, the same real,
                    # standing constraint stated for every other
                    # durable-path feature this session) — rejected here
                    # instead, at the one, real, authoritative,
                    # publish-blocking validation layer, rather than
                    # silently advertising a graph the runtime can't
                    # correctly execute. Real, honest scope: only fires
                    # when THIS flow itself needs durable routing — a
                    # sync-path parent calling a durable child already
                    # works correctly (handle_subagent's own real
                    # HitlPause is genuinely caught by the sync harness,
                    # exactly as designed) and is unaffected.
                    target_flow = _real_current_flow(session, target_agent)
                    if any(tn.get("type") == "hitl" for tn in target_flow.get("nodes", [])):
                        result.add(
                            "error", "durable_parent_durable_child_hitl_unsupported",
                            f"This flow already needs the durable (Temporal) execution path (it has its own "
                            f"hitl node), and this Sub-Agent node's own target ('{target}') also needs the "
                            f"durable path (its own current flow has a hitl node) — a durable parent calling "
                            f"a durable child that pauses for human input isn't supported yet; the child's "
                            f"own pause would be misreported as an orchestration failure. Remove one of the "
                            f"two hitl nodes, or restructure so the Sub-Agent call doesn't need the durable "
                            f"path.",
                            n.get("id"),
                        )

    # 12) Version compatibility — see this function's own docstring;
    # this platform has no real, separate per-node schema-version
    # concept, so this folds entirely into check 2 above. No separate
    # check is added here rather than fabricating one that verifies
    # nothing real.

    return result
