"""
Real Guardrails overview (§0.49, GAP_REPORT) - a single, honest
aggregation of every real guardrail mechanism this platform actually
has, split into what's genuinely per-agent and editable here (content
moderation, gateway-trust auth - both real features.* toggles already
used elsewhere, not a second copy of the same setting) versus what's
structural and platform-wide (UCS entitlement, runtime safety caps,
build/publish-time validation) - shown as real, live reference data,
not something this page pretends to let you edit.

Every constant returned here is imported directly from its real
source of truth (harness.py, sandbox.py) rather than re-typed as a
duplicate literal - the exact drift risk this same file's own
SUB_AGENT_HOP_CAP fix (see harness.py) was written to eliminate.
"""
from typing import List

from fastapi import APIRouter, Depends
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select

from ..db import get_session
from ..models import Agent
from ..harness import MAX_GRAPH_STEPS, SUB_AGENT_HOP_CAP, CHAT_HISTORY_MAX_TURNS
from ..flow_compiler import objects_to_check
from ..sandbox import TIMEOUT_SECONDS, MEMORY_LIMIT_MB

router = APIRouter(prefix="/api/guardrails", tags=["guardrails"])

STEP_LIMIT_DEFAULT = 12
MAX_LOOPS_DEFAULT = 10

_LLM_AGENT_TYPES = {"llm", "agent"}


def moderation_effect(moderation: dict) -> dict:
    """Whether a moderation config can actually block anything.

    `enabled` alone does not mean protected. With no provider the check
    falls through to keywords, and with no keyword list it never
    matches — so a config of {"enabled": true} refuses nothing, not even
    a request for bomb-making instructions. Confirmed directly against
    _moderation_flagged.

    Seven agents on this install were in exactly that state while the
    Guardrails page showed them a green "on", which is the most
    dangerous thing a guardrails page can do: report protection that is
    not there.

    Returns the verdict and, when it cannot act, the reason.
    """
    if not moderation.get("enabled"):
        return {"effective": False, "reason": "off"}

    provider = (moderation.get("provider") or "").strip()
    if not provider:
        return {"effective": False,
                "reason": "no provider chosen — this blocks nothing"}
    if provider == "keywords" and not [k for k in (moderation.get("keywords") or [])
                                       if str(k).strip()]:
        return {"effective": False,
                "reason": "keywords provider with an empty word list — nothing to match"}
    if provider == "custom_endpoint" and not (moderation.get("custom_endpoint_url") or "").strip():
        return {"effective": False,
                "reason": "custom endpoint with no URL — nothing to call"}
    if not (moderation.get("input_enabled", True) or moderation.get("output_enabled", True)):
        return {"effective": False,
                "reason": "neither input nor output moderation is on"}
    return {"effective": True, "reason": ""}


@router.get("/overview", dependencies=[Depends(require_permission(P.GUARDRAILS))])
def get_guardrails_overview(session: Session = Depends(get_session)) -> dict:
    agents = session.exec(select(Agent).where(Agent.plane == "App")).all()
    agent_rows: List[dict] = []
    for a in agents:
        nodes = (a.flow or {}).get("nodes", [])
        features = a.features or {}
        moderation = features.get("moderation") or {}

        llm_agent_nodes = [n for n in nodes if n.get("type") in _LLM_AGENT_TYPES]
        loop_nodes = [n for n in nodes if n.get("type") == "loop"]
        llm_agent_with_custom_step_limit = sum(
            1 for n in llm_agent_nodes if (n.get("config") or {}).get("step_limit") not in (None, "")
        )
        loop_with_custom_max_loops = sum(
            1 for n in loop_nodes if (n.get("config") or {}).get("max_loops") not in (None, "")
        )

        agent_rows.append({
            "id": a.id, "name": a.name, "status": a.status, "app_type": a.app_type,
            "moderation": {
                "enabled": bool(moderation.get("enabled")),
                "provider": moderation.get("provider") or None,
                "input_enabled": moderation.get("input_enabled", True),
                "output_enabled": moderation.get("output_enabled", True),
                # Computed here so the page cannot draw its own
                # conclusion from `enabled` and get it wrong.
                **moderation_effect(moderation),
            },
            "require_authenticated_user": bool(features.get("require_authenticated_user")),
            "ucs": {
                "touches_checked_objects": bool(objects_to_check(nodes)),
                "checked_object_count": len(objects_to_check(nodes)),
            },
            "safety_caps": {
                "llm_agent_node_count": len(llm_agent_nodes),
                "llm_agent_nodes_with_custom_step_limit": llm_agent_with_custom_step_limit,
                "loop_node_count": len(loop_nodes),
                "loop_nodes_with_custom_max_loops": loop_with_custom_max_loops,
            },
        })

    return {
        "agents": agent_rows,
        "platform_constants": {
            "max_graph_steps": MAX_GRAPH_STEPS,
            "sub_agent_hop_cap": SUB_AGENT_HOP_CAP,
            "step_limit_default": STEP_LIMIT_DEFAULT,
            "max_loops_default": MAX_LOOPS_DEFAULT,
            "code_sandbox_timeout_seconds": TIMEOUT_SECONDS,
            "code_sandbox_memory_mb": MEMORY_LIMIT_MB,
            "chat_history_max_turns": CHAT_HISTORY_MAX_TURNS,
        },
    }
