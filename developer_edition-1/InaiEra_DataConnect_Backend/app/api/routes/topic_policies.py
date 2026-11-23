from fastapi import APIRouter, Depends, status
from gateway_auth import Principal

from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.topic import TopicPolicy, TopicPolicyCreate
from app.services.topic_policy import topic_policy_service

router = APIRouter()


@router.get("", response_model=list[TopicPolicy])
async def list_topic_policies(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> list[dict]:
    return await topic_policy_service.list_policies(org_id)


@router.post("", response_model=TopicPolicy, status_code=status.HTTP_201_CREATED)
async def create_topic_policy(
    payload: TopicPolicyCreate,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:edit")),
) -> dict:
    return await topic_policy_service.create_policy(org_id, payload.model_dump(exclude_none=True))


@router.get("/{policy_id}", response_model=TopicPolicy)
async def get_topic_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> dict:
    return await topic_policy_service.get_or_404(org_id, policy_id)


@router.put("/{policy_id}", response_model=TopicPolicy)
async def update_topic_policy(
    policy_id: str,
    payload: TopicPolicyCreate,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:edit")),
) -> dict:
    # PUT replaces the whole resource — include every field (not just the
    # ones set), so an omitted field is explicitly cleared rather than left
    # untouched, matching the old in-memory store's upsert() semantics.
    data = payload.model_dump()
    data.pop("id", None)  # the path param owns identity — never let the body move it
    return await topic_policy_service.update_policy(org_id, policy_id, data)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic_policy(
    policy_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:edit")),
) -> None:
    await topic_policy_service.get_or_404(org_id, policy_id)  # 404s if missing or owned by another org
    await topic_policy_service.delete_policy(org_id, policy_id)
