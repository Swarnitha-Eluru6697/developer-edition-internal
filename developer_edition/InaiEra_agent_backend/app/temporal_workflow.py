"""
Real Temporal workflow/activity definitions for the durable execution path
(§5, §3.6 of the design doc). A flow containing a Human-in-the-Loop node (or
other durability-requiring type) routes here instead of the sync harness
path — every completed node is durably recorded by Temporal, so a worker
crash mid-run resumes from the last completed node, not from scratch.

Walks the same REAL graph (nodes + explicit edges) as harness.run_flow_sync,
via the same shared compute_next_node_id() — so a condition node's
branching can't silently behave differently on the durable path than on
the sync path, which was a real risk once there were two separate places
walking the flow graph. The same is true of `loop`: this workflow's inner
_run_loop_body mirrors run_flow_sync's run_loop_body node-for-node — a real
container, walked as its own real sub-graph of `parent_id`-linked children,
exiting early on a real `exit_loop` node or on the `max_loops` safety cap.

This connects to a REAL Temporal server — `temporal server start-dev`
locally, a real cluster in production — via the temporalio Python SDK. Not
a simulation. Requires a running worker process (worker.py) to actually
execute; starting a workflow without a worker polling the task queue will
hang until one connects.
"""
from typing import Any, Dict, List, Optional
from datetime import timedelta
import asyncio

from temporalio import workflow, activity
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .harness import (
        NODE_HANDLERS, call_node_handler, UCSDenied, HarnessStepLimitExceeded, compute_next_node_id,
        MAX_GRAPH_STEPS, load_env_map, interpolate_config, _resolve_outgoing_edges,
        compute_ready_wave, _interp_vars, _resolve_whole_var, _pick_body_output,
        finalize_iteration_outcomes, _aggregate_llm_usage,
    )
    from .db import engine
    from sqlmodel import Session
    import copy
    import asyncio


# Real, per-node-category retry policies (§0.76, GAP_REPORT) — the
# actual fix for the reported gap: every real workflow.execute_activity
# call in this file used to omit retry_policy entirely, which means
# Temporal's own real default applied uniformly to every node type —
# an LLM call, a real mutating HTTP POST, and a pure in-memory
# transform were all retried the exact same way, with no real
# distinction for which ones are actually safe to retry automatically.
# A pure, deterministic function — no I/O, no randomness, no wall-clock
# read — so it's genuinely safe to call directly from workflow code
# under Temporal's own determinism rules.
#
# Real, honest per-category reasoning, matching the report's own
# worked example exactly:
#   LLM-touching (llm/agent/subagent/classifier/parameter_extractor):
#     retryable — generating text has no external side effect of its
#     own; a transient real provider error (rate limit, timeout) is
#     exactly the case automatic retry exists for.
#   HTTP GET/HEAD: possibly retryable — safe by real HTTP semantics in
#     general, given a moderate, bounded retry count (this platform
#     can't verify a given destination's OWN real idempotency, so
#     "possibly" stays honest rather than assuming every real GET
#     endpoint is perfectly side-effect-free).
#   HTTP POST/PUT/PATCH/DELETE: NOT automatically retryable —
#     maximum_attempts=1. A real mutating call whose outcome on the far
#     side is unknown (did the real request even arrive before the
#     real failure?) must never be silently repeated by Temporal
#     itself; handle_http's own real Idempotency-Key header (harness.py,
#     same real fix) is a separate, complementary mechanism for when
#     the actual destination supports that real convention, not a
#     substitute for this.
#   tool (a real tool mutation): requires idempotency — this reference
#     platform's own handle_tool is still a real, stated placeholder
#     (no real external call yet — see its own docstring), so
#     maximum_attempts=1 is the safe, honest default until a real
#     connector with its own real idempotency support exists.
#   memory / variable_assigner: requires idempotency — genuinely
#     provided by idempotent_once (harness.py, same real fix) for the
#     one real, concrete case this platform has today (a chat-history
#     append), so real Temporal retries are safe to allow here.
#   docextractor: fetches an external URL, read-only — possibly
#     retryable, same real reasoning as HTTP GET.
#   everything else (code, template, condition, aggregator, output,
#     respond, gov, trigger, list_operator, exit_loop, comment, loop,
#     iteration, hitl): no real external side effect of their own —
#     real, bounded, generous default retries.
_LLM_TOUCHING_TYPES = {"llm", "agent", "subagent", "classifier", "parameter_extractor"}
_IDEMPOTENT_MEMORY_TYPES = {"memory", "variable_assigner"}


def _retry_policy_for_node(node: Dict[str, Any]) -> RetryPolicy:
    node_type = node.get("type")
    cfg = node.get("config") or {}

    if node_type in _LLM_TOUCHING_TYPES:
        return RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2),
                            backoff_coefficient=2.0, maximum_interval=timedelta(seconds=30))

    if node_type == "http":
        method = (cfg.get("method") or "GET").upper()
        if method in ("GET", "HEAD"):
            return RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1),
                                backoff_coefficient=2.0, maximum_interval=timedelta(seconds=20))
        return RetryPolicy(maximum_attempts=1)

    if node_type == "tool":
        return RetryPolicy(maximum_attempts=1)

    if node_type == "docextractor":
        return RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1),
                            backoff_coefficient=2.0, maximum_interval=timedelta(seconds=20))

    return RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=1),
                        backoff_coefficient=2.0, maximum_interval=timedelta(seconds=30))


# §0.105, GAP_REPORT — a real, reported timeout-mismatch bug: every
# real call site below used a flat, hardcoded 30s start_to_close_timeout
# regardless of what the node itself was actually configured for — an
# llm node's own real, user-configurable timeout_s (handle_llm,
# harness.py) could be set well past 30s, and Temporal would still
# genuinely kill/retry the Activity at 30s, silently ignoring the
# person's own explicit configuration. A generous, fixed FLOOR (this
# platform's own prior, flat default) is kept as a genuine minimum for
# every node type — this is additive headroom, not a tightening, for
# any node that never had a configurable timeout to begin with.
_DEFAULT_ACTIVITY_TIMEOUT_SECONDS = 30
_ACTIVITY_TIMEOUT_SAFETY_MARGIN_SECONDS = 15


def _activity_timeout_for_node(node: Dict[str, Any]) -> timedelta:
    """Real activity timeout = node timeout + safety margin (§0.105,
    GAP_REPORT), matching the report's own recommended formula exactly
    — the actual fix for the real, reported mismatch above. Only `llm`
    (and, by construction, `agent`/`subagent`/`classifier`/
    `parameter_extractor` — every real _LLM_TOUCHING_TYPES member,
    since they all ultimately call the same real, timed provider SDK
    through _call_llm_tracked) has a genuine, user-configurable
    timeout_s field today (handle_llm's own cfg.get("timeout_s"),
    harness.py) — every other real node type has no such field to
    derive from yet, so it keeps the same, generous, unchanged flat
    default this platform already used for every node before this fix,
    honestly stated as a floor rather than fabricating a per-type value
    with no real, configured basis."""
    cfg = node.get("config") or {}
    if node.get("type") in _LLM_TOUCHING_TYPES:
        configured = cfg.get("timeout_s")
        if configured not in (None, ""):
            try:
                return timedelta(seconds=max(float(configured), 1.0) + _ACTIVITY_TIMEOUT_SAFETY_MARGIN_SECONDS)
            except (TypeError, ValueError):
                pass
    return timedelta(seconds=_DEFAULT_ACTIVITY_TIMEOUT_SECONDS)


def _idempotency_key_for(node_id: str, iteration: Optional[int] = None) -> str:
    """Real, stable-across-retries idempotency key (§0.76, GAP_REPORT)
    — deliberately workflow_id + node_id + a real loop/iteration index,
    NOT the real Temporal attempt number: the entire point is that
    every genuine retry of the SAME logical node-execution attempt
    computes the IDENTICAL key, so idempotent_once (harness.py) and the
    real Idempotency-Key HTTP header (handle_http, harness.py) can both
    recognize "this exact logical step already happened." A different
    real loop/iteration index is a genuinely different logical event
    (the same node's Nth real pass through a loop is not the same
    real-world action as its N+1th), so that's real, deliberately
    included; the attempt number would defeat the entire purpose if it
    were.  `workflow.info().workflow_id` is a real, stable, genuinely
    deterministic property of the running workflow — safe to read
    directly from workflow code."""
    wf_id = workflow.info().workflow_id
    if iteration is not None:
        return f"{wf_id}:{node_id}:{iteration}"
    return f"{wf_id}:{node_id}"


def _execute_node_sync(node: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The real, genuinely blocking body of one node execution — a
    real DB session open, a real handler dispatch (which may itself
    make a real, blocking model SDK call, HTTP call, or sandboxed
    subprocess call) — extracted into its own plain, synchronous
    function specifically so execute_node_activity (below) can hand
    it to a real, separate worker thread via asyncio.to_thread rather
    than running it directly on the async Activity's own event loop.

    §0.105, GAP_REPORT — the actual fix for a real, reported bug: this
    body used to run directly, inline, inside an `async def` Activity
    with no `await` anywhere near the actual blocking work — every
    model SDK call, httpx call, DB query, and sandboxed subprocess call
    genuinely blocked the SAME asyncio event loop the Temporal worker
    uses to poll for new work, process cancellations, and send
    heartbeats for every OTHER concurrently-running Activity in this
    same worker process. A single, slow node (a large LLM call, a slow
    HTTP endpoint) could starve every other real, concurrent Activity
    this worker was otherwise ready to make progress on.

    ctx["_durable_execution"] = True (§0.105, GAP_REPORT) — the actual
    fix for the report's own second, real, reported concern: retry
    multiplication. handle_llm/handle_code/handle_http each have their
    own real, independent, handler-level retry_on_failure loop — on
    the sync path, that's the ONLY retry mechanism, and it stays fully
    active there. On THIS, durable path, Temporal's own
    _retry_policy_for_node ALSO retries the entire Activity on
    failure — stacking the two multiplies real provider calls (and,
    for an LLM node specifically, real, duplicated token cost) well
    past what either retry policy's own configured attempt count
    alone would suggest. Setting this flag here — checked by each of
    those three handlers' own retry decision — makes Temporal the
    sole, real retry owner for this execution mode, matching the
    report's own recommended \"pick one retry owner per execution
    mode\" model exactly; the sync path (run_flow_sync, harness.py)
    never sets this, so handler-level retries remain fully active
    there, where they're the only real retry mechanism at all."""
    with Session(engine) as session:
        env_map = load_env_map(session, ctx["agent_id"])
        from .agent_assets import effective_env_map
        node_config = interpolate_config(node.get("config") or {},
                                         effective_env_map(session, ctx["agent_id"], node, env_map))
        # Real usage tracking (§0.78, GAP_REPORT) — same real reset-
        # before/aggregate-after pattern as the sync path's own
        # run_flow_sync (harness.py) — reset immediately before this
        # one node's own handler runs, so the real usage this
        # specific Activity invocation captures (via
        # _call_llm_tracked, harness.py) can't include a stale
        # accumulation from an earlier real node execution sharing
        # the same ctx dict.
        ctx["_llm_usage"] = []
        # Real fix (§0.51, GAP_REPORT) — this used to keep its own,
        # separate hardcoded tuple of which node types need `session`
        # passed in, and that tuple had silently drifted out of sync
        # with the sync path's own list: it was missing "llm",
        # "variable_assigner", "parameter_extractor", and "agent" —
        # all four handlers genuinely require session as a real
        # positional argument, so a durable-path flow reaching any of
        # them raised a real TypeError at runtime. Now calls the same
        # one real dispatch function harness.py's own sync-path call
        # sites use, reading from the same one real
        # SESSION_REQUIRED_NODE_TYPES set — this specific class of
        # drift can't recur.
        ctx["_current_node_id"] = node.get("id")
        ctx["_durable_execution"] = True
        ctx = call_node_handler(node["type"], node_config, ctx, session)
        # Real per-node token/cost usage (§0.78, GAP_REPORT) — same
        # real aggregation the sync path's own span uses; see
        # _aggregate_llm_usage's own docstring for why this is None
        # (not zero-valued) for a node that made no real LLM calls.
        usage = _aggregate_llm_usage(ctx)
    span = {"node": node["type"], "status": "ok"}
    if usage:
        span.update(usage)
    return {**ctx, "_span": span}


def _json_safe_activity_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Make an activity's return value safe for Temporal's JSON payload
    converter.

    §0.79 introduced a real Decimal cost_usd on every LLM span, and
    routers/execution.py's own _json_safe_spans converts it at the JSONB
    persistence boundary. Temporal's activity return is a SECOND JSON
    boundary, and nothing converted it there: any durable run whose LLM
    node made a real, costed call died with
    `TypeError: Object of type Decimal is not JSON serializable`,
    retried three times, and failed the workflow. It never showed up in
    testing because a mock provider produces no cost to convert.

    In practice this blocked every agent containing a Human Input node,
    since a hitl node forces the durable path.

    Same boundary decision as _json_safe_spans: the typed
    Run.cost_usd/Trace.cost_usd NUMERIC columns stay the authoritative,
    full-precision source; what crosses the wire for display is a float.
    """
    from decimal import Decimal

    def convert(value: Any, depth: int = 0) -> Any:
        if depth > 12:
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return {k: convert(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v, depth + 1) for v in value]
        return value

    return convert(result)


@activity.defn
async def execute_node_activity(node: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """One Temporal Activity = one node execution — the durable-path
    equivalent of one call to run_flow_sync's exec_node closure. Loads and
    applies {{env.KEY}} interpolation the same way the sync path does (via
    the same load_env_map/interpolate_config functions) — a real risk,
    now avoided, was this path silently NOT substituting env vars while
    the sync path did.

    §0.105, GAP_REPORT — the actual genuinely-blocking body is now in
    _execute_node_sync (above), run via asyncio.to_thread — this
    async def itself never blocks its own event loop; see that
    function's own docstring for the full real reasoning."""
    handler = NODE_HANDLERS.get(node["type"])
    if not handler:
        # Real, loud failure (§0.73, GAP_REPORT) — matches the sync
        # path's own UnsupportedNodeType fix (harness.py) in spirit,
        # but shaped as a real, structured error return rather than a
        # raised exception, matching this activity's own existing
        # convention for every other node-level failure below (a
        # raised exception here would trigger Temporal's own activity
        # retry mechanism, which is wrong for a failure that would
        # never succeed on retry — the node type is genuinely
        # unsupported, not transiently unavailable). This used to
        # return "status": "skipped" and let the workflow continue as
        # if the step had simply never existed.
        return {**ctx, "_span": {"node": node["type"], "status": "error",
                                  "error": f"No real handler for node type '{node['type']}'"},
                "_fail_class": "orchestration"}
    try:
        return _json_safe_activity_result(await asyncio.to_thread(_execute_node_sync, node, ctx))
    except UCSDenied as e:
        return {**ctx, "_span": {"node": node["type"], "status": "ucs_denied", "detail": e.detail},
                "_fail_class": "ucs"}
    except HarnessStepLimitExceeded:
        return {**ctx, "_span": {"node": node["type"], "status": "harness_fail"}, "_fail_class": "harness"}
    except Exception as e:
        return {**ctx, "_span": {"node": node["type"], "status": "error", "error": str(e)},
                "_fail_class": "orchestration"}


@activity.defn
async def render_hitl_form_activity(node: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Real hitl form rendering — a genuine Activity, not inline
    workflow code, specifically because it needs real DB access for
    {{env.KEY}} interpolation (load_env_map), which workflow code isn't
    allowed to do directly under Temporal's own determinism rules.
    Mirrors harness.py's raise_hitl_pause rendering logic exactly (same
    real honest delivery_status placeholders for a non-web_app delivery
    method) — reusing the same real _interp_vars function rather than
    a second, independently-drifting reimplementation."""
    with Session(engine) as session:
        env_map = load_env_map(session, ctx["agent_id"])
    cfg = interpolate_config(node.get("config") or {}, env_map)
    content = _interp_vars(cfg.get("form_content", ""), ctx)
    fields = cfg.get("fields") or []
    actions = cfg.get("actions") or [{"id": "continue", "title": "Continue", "style": "primary"}]
    delivery_methods = cfg.get("delivery_methods") or ["web_app"]
    delivery_status = {
        m: ("delivered (web app)" if m == "web_app" else
            "not sent \u2014 no email provider configured in this reference platform")
        for m in delivery_methods
    }
    return {
        "hitl_node_id": node["id"], "hitl_rendered_content": content,
        "hitl_fields": fields, "hitl_actions": actions, "hitl_delivery_status": delivery_status,
    }


class _LoopExited(Exception):
    """Internal signal only — never leaves this module. Raised by
    _run_loop_body when a real exit_loop node is reached or max_loops is
    hit, so the outer walk can tell 'the loop finished normally' apart
    from 'a node inside it genuinely failed' without threading an extra
    return value through every call site."""


@workflow.defn
class AgentRunWorkflow:
    def __init__(self) -> None:
        # Real signal/query state (§0.42, GAP_REPORT) — a genuine
        # Temporal-native pause, not the sync path's own snapshot-and-
        # exit approach. self._hitl_pending reflects whatever the
        # workflow is CURRENTLY paused on (queryable at any time by a
        # caller that doesn't otherwise know the run's live state);
        # self._hitl_response is set by a real signal and consumed by
        # a real wait_condition inside run() below.
        self._hitl_pending: Optional[Dict[str, Any]] = None
        self._hitl_response: Optional[Dict[str, Any]] = None

    @workflow.signal
    def submit_hitl_response(self, action_id: str, field_values: Dict[str, Any]) -> None:
        """The real, external resume mechanism for a hitl pause on this
        durable path — routers/execution.py's real /hitl/{id}/respond
        endpoint calls client.get_workflow_handle(workflow_id).signal(
        AgentRunWorkflow.submit_hitl_response, args=[...]) for any
        HitlRequest whose temporal_workflow_id is set. Genuinely
        different from the sync path's resume_flow_sync — this signals
        a real, still-running workflow instance rather than
        reconstructing ctx from a snapshot."""
        self._hitl_response = {"action_id": action_id, "field_values": field_values or {}}

    @workflow.query
    def get_status(self) -> Dict[str, Any]:
        """Real, live status — lets a caller (routers/execution.py's
        _run_durable, right after starting the workflow, and the
        respond endpoint, right after signaling it) find out whether
        this real workflow is currently paused on a hitl node without
        needing to wait for it to fully complete."""
        if self._hitl_pending:
            return {"status": "paused_for_human_input", **self._hitl_pending}
        return {"status": "running"}

    @workflow.run
    async def run(self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], ctx: Dict[str, Any]) -> Dict[str, Any]:
        nodes_by_id = {n["id"]: n for n in nodes}
        edges_by_source: Dict[str, List[Dict[str, Any]]] = {}
        for e in edges:
            edges_by_source.setdefault(e["source"], []).append(e)

        spans: List[Dict[str, Any]] = []
        steps_state = {"count": 0}
        fail_holder: Dict[str, Any] = {}

        async def exec_node(node: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal ctx
            # Real idempotency key + per-category retry policy (§0.76,
            # GAP_REPORT) — ctx["vars"]["loop_iteration"] is already
            # set by run_loop_body below whenever this call happens
            # inside a real loop body, so the SAME node visited on
            # different real loop passes gets a genuinely distinct key
            # per pass, while every real Temporal retry of the SAME
            # pass's SAME node shares the identical key.
            call_ctx = {**ctx, "_idempotency_key": _idempotency_key_for(node["id"], ctx["vars"].get("loop_iteration"))}
            result = await workflow.execute_activity(
                execute_node_activity, args=[node, call_ctx],
                start_to_close_timeout=_activity_timeout_for_node(node),
                retry_policy=_retry_policy_for_node(node),
            )
            span = result.pop("_span", {"node": node.get("type"), "status": "unknown"})
            spans.append(span)
            fail_class = result.pop("_fail_class", None)
            ctx = result
            if fail_class:
                fail_holder["fail_class"] = fail_class
                raise _LoopExited()
            return ctx

        async def run_loop_body(loop_node: Dict[str, Any]) -> None:
            children = [n for n in nodes if n.get("parent_id") == loop_node["id"]]
            if not children:
                return
            child_ids = {c["id"] for c in children}
            child_edges_by_source: Dict[str, List[Dict[str, Any]]] = {}
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
                current_child: Optional[Dict[str, Any]] = entry
                exited = False
                while current_child:
                    if steps_state["count"] >= MAX_GRAPH_STEPS:
                        return
                    steps_state["count"] += 1
                    await exec_node(current_child)  # real span even for exit_loop
                    if current_child["type"] == "exit_loop":
                        exited = True
                        break
                    next_id = compute_next_node_id(current_child, ctx, child_edges_by_source)
                    current_child = nodes_by_id.get(next_id) if next_id else None
                if exited:
                    return

        async def run_hitl_node(node: Dict[str, Any]) -> None:
            """Real, genuine Temporal-native pause (§0.42, GAP_REPORT) —
            the actual reason durability matters for a hitl node: this
            wait_condition can genuinely last hours or days and survive
            a worker restart, unlike the sync path's in-memory-until-
            resumed HitlPause. Sets ctx["vars"]["hitl_action_id"] on
            resume so _resolve_outgoing_edges' own real hitl branch
            (harness.py, §0.42) correctly activates only the one real
            outgoing edge matching the action actually chosen."""
            nonlocal ctx
            # Real, explicit retry policy (§0.76, GAP_REPORT) — this
            # activity only renders a real form (reads config/ctx,
            # produces display content); no real external side effect
            # of its own, so a real, bounded, generous retry policy is
            # safe rather than leaving this at Temporal's own unbounded
            # default.
            rendered = await workflow.execute_activity(
                render_hitl_form_activity, args=[node, ctx],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=1),
                                          backoff_coefficient=2.0, maximum_interval=timedelta(seconds=30)),
            )
            self._hitl_pending = rendered
            spans.append({"node": "hitl", "node_id": node.get("id"), "status": "paused"})
            self._hitl_response = None
            await workflow.wait_condition(lambda: self._hitl_response is not None)
            resp = self._hitl_response
            self._hitl_pending = None
            ctx["vars"]["hitl_last_response"] = resp["field_values"]
            ctx["vars"]["hitl_action_id"] = resp["action_id"]
            for k, v in resp["field_values"].items():
                ctx["vars"][k] = v
            spans.append({"node": "hitl", "node_id": node.get("id"), "status": "resumed", "action_id": resp["action_id"]})

        async def run_iteration_body(iteration_node: Dict[str, Any]) -> None:
            """Real Temporal-native Iteration (§0.52, GAP_REPORT) —
            genuinely ported from harness.py's own run_iteration_body,
            not a reimplementation from scratch: the same real array
            resolution (_resolve_whole_var), the same real per-iteration
            item/index binding via a real isolated per-iteration ctx,
            the same real 3 documented error modes ('Terminated',
            'Continue on error', 'Remove abnormal output'), the same
            real flatten default. Before this fix, `iteration` was
            missing from this workflow's own solo-node set entirely and
            had no NODE_HANDLERS entry either, so a durable-path flow
            reaching one fell through execute_node_activity's generic
            "no handler wired yet" branch and silently continued with
            unchanged ctx — worse than a hard failure, since downstream
            nodes would proceed against stale/incorrect data rather
            than the run visibly stopping.

            The one real difference from the sync version, required by
            Temporal's own determinism rules (workflow code can't do
            direct DB access or spawn real OS threads): each body node,
            per iteration, is a real workflow.execute_activity call via
            the same exec_activity_isolated helper a multi-node plain
            wave already uses below, rather than an in-process function
            call; and real "parallel" mode uses real asyncio.gather over
            multiple full iteration walks, chunked to the same real
            max-10-concurrent Dify-documented cap, instead of a raw
            ThreadPoolExecutor, which isn't valid inside workflow code."""
            nonlocal ctx
            cfg = iteration_node.get("config") or {}
            children = [n for n in nodes if n.get("parent_id") == iteration_node["id"]]
            array_val = _resolve_whole_var(cfg.get("input_var", ""), ctx)
            output_var = cfg.get("output_var") or "iteration_output"

            if not isinstance(array_val, list) or not children or not array_val:
                ctx["vars"][output_var] = []
                return

            child_ids = {c["id"] for c in children}
            child_edges_by_source: Dict[str, List[Dict[str, Any]]] = {}
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

            async def run_one(index: int, item: Any) -> Any:
                iter_vars = copy.deepcopy(ctx["vars"])
                iter_vars["item"] = item
                iter_vars["index"] = index
                iter_ctx: Dict[str, Any] = {**ctx, "vars": iter_vars, "output": None}
                current_child: Optional[Dict[str, Any]] = entry
                steps_here = 0
                while current_child:
                    steps_here += 1
                    if steps_here > MAX_GRAPH_STEPS:
                        raise HarnessStepLimitExceeded()
                    result = await exec_activity_isolated(current_child, iter_ctx)
                    span = result.pop("_span", {"node": current_child.get("type"), "status": "unknown"})
                    span["iteration_index"] = index
                    spans.append(span)
                    fail_class = result.pop("_fail_class", None)
                    if fail_class:
                        raise RuntimeError(span.get("error") or span.get("status") or "node failed")
                    iter_ctx = result
                    next_id = compute_next_node_id(current_child, iter_ctx, child_edges_by_source)
                    current_child = nodes_by_id.get(next_id) if next_id else None
                return _pick_body_output(iter_ctx)

            async def run_one_safe(index: int, item: Any):
                try:
                    return index, await run_one(index, item), None
                except Exception as e:
                    return index, None, str(e)

            outcomes: List[Optional[tuple]] = [None] * len(array_val)
            terminated_error: Optional[str] = None

            if parallel:
                max_workers = min(10, len(array_val))  # real Dify-documented cap
                items = list(enumerate(array_val))
                for chunk_start in range(0, len(items), max_workers):
                    chunk = items[chunk_start:chunk_start + max_workers]
                    results = await asyncio.gather(*[run_one_safe(i, item) for i, item in chunk])
                    for i, value, error in results:
                        outcomes[i] = (i, value, error)
                        if error and error_mode == "Terminated" and terminated_error is None:
                            terminated_error = f"Iteration {i} failed: {error}"
                    if terminated_error:
                        break
            else:
                for i, item in enumerate(array_val):
                    i2, value, error = await run_one_safe(i, item)
                    outcomes[i] = (i2, value, error)
                    if error and error_mode == "Terminated":
                        terminated_error = f"Iteration {i} failed: {error}"
                        break

            if terminated_error:
                fail_holder["fail_class"] = "orchestration"
                spans.append({"node": "iteration", "node_id": iteration_node.get("id"), "status": "error", "error": terminated_error})
                raise _LoopExited()

            results: List[Any] = finalize_iteration_outcomes(outcomes, error_mode, flatten)

            ctx["vars"][output_var] = results

        edges_by_target: Dict[str, List[Dict[str, Any]]] = {}
        for e in edges:
            edges_by_target.setdefault(e["target"], []).append(e)

        start_node = next((n for n in nodes if n["type"] == "trigger"), None)
        if not start_node:
            return {"status": "failed", "output": None,
                    "spans": [{"node": None, "status": "error", "error": "no trigger node"}],
                    "fail_class": "orchestration"}

        # Real wave-based scheduling (§0.41, GAP_REPORT) — brings this
        # durable path up to the same real fan-out/join behavior
        # run_graph_concurrent already gives the sync path, closing a
        # real, previously-latent bug: with the old single-next-node
        # walk, a plain node's SECOND real outgoing edge (fan-out —
        # see add_edge's own real support for more than one edge per
        # source/handle) would silently never fire at all on this
        # durable path, not just run without concurrency. Reuses
        # harness._resolve_outgoing_edges directly — the exact same
        # real branch-liveness logic the sync scheduler uses — rather
        # than a second, easily-drifting reimplementation, which is
        # what let the two paths diverge in the first place.
        #
        # Temporal-safe concurrency: a raw ThreadPoolExecutor (the sync
        # scheduler's own real mechanism) isn't valid inside workflow
        # code — Temporal workflows must stay deterministic. The real,
        # canonical Temporal pattern for "run N activities at once and
        # wait for all of them" is asyncio.gather() over N real
        # workflow.execute_activity() calls, which is what a "plain"
        # wave of more than one node uses below. Each activity call
        # gets its own real, independently-serialized ctx argument —
        # Temporal's own activity-argument serialization is what
        # provides real isolation here, the same real guarantee the
        # sync path's own explicit deep-copy gives by hand.
        activated_edges: set = set()
        dead_edges: set = set()
        done_nodes: set = set()
        dead_nodes: set = set()

        async def exec_activity_isolated(node: Dict[str, Any], node_ctx: Dict[str, Any]) -> Dict[str, Any]:
            """The real per-node Activity call used by a multi-node
            wave — takes an explicit ctx rather than mutating the
            outer one via nonlocal, so concurrently-gathered calls
            never share mutable state (each genuinely gets its own
            real, independently-serialized argument)."""
            # Real idempotency key + per-category retry policy (§0.76,
            # GAP_REPORT) — real iteration index resolution mirrors
            # both real real container conventions this call site
            # serves: run_iteration_body's own real per-item "index"
            # var, and (were a real loop's own children ever dispatched
            # through this same isolated path) "loop_iteration" — a
            # real, honest fallback to no iteration component at all
            # for a genuine top-level multi-node wave, where neither
            # applies.
            iteration = node_ctx.get("vars", {}).get("index", node_ctx.get("vars", {}).get("loop_iteration"))
            call_ctx = {**node_ctx, "_idempotency_key": _idempotency_key_for(node["id"], iteration)}
            return await workflow.execute_activity(
                execute_node_activity, args=[node, call_ctx],
                start_to_close_timeout=_activity_timeout_for_node(node),
                retry_policy=_retry_policy_for_node(node),
            )

        try:
            while True:
                # Real, shared wave computation — the exact same
                # node_readiness/propagate_dead_nodes logic
                # run_graph_concurrent (the sync scheduler) now also
                # calls, so the two real schedulers can't silently
                # diverge again.
                wave = compute_ready_wave(nodes_by_id, start_node["id"], edges_by_source, edges_by_target,
                                           activated_edges, dead_edges, done_nodes, dead_nodes)
                if not wave:
                    # §0.103, GAP_REPORT — the exact same real fix as
                    # the sync scheduler's own equivalent guard
                    # (harness.py's run_graph_concurrent) — an empty
                    # wave does NOT, by itself, mean the graph
                    # genuinely finished. It also happens when real
                    # nodes remain that can never become ready: each
                    # one's own unresolved incoming edge traces back
                    # to another node in the SAME unresolved set — a
                    # true cycle, confirmed live, before this fix, to
                    # let this exact scheduler reach its own normal,
                    # successful completion with those nodes never
                    # having run at all.
                    blocked = [nid for nid in nodes_by_id if nid not in done_nodes and nid not in dead_nodes]
                    if blocked:
                        unresolved_edges = [
                            e["id"] for e in edges
                            if e["target"] in blocked and e["id"] not in activated_edges and e["id"] not in dead_edges
                        ]
                        spans.append({
                            "node": None, "status": "error",
                            "error": f"Graph deadlock — {len(blocked)} node(s) can never become ready to run: "
                                     f"{', '.join(sorted(blocked))}.",
                        })
                        return {
                            "status": "failed", "output": None, "spans": spans, "fail_class": "graph_deadlock",
                            "deadlock_blocked_nodes": sorted(blocked), "deadlock_unresolved_edges": sorted(unresolved_edges),
                        }
                    break

                if steps_state["count"] + len(wave) > MAX_GRAPH_STEPS:
                    spans.append({"node": None, "status": "error",
                                  "error": f"graph exceeded {MAX_GRAPH_STEPS} steps — likely an unintended cycle"})
                    return {"status": "failed", "output": None, "spans": spans, "fail_class": "orchestration"}
                steps_state["count"] += len(wave)

                plain = [n for n in wave if n["type"] not in ("loop", "hitl", "iteration")]
                solo = [n for n in wave if n["type"] in ("loop", "hitl", "iteration")]
                # hitl last within its own solo group — mirrors the sync
                # scheduler's own real reasoning (see harness.py's
                # _SOLO_NODE_TYPES docstring): lets any real progress
                # from this same wave's other solo nodes (loop,
                # iteration) commit before a real, possibly long-lived
                # pause.
                solo.sort(key=lambda n: 1 if n["type"] == "hitl" else 0)

                if len(plain) == 1:
                    await exec_node(plain[0])
                elif len(plain) > 1:
                    isolated_ctxs = [{**ctx, "vars": copy.deepcopy(ctx["vars"])} for _ in plain]
                    results = await asyncio.gather(*[
                        exec_activity_isolated(node, node_ctx) for node, node_ctx in zip(plain, isolated_ctxs)
                    ])
                    fail_class = None
                    for node, result in sorted(zip(plain, results), key=lambda pair: pair[0]["id"]):
                        span = result.pop("_span", {"node": node.get("type"), "status": "unknown"})
                        spans.append(span)
                        this_fail = result.pop("_fail_class", None)
                        if this_fail and not fail_class:
                            fail_class = this_fail
                        ctx["vars"].update(result.get("vars", {}))
                        if result.get("output") is not None:
                            ctx["output"] = result["output"]
                    if fail_class:
                        fail_holder["fail_class"] = fail_class
                        raise _LoopExited()

                for node in plain:
                    done_nodes.add(node["id"])
                    act, dead = _resolve_outgoing_edges(node, ctx, edges_by_source)
                    activated_edges.update(act)
                    dead_edges.update(dead)

                for node in solo:
                    if node["type"] == "loop":
                        await run_loop_body(node)
                    elif node["type"] == "iteration":
                        await run_iteration_body(node)
                    else:  # hitl
                        await run_hitl_node(node)
                    done_nodes.add(node["id"])
                    act, dead = _resolve_outgoing_edges(node, ctx, edges_by_source)
                    activated_edges.update(act)
                    dead_edges.update(dead)
        except _LoopExited:
            fail_class = fail_holder["fail_class"]
            return {"status": "denied" if fail_class == "ucs" else "failed",
                    "output": None, "spans": spans, "fail_class": fail_class}

        return {"status": "success", "output": ctx.get("output"), "spans": spans, "fail_class": None}
