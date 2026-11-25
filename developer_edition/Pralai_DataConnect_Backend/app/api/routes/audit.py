"""
Audit / history routes.

Read-only by design: every mutating route already writes its own row via
app.core.audit.record_audit(); this is the first (and only) place those rows
are read back. Requires dataconnect:audit:view. A pipeline's generated_config
inside a before/after snapshot is only shown to a caller who also holds
dataconnect:pipelines:deploy (see app/core/response_filters.py).
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from gateway_auth import Principal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import require
from app.core.response_filters import audit_state_for
from app.core.tenant import get_org_id
from app.db.session import get_session
from app.models.audit import AuditLog
from app.schemas.audit import AuditLogEntry

router = APIRouter()


def _to_entry(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "actor": row.actor,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "before_state": row.before_state,
        "after_state": row.after_state,
        "created_at": row.created_at,
    }


@router.get("", response_model=list[AuditLogEntry], summary="List audit/history entries for this org")
async def list_audit_logs(
    resource_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    org_id: str = Depends(get_org_id),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require("dataconnect:audit:view")),
) -> list[dict]:
    stmt = select(AuditLog).where(AuditLog.org_id == org_id).order_by(AuditLog.created_at.desc()).limit(limit)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    result = await session.execute(stmt)
    entries = [_to_entry(row) for row in result.scalars().all()]
    # A pipeline's before/after snapshot embeds its generated_config; audit:view does not grant that config
    # (BUG-07), so it is emptied for a caller without dataconnect:pipelines:deploy.
    for entry in entries:
        entry["before_state"] = audit_state_for(principal, entry["before_state"])
        entry["after_state"] = audit_state_for(principal, entry["after_state"])
    return entries
