"""
Operations router — GET /operations/{id} polls a long-running action
submitted by another route (pipeline deploy/pause/resume/restart,
connection discover) via app/services/operation_runner.py.

Read-only and org-scoped like GET /pipelines/{id}/status. Requires
dataconnect:pipelines:view: an Operation's resource_type is "pipeline" or
"connection" (see app/models/operations.py); pipeline lifecycle operations
are the more common case polled through this endpoint.
"""

from fastapi import APIRouter, Depends, HTTPException
from gateway_auth import Principal

from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.operations import Operation
from app.services import operation_runner

router = APIRouter()


@router.get("/{operation_id}", response_model=Operation, summary="Get operation status")
async def get_operation(
    operation_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> dict:
    operation = await operation_runner.get_operation(org_id, operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation
