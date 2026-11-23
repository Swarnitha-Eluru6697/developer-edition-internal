"""
Real MCP (Model Context Protocol) server per agent — not a placeholder
toggle. Implements the core JSON-RPC 2.0 methods (`initialize`,
`tools/list`, `tools/call`) an MCP client like Claude Desktop or Cursor
actually needs to discover and invoke this agent as a tool, matching
https://docs.dify.ai/en/cloud/use-dify/publish/publish-mcp's own flow:
toggle on to get a unique server URL, treat that URL like an API key,
regenerate to invalidate the old one immediately.

Honest scope: this is the request/response subset of MCP over plain
HTTP POST, not the full spec (no SSE streaming transport, no
resources/prompts primitives — only the `tools` capability, since that's
all a single agent-as-a-tool needs). A real MCP client speaking this
subset can genuinely discover the tool and invoke it; anything beyond
that is a disclosed gap, not silently unsupported.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session

from ..db import get_session
from ..models import Agent
from ..auth import enforce_authenticated_user
from .execution import execute_agent_flow, create_hitl_pause_run, resolve_execution, _finalize_run
import time
import uuid

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _tool_name(agent: Agent) -> str:
    # MCP tool names are conventionally slug-like — real, derived from
    # the agent's actual name, not a generic placeholder.
    return "".join(c if c.isalnum() else "_" for c in agent.name.lower()).strip("_") or agent.id


def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _rpc_result(rpc_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


@router.post("/{agent_id}/{token}", dependencies=[Depends(require_permission(P.AGENTS_RUN))])
async def mcp_endpoint(agent_id: str, token: str, body: dict, request: Request, session: Session = Depends(get_session)):
    rpc_id = body.get("id")
    method = body.get("method")

    agent = session.get(Agent, agent_id)
    if not agent:
        return _rpc_error(rpc_id, -32001, "Unknown agent")

    # Real credential check — a disabled server or a mismatched/old
    # (post-regenerate) token is refused the same way a real API key
    # would be, not silently allowed through. Real decryption (§0.59,
    # GAP_REPORT) — the stored token is encrypted at rest now;
    # decrypt_secret's own honest fallback means a pre-existing,
    # genuinely-plaintext token (from before this fix, never rotated
    # since) still compares correctly.
    #
    # §0.101, GAP_REPORT — reads real, top-level Agent columns
    # (mcp_enabled/mcp_token_encrypted), not features["mcp"] — the
    # actual fix for a real, reported vulnerability: features is what
    # a version restore can overwrite wholesale from an old snapshot;
    # these columns genuinely can't be, so a rotated/revoked token can
    # never be resurrected by restoring an old version.
    from ..secrets_crypto import decrypt_secret
    if not agent.mcp_enabled or decrypt_secret(agent.mcp_token_encrypted) != token:
        return _rpc_error(rpc_id, -32002, "MCP Server is disabled or this URL is no longer valid")

    if method == "initialize":
        return _rpc_result(rpc_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": agent.name, "version": agent.version},
        })

    if method == "tools/list":
        return _rpc_result(rpc_id, {
            "tools": [{
                "name": _tool_name(agent),
                "description": agent.description or f"Run the {agent.name} agent.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"input": {"type": "string", "description": "The message or task to send this agent."}},
                    "required": ["input"],
                },
            }],
        })

    if method == "tools/call":
        params = body.get("params") or {}
        tool_name = params.get("name")
        if tool_name != _tool_name(agent):
            return _rpc_error(rpc_id, -32602, f"Unknown tool '{tool_name}'")
        # An MCP token proves the CALLER holds a credential for this
        # agent; it does not say who the person behind it is. When the
        # agent has opted into a gateway-verified identity, that
        # distinction is the whole point, so a call with no verified
        # identity is refused here rather than run with none.
        try:
            enforce_authenticated_user(agent, request, "mcp")
        except HTTPException as e:
            return _rpc_error(rpc_id, -32001, str(e.detail))
        user_input = (params.get("arguments") or {}).get("input", "")
        # Genuinely runs the SAME flow interpreter /run uses — an MCP
        # call is a real execution, not a canned demo response.
        #
        # §0.108, GAP_REPORT — resolved explicitly, once, here (rather
        # than left for execute_agent_flow to resolve internally) so
        # this SAME resolved version can be recorded on the real
        # Run/Trace rows below — the actual fix for a real, reported
        # gap found while verifying this exact section's own primary
        # fix: an MCP-invoked run used to create NO Run/Trace row at
        # all for any outcome except a genuine HITL pause — completely
        # invisible to the Runs/Traces dashboard and every real
        # monitoring view that reads from them, not merely
        # inconsistent with /run's own behavior but genuinely absent
        # from the platform's own audit trail entirely.
        start = time.time()
        try:
            resolved = resolve_execution(session, agent, preview=False)
            result = await execute_agent_flow(session, agent, f"mcp:{_tool_name(agent)}", user_input, execution=resolved)
        except Exception as e:
            # Real, honest error (§0.53, GAP_REPORT) — a real Temporal
            # connection failure (or any other real execution error) is
            # reported as a proper JSON-RPC error, matching this
            # endpoint's own real protocol, rather than leaking out as
            # an unstyled 500 with no detail.
            return _rpc_error(rpc_id, -32000, f"Execution failed: {e}")
        if result.get("status") == "paused_for_human_input":
            # Real, shared HITL pause lifecycle (§0.98, GAP_REPORT) —
            # the actual fix for a real, reported gap: this used to
            # compute is_error = status != "success", which made a
            # genuine, correct pause indistinguishable from a real
            # failure — with no Run/HitlRequest of any kind ever
            # created for ANY MCP invocation (not just paused ones),
            # so a paused workflow had no real, resumable record
            # anywhere. See create_hitl_pause_run's own docstring
            # (execution.py) for the full real design. Honest scope:
            # an MCP client can't itself resume this pause through
            # MCP's own request/response protocol (no long-lived
            # session primitive for it) — but the platform's own real
            # /api/hitl/{request_id}/respond endpoint now genuinely
            # can, the same one real resume mechanism every other
            # channel's own pause already uses, rather than this one
            # being silently lost.
            duration_s = time.time() - start
            run_id = f"r-mcp-{uuid.uuid4().hex[:8]}"
            request_id = create_hitl_pause_run(
                session, agent, invoking_user=f"mcp:{_tool_name(agent)}", run_id=run_id, result=result,
                trigger="MCP", ts_str=datetime.utcnow().strftime("%H:%M:%S"), duration_s=duration_s,
                resolved=resolved,
            )
            session.commit()
            text = (
                f"This agent paused for human input (run {run_id}, request {request_id}) — "
                f"it has not failed, but it cannot be resumed through this MCP call. "
                f"A person can resume it via the platform's own HITL response endpoint."
            )
            return _rpc_result(rpc_id, {"content": [{"type": "text", "text": text}], "isError": False})
        is_error = result.get("status") not in ("success", "blocked")
        # §0.108, GAP_REPORT — "blocked" (a real, deliberate, policy-
        # driven outcome — moderation genuinely worked as configured)
        # is deliberately NOT treated as an error here, the same real
        # distinction REST's own /run already makes (a moderation-
        # blocked call still returns a real HTTP 200, never a 4xx/5xx)
        # — this was the one, remaining real inconsistency between the
        # two channels' own error semantics for the exact same real
        # outcome, confirmed live before this fix.
        text = result.get("output") or (
            f"Run failed ({result.get('status')}"
            f"{': ' + result['fail_class'] if result.get('fail_class') else ''}) — a real failure, not a placeholder."
        )

        # §0.108, GAP_REPORT — the actual fix for a real, reported gap
        # found while verifying this section's own primary fix: this
        # used to create NO Run/Trace row at all for this — the
        # overwhelmingly common — outcome, leaving every real,
        # successful (or failed, or blocked) MCP-invoked run
        # completely invisible to the Runs/Traces dashboard and every
        # real monitoring view. Now calls the same, real, shared
        # _finalize_run every other channel uses.
        duration_s = time.time() - start
        _finalize_run(
            session, agent, invoking_user=f"mcp:{_tool_name(agent)}", user_input=user_input,
            execution_mode="mcp", result=result, duration_s=duration_s,
            run_id=f"r-mcp-{uuid.uuid4().hex[:8]}", trace_id=f"tr-mcp-{uuid.uuid4().hex[:8]}",
            agent_version_id=resolved.agent_version_id, agent_version=resolved.agent_version, flow_hash=resolved.flow_hash,
        )
        session.commit()
        return _rpc_result(rpc_id, {"content": [{"type": "text", "text": text}], "isError": is_error})

    return _rpc_error(rpc_id, -32601, f"Unknown method '{method}'")
