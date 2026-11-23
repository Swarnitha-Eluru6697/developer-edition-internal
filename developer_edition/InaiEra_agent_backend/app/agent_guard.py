"""Per-agent (object-level) entitlement, on top of route permissions.

Every route first requires an app permission (app/permissions.py, e.g.
agent:studio:edit or agent:agents:publish) — that is the role-free, Admin-Portal
owned authorisation. This module adds the optional second gate: whether the
caller holds a grant on THIS agent in UCS.

The decision comes from the UCS level a caller holds on the agent (USE_AGENT /
CREATE_AGENT_VERSION / CREATE_AGENT), mapped to permissions by app/agent_roles.py — see
ucs_client.check_agent_capability. This module is where a route asks, so a permission
stops being "recorded" and starts being "enforced".

Two things make it safe to switch on:

  * identity comes from the gateway contract (gateway_identity), not from a query
    parameter a caller could choose. Routes that already accept an explicit
    `invoking_user` keep working — it is used only when the gateway gave us nobody,
    which is what keeps existing internal callers alive.

  * it is OFF by default. With AGENT_RBAC_ENFORCE unset, a would-be denial is logged
    and allowed, so the matrix can be observed against real traffic before it starts
    refusing anything. Turning it on is a deployment decision, not a code change.

A deny is a 403 naming the missing scope, because "forbidden" with no reason is the
thing that gets RBAC switched back off.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def enforcing() -> bool:
    """Whether a denial actually refuses. Read per call so it can be flipped without
    a rebuild, matching gateway_trust_enabled()'s reasoning."""
    return os.environ.get("AGENT_RBAC_ENFORCE") == "true"


def caller_groups(request) -> Optional[list]:
    """Groups the gateway asserted for this request, as the gateway-auth library
    parsed them from x-user-groups, or None when there is no gateway principal.

    Used alongside the identity so the UCS check sees the SAME membership the
    gateway verified, instead of a second lookup that can disagree with it.
    """
    try:
        from .gateway_identity import groups_from_gateway_headers
        return groups_from_gateway_headers(request)
    except Exception:
        return None


def caller(request, invoking_user: Optional[str] = None) -> Optional[str]:
    """Who is asking: the gateway-verified identity, else an explicit parameter.

    Order matters. A header the gateway signed outranks anything in the URL, so a
    caller cannot name themselves someone else by adding ?invoking_user=.
    """
    try:
        from .gateway_identity import identity_from_gateway_headers
        who = identity_from_gateway_headers(request)
        if who:
            return who
    except Exception as e:                      # never let identity plumbing 500 a route
        logger.warning("Gateway identity unavailable (%s)", e)
    return invoking_user or None


def require(request, agent_id: str, action: str, agent_name: str = "",
            invoking_user: Optional[str] = None) -> None:
    """Refuse unless the caller holds `action` on this agent, per UCS.

    Raises 403 on a real denial and 503 when UCS cannot be reached — an authorization
    gate that fails open on an outage is worse than one that fails loud, which is the
    stance check_agent_access already takes for published apps.
    """
    from .ucs_client import check_agent_capability, UCSUnavailable

    who = caller(request, invoking_user)
    if not who:
        # No identity at all. Under enforcement that is itself a refusal; otherwise it
        # is the pre-existing world, where these routes never had a caller to check.
        if enforcing():
            raise HTTPException(401, "Agent RBAC is enforced and this request carries no "
                                     "verified identity")
        logger.info("agent-rbac: no identity for %s on %s — allowed (enforcement off)",
                    action, agent_id)
        return

    try:
        d = check_agent_capability(who, agent_id, action, agent_name,
                                   groups=caller_groups(request))
    except UCSUnavailable as e:
        if enforcing():
            raise HTTPException(503, f"Could not check agent permission — UCS unreachable: {e}")
        logger.warning("agent-rbac: UCS unreachable checking %s on %s (%s) — allowed "
                       "(enforcement off)", action, agent_id, e)
        return

    if d["allowed"]:
        return
    if enforcing():
        raise HTTPException(403, f"{who} may not {action} this agent — {d['reason']}")
    logger.info("agent-rbac: WOULD DENY %s on %s for %s — %s",
                action, agent_id, who, d["reason"])
