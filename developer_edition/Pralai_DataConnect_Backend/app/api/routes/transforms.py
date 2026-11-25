from fastapi import APIRouter, Depends, HTTPException, status
from gateway_auth import Principal

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.common import ValidationResult
from app.schemas.transform import TransformPolicy, TransformPolicyCreate, TransformPreviewRequest
from app.services.transform_policy import generate_smt_config, validate_transform_settings
from app.services.transform_service import transform_service

router = APIRouter()


@router.get("", response_model=list[TransformPolicy])
async def list_transform_policies(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:transforms:view")),
) -> list[dict]:
    return await transform_service.list_policies(org_id)


@router.post("", response_model=TransformPolicy, status_code=status.HTTP_201_CREATED)
async def create_transform_policy(
    payload: TransformPolicyCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:transforms:edit")),
) -> dict:
    data = payload.model_dump(exclude_none=True)
    data["org_id"] = org_id
    generate_smt_config(data.get("transforms", []))
    validate_transform_settings(data.get("transforms", []))
    created = await transform_service.create_policy(org_id, data)
    await record_audit(
        org_id=org_id, actor=actor, action="created",
        resource_type="transform_policy", resource_id=created["id"], after_state=created,
    )
    return created


@router.get("/{policy_id}", response_model=TransformPolicy)
async def get_transform_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:transforms:view")),
) -> dict:
    policy = await transform_service.get_policy(org_id, policy_id)
    if not policy or policy["org_id"] != org_id:
        raise HTTPException(status_code=404, detail="Transform policy not found")
    return policy


@router.put("/{policy_id}", response_model=TransformPolicy)
async def update_transform_policy(
    policy_id: str,
    payload: TransformPolicyCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:transforms:edit")),
) -> dict:
    before = await get_transform_policy(policy_id, org_id)  # 404s if missing or owned by another org
    data = payload.model_dump(exclude_none=True)
    data.pop("origin", None)  # how a policy was made is fixed at creation: an edit that omits it must not reset an "auto" policy to "custom"
    data["org_id"] = org_id
    generate_smt_config(data.get("transforms", []))
    validate_transform_settings(data.get("transforms", []))
    updated = await transform_service.update_policy(org_id, policy_id, data)
    await record_audit(
        org_id=org_id, actor=actor, action="updated",
        resource_type="transform_policy", resource_id=policy_id,
        before_state=before, after_state=updated,
    )
    return updated


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transform_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:transforms:edit")),
) -> None:
    # Previously deleted with no existence/org-ownership check at all — an
    # audit record for "org X deleted policy Y" is only honest if we first
    # confirm Y actually belonged to X, so this now 404s the same way every
    # other resource's delete route already does.
    before = await get_transform_policy(policy_id, org_id)
    await transform_service.delete_policy(org_id, policy_id)
    await record_audit(
        org_id=org_id, actor=actor, action="deleted",
        resource_type="transform_policy", resource_id=policy_id, before_state=before,
    )


@router.post("/{policy_id}/validate", response_model=ValidationResult)
async def validate_transform_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:transforms:edit")),
) -> dict:
    try:
        policy = await get_transform_policy(policy_id, org_id)
        generate_smt_config(policy.get("transforms", []))
        validate_transform_settings(policy.get("transforms", []))
    except ValueError as exc:
        return {"valid": False, "errors": [str(exc)]}
    return {"valid": True, "errors": []}


@router.post("/{policy_id}/preview")
async def preview_transform_policy(
    policy_id: str,
    payload: TransformPreviewRequest,
    org_id: str = Depends(get_org_id),
    # FE-BUG-19: kept at transforms:edit, not loosened to transforms:view — this is the interactive "type a
    # sample record, POST for a preview" tool. The read-only view of a policy's SMT chain is the GET-backed
    # TransformViewModal (Transforms.tsx).
    _principal: Principal = Depends(require("dataconnect:transforms:edit")),
) -> dict:
    policy = await get_transform_policy(policy_id, org_id)
    return {
        "input_record": payload.input_record,
        "output_record": payload.input_record,
        "generated_config": generate_smt_config(policy.get("transforms", [])),
    }
