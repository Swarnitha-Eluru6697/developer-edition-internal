from fastapi import APIRouter, Depends, HTTPException, Request, status
from gateway_auth import Principal

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.common import ValidationResult
from app.schemas.schema import (
    SchemaDriftApproval,
    SchemaDriftApprovalCreate,
    SchemaDriftRequest,
    SchemaDriftResult,
    SchemaDriftReviewRequest,
    SchemaInferRequest,
    SchemaInferResult,
    SchemaVersion,
    SchemaVersionCreate,
)
from app.services.schema_service import schema_service

router = APIRouter()


@router.post("/infer", response_model=SchemaInferResult)
async def infer_schema_endpoint(
    payload: SchemaInferRequest,
    request: Request,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:edit")),
) -> dict:
    content_length = request.headers.get("content-length")
    try:
        return await schema_service.infer(
            org_id,
            payload.records,
            content_length=int(content_length) if content_length and content_length.isdigit() else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc


@router.get("", response_model=list[SchemaVersion])
async def list_schemas(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:view")),
) -> list[dict]:
    return await schema_service.list_schemas(org_id)


@router.post("", response_model=SchemaVersion, status_code=status.HTTP_201_CREATED)
async def create_schema(
    payload: SchemaVersionCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:schemas:edit")),
) -> dict:
    created = await schema_service.create_schema(org_id, payload.model_dump(exclude_none=True))
    await record_audit(
        org_id=org_id, actor=actor, action="created",
        resource_type="schema_version", resource_id=created["id"], after_state=created,
    )
    return created


@router.get("/drift-approvals", response_model=list[SchemaDriftApproval])
async def list_drift_approvals(
    status: str | None = None,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:view")),
) -> list[dict]:
    return await schema_service.list_drift_approvals(org_id, status=status)


@router.post("/drift-approvals", response_model=SchemaDriftApproval)
async def create_drift_approval(
    payload: SchemaDriftApprovalCreate,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:edit")),
) -> dict:
    return await schema_service.create_drift_approval(org_id, payload.model_dump(exclude_none=True))


@router.post("/drift-approvals/{approval_id}/approve", response_model=SchemaDriftApproval)
async def approve_drift(
    approval_id: str,
    payload: SchemaDriftReviewRequest,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:schemas:approve-drift")),
) -> dict:
    approval = await schema_service.approve_drift(
        org_id,
        approval_id,
        payload.reviewed_by,
        payload.comment,
        resulting_schema_version_id=payload.resulting_schema_version_id,
    )
    if not approval:
        raise HTTPException(status_code=404, detail="Schema drift approval not found")
    # `actor` (the platform-supplied caller identity) and `reviewed_by` (a
    # self-reported name in the request body, kept on the domain record
    # itself — see SchemaDriftApproval.reviewed_by) are deliberately
    # independent: the audit trail records who actually called the API,
    # regardless of what name they typed into the review comment.
    await record_audit(
        org_id=org_id, actor=actor, action="approved",
        resource_type="schema_drift_approval", resource_id=approval_id, after_state=approval,
    )
    return approval


@router.post("/drift-approvals/{approval_id}/reject", response_model=SchemaDriftApproval)
async def reject_drift(
    approval_id: str,
    payload: SchemaDriftReviewRequest,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:schemas:approve-drift")),
) -> dict:
    approval = await schema_service.reject_drift(org_id, approval_id, payload.reviewed_by, payload.comment)
    if not approval:
        raise HTTPException(status_code=404, detail="Schema drift approval not found")
    await record_audit(
        org_id=org_id, actor=actor, action="rejected",
        resource_type="schema_drift_approval", resource_id=approval_id, after_state=approval,
    )
    return approval


@router.get("/{schema_id}", response_model=SchemaVersion)
async def get_schema(
    schema_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:view")),
) -> dict:
    schema = await schema_service.get_schema(org_id, schema_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Schema not found")
    return schema


@router.post("/{schema_id}/versions", response_model=SchemaVersion)
async def create_schema_version(
    schema_id: str,
    payload: SchemaVersionCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:schemas:edit")),
) -> dict:
    await get_schema(schema_id, org_id)
    created = await schema_service.create_schema(org_id, payload.model_dump(exclude_none=True))
    await record_audit(
        org_id=org_id, actor=actor, action="created",
        resource_type="schema_version", resource_id=created["id"], after_state=created,
    )
    return created


@router.post("/{schema_id}/validate-compatibility", response_model=ValidationResult)
async def validate_compatibility(
    schema_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:edit")),
) -> dict:
    return schema_service.validate_compatibility(await get_schema(schema_id, org_id))


@router.get("/{schema_id}/drift-events")
async def drift_events(
    schema_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:view")),
) -> list[dict]:
    return schema_service.drift_events(await get_schema(schema_id, org_id))


@router.post("/{schema_id}/detect-drift", response_model=SchemaDriftResult)
async def detect_drift(
    schema_id: str,
    payload: SchemaDriftRequest,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:schemas:edit")),
) -> dict:
    schema = await get_schema(schema_id, org_id)
    result = schema_service.detect_drift(schema, payload.records)
    if result["approval_required"]:
        approval = await schema_service.create_drift_approval(
            org_id,
            {
                "schema_version_id": schema_id,
                "pipeline_id": payload.pipeline_id,
                "run_id": payload.run_id,
                "observed_schema": result["observed_schema"],
                "drift_events": result["events"],
                "detected_fingerprint": result["detected_fingerprint"],
                "current_fingerprint": result["current_fingerprint"],
            },
        )
        result["approval_request_id"] = approval["id"]
    result.pop("observed_schema", None)
    return result
