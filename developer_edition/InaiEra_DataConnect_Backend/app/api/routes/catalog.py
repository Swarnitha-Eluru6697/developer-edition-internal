from fastapi import APIRouter, Depends, HTTPException, status
from gateway_auth import Principal

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.permissions import require
from app.schemas.catalog import (
    ConnectorTemplate,
    ConnectorTemplateCreate,
    ConnectorTemplateRevision,
)
from app.services import connector_availability
from app.services.catalog_service import catalog_service

router = APIRouter()


@router.get("", response_model=list[ConnectorTemplate])
async def list_connector_templates(
    _principal: Principal = Depends(require("dataconnect:templates:view")),
) -> list[dict]:
    return await connector_availability.annotate(await catalog_service.list_templates())


@router.post("", response_model=ConnectorTemplate, status_code=status.HTTP_201_CREATED)
async def create_connector_template(
    payload: ConnectorTemplateCreate,
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:templates:manage")),
) -> dict:
    created = await catalog_service.create_template(payload.model_dump(exclude_none=True))
    # Connector templates are platform-global, not org-scoped (no
    # Depends(get_org_id) anywhere in this file) — org_id=None here is the
    # rare legitimate case, not an omission. See AuditLog's docstring.
    await record_audit(
        org_id=None, actor=actor, action="created",
        resource_type="connector_template", resource_id=created["id"], after_state=created,
    )
    return created


@router.get("/{template_id}", response_model=ConnectorTemplate)
async def get_connector_template(
    template_id: str,
    _principal: Principal = Depends(require("dataconnect:templates:view")),
) -> dict:
    template = await catalog_service.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Connector template not found")
    return (await connector_availability.annotate([template]))[0]


@router.put("/{template_id}", response_model=ConnectorTemplate)
async def update_connector_template(
    template_id: str,
    payload: ConnectorTemplateCreate,
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:templates:manage")),
) -> dict:
    before = await catalog_service.get_template(template_id)
    if not before:
        raise HTTPException(status_code=404, detail="Connector template not found")
    updated = await catalog_service.update_template(template_id, payload.model_dump(exclude_none=True))
    await record_audit(
        org_id=None, actor=actor, action="updated",
        resource_type="connector_template", resource_id=template_id,
        before_state=before, after_state=updated,
    )
    return updated


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector_template(
    template_id: str,
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:templates:manage")),
) -> None:
    before = await catalog_service.get_template(template_id)
    if not before:
        raise HTTPException(status_code=404, detail="Connector template not found")
    await catalog_service.delete_template(template_id)
    await record_audit(
        org_id=None, actor=actor, action="deleted",
        resource_type="connector_template", resource_id=template_id, before_state=before,
    )


@router.get(
    "/{template_id}/revisions",
    response_model=list[ConnectorTemplateRevision],
    summary="Immutable revision history for one connector template",
)
async def list_connector_template_revisions(
    template_id: str,
    _principal: Principal = Depends(require("dataconnect:templates:view")),
) -> list[dict]:
    return await catalog_service.list_revisions(template_id)
