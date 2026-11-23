"""Route permissions for this service (contract docs/auth/01-CONTRACT.md).

Authorisation is by permission ID only. The IDs are declared in the repo's
permissions.json (imported by the Admin Portal); which person holds which ID
is data owned by the Admin Portal and arrives in Envoy's x-user-permissions
header. Nothing here knows any role name.

`protect_app` (app/main.py) already refuses every non-public route without a
gateway-verified principal (401). These dependencies add the 403 for a
principal that lacks the permission a route needs.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status
from gateway_auth import Principal, require_permission

from .gateway_identity import get_principal

ACCESS = "agent:access:view"
OVERVIEW = "agent:overview:view"
STUDIO = "agent:studio:edit"
TEMPLATES_VIEW = "agent:templates:view"
TEMPLATES_PLATFORM = "agent:templates:manage-platform"
RAG = "agent:rag:manage"
INTEGRATIONS = "agent:integrations:manage"
RUNS = "agent:runs:view"
TRACES = "agent:traces:view"
MONITORING = "agent:monitoring:view"
GUARDRAILS = "agent:guardrails:manage"
ALERTS = "agent:alerts:manage"
WEBAPPS = "agent:webapps:use"
SETTINGS = "agent:settings:edit"
AGENTS_RUN = "agent:agents:run"
AGENTS_PUBLISH = "agent:agents:publish"
MAINTENANCE = "agent:maintenance:run"

# Pages that list or read agents (their config, versions, logs, quality).
AGENT_READERS = (OVERVIEW, STUDIO, RUNS, TRACES, MONITORING, GUARDRAILS, ALERTS)
# Invoking an agent: a builder/operator testing it, or someone using its web app.
AGENT_INVOKERS = (AGENTS_RUN, WEBAPPS)


def require_any_permission(*permissions: str):
    """Dependency: 401 without a gateway principal, 403 unless it holds at
    least one of `permissions`. For endpoints shared by several pages (the
    agent list is read by Overview, Studio, Runs ...)."""
    if len(permissions) == 1:
        return require_permission(permissions[0])
    wanted = tuple(permissions)

    def dependency(request: Request) -> Principal:
        p = get_principal(request)
        if not any(p.has(x) for x in wanted):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"missing permission (one of: {', '.join(wanted)})")
        return p

    return dependency


def principal_has(request: Request, permission: str) -> bool:
    """Whether this request's gateway principal holds `permission`; False when
    there is no principal. For in-handler decisions that depend on the
    resource (e.g. a platform template vs a private one)."""
    try:
        return get_principal(request).has(permission)
    except HTTPException:
        return False
