"""Audit logging.

Every state-changing action goes through this service. Audit log is append-only.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


class AuditService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def record(
        self,
        *,
        user_id: uuid.UUID | None,
        action: str,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        metadata: dict[str, Any] | None = None,
        # WS-AUDIT-016: optional, additive-only. Existing call sites (there
        # are ~30) never pass these and are completely unaffected; a caller
        # that has a workspace/run in scope can now record it without
        # inventing its own metadata key for it.
        workspace_id: uuid.UUID | None = None,
        run_id: uuid.UUID | str | None = None,
    ) -> None:
        # request_id/trace_id/org_id are already known for every request
        # (bound by RequestContextMiddleware + get_current_user — see
        # app/core/observability.py) — pulled in here automatically so every
        # audit row carries them without any caller having to pass them.
        # get_contextvars() is a plain dict read; it can't fail, so no
        # try/except is needed to keep this call site as safe as the rest.
        ctx = structlog.contextvars.get_contextvars()
        standardized: dict[str, Any] = {}
        if ctx.get("request_id"):
            standardized["request_id"] = ctx["request_id"]
        if ctx.get("trace_id"):
            standardized["trace_id"] = ctx["trace_id"]
        if ctx.get("org_id"):
            standardized["org_id"] = ctx["org_id"]
        # workspace_id / run_id: explicit, else implied by the resource, else
        # the request's own path context (observability.bind_path_context).
        if workspace_id is None:
            if resource_type == "workspace" and resource_id is not None:
                workspace_id = resource_id
            else:
                workspace_id = ctx.get("workspace_id")
        if run_id is None:
            if resource_type == "execution" and resource_id is not None:
                run_id = resource_id
            else:
                run_id = ctx.get("execution_id")
        if workspace_id is not None:
            standardized["workspace_id"] = str(workspace_id)
        if run_id is not None:
            standardized["run_id"] = str(run_id)

        self._db.add(AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=_valid_ip(ip_address),
            user_agent=user_agent,
            # Caller-supplied metadata wins on key collision — it's more
            # specific to the action than these generic correlation fields.
            metadata_={**standardized, **(metadata or {})},
        ))
        await self._db.flush()


def _valid_ip(value: str | None) -> str | None:
    """audit_logs.ip_address is Postgres INET: anything that isn't an IP
    (a unix-socket peer, a proxy-supplied name) would fail the INSERT and
    with it the whole request. Record it only when it is one."""
    if not value:
        return None
    import ipaddress
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None
