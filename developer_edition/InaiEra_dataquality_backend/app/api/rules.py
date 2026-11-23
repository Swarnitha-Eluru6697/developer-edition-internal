"""Rules API."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.core.deps import DB, Reader, RulesWriter
from app.schemas import RuleApprove, RuleCreate, RuleOut, RuleUpdate
from app.services.rule_service import RuleService

router = APIRouter(prefix="/api/v1/rules", tags=["rules"])


@router.get("", response_model=list[RuleOut])
async def list_rules(user: Reader, db: DB, dimension: str | None = Query(None),
                     rule_type: str | None = Query(None), tag: str | None = Query(None)):
    rules = await RuleService(db).list_for_org(user.org_id, dimension=dimension,
                                               rule_type=rule_type, tag=tag)
    return [RuleOut.model_validate(r) for r in rules]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(payload: RuleCreate, user: RulesWriter, db: DB) -> RuleOut:
    return RuleOut.model_validate(await RuleService(db).create(user, payload))


@router.get("/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: UUID, user: Reader, db: DB) -> RuleOut:
    return RuleOut.model_validate(await RuleService(db).get(user.org_id, rule_id))


@router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: UUID, payload: RuleUpdate, user: RulesWriter, db: DB) -> RuleOut:
    return RuleOut.model_validate(await RuleService(db).update(user, rule_id, payload))


@router.post("/{rule_id}/approve", response_model=RuleOut)
async def approve_rule(rule_id: UUID, payload: RuleApprove, user: RulesWriter, db: DB) -> RuleOut:
    """Approve a privileged (custom_sql) rule — requires dq.custom_sql.write and
    acknowledged_privileged=true. Bumps the rule version."""
    return RuleOut.model_validate(
        await RuleService(db).approve(user, rule_id, payload.acknowledged_privileged))


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: UUID, user: RulesWriter, db: DB) -> None:
    await RuleService(db).delete(user, rule_id)
