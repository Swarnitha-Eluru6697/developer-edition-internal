"""Rulesets API."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import func as sf, select

from app.core.deps import DB, Reader, RulesWriter
from app.models import RulesetItem
from app.schemas import (
    EnforcementMode, JobType, RulesetCapability, RulesetCreate, RulesetItemCreate, RulesetItemOut,
    RulesetItemUpdate, RulesetOut, RulesetSummary, RulesetUpdate,
)
from app.services.ruleset_service import RulesetService

router = APIRouter(prefix="/api/v1/rulesets", tags=["rulesets"])


@router.get("", response_model=list[RulesetSummary])
async def list_rulesets(user: Reader, db: DB) -> list[RulesetSummary]:
    rulesets = await RulesetService(db).list_for_org(user.org_id)
    counts = dict((await db.execute(
        select(RulesetItem.ruleset_id, sf.count()).group_by(RulesetItem.ruleset_id))).all())
    return [RulesetSummary(id=rs.id, org_id=rs.org_id, name=rs.name, description=rs.description,
                           version=rs.version, created_at=rs.created_at,
                           item_count=counts.get(rs.id, 0)) for rs in rulesets]


@router.post("", response_model=RulesetOut, status_code=status.HTTP_201_CREATED)
async def create_ruleset(payload: RulesetCreate, user: RulesWriter, db: DB) -> RulesetOut:
    svc = RulesetService(db)
    rs = await svc.create(user, payload)
    return RulesetOut.model_validate(await svc.get(user.org_id, rs.id, with_items=True))


@router.get("/{ruleset_id}", response_model=RulesetOut)
async def get_ruleset(ruleset_id: UUID, user: Reader, db: DB) -> RulesetOut:
    return RulesetOut.model_validate(
        await RulesetService(db).get(user.org_id, ruleset_id, with_items=True))


@router.patch("/{ruleset_id}", response_model=RulesetOut)
async def update_ruleset(ruleset_id: UUID, payload: RulesetUpdate, user: RulesWriter,
                         db: DB) -> RulesetOut:
    svc = RulesetService(db)
    await svc.update(user, ruleset_id, payload)
    return RulesetOut.model_validate(await svc.get(user.org_id, ruleset_id, with_items=True))


@router.delete("/{ruleset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ruleset(ruleset_id: UUID, user: RulesWriter, db: DB) -> None:
    await RulesetService(db).delete(user, ruleset_id)


@router.get("/{ruleset_id}/capability", response_model=RulesetCapability)
async def get_ruleset_capability(ruleset_id: UUID, user: Reader, db: DB, job_type: JobType,
                                 enforcement_mode: EnforcementMode = "warn",
                                 flink_fail_stop: bool = False) -> RulesetCapability:
    """DQ-CAP-007: which rules would be supported / unsupported / degraded for a
    job_type + enforcement_mode, BEFORE creating a binding."""
    return RulesetCapability(**await RulesetService(db).capability(
        user.org_id, ruleset_id, job_type, enforcement_mode, flink_fail_stop))


@router.get("/{ruleset_id}/items", response_model=list[RulesetItemOut])
async def list_items(ruleset_id: UUID, user: Reader, db: DB) -> list[RulesetItemOut]:
    rs = await RulesetService(db).get(user.org_id, ruleset_id, with_items=True)
    return [RulesetItemOut.model_validate(i) for i in rs.items]


@router.post("/{ruleset_id}/items", response_model=RulesetItemOut, status_code=status.HTTP_201_CREATED)
async def add_item(ruleset_id: UUID, payload: RulesetItemCreate, user: RulesWriter,
                   db: DB) -> RulesetItemOut:
    svc = RulesetService(db)
    item = await svc.add_item(user, ruleset_id, payload)
    return RulesetItemOut.model_validate(await svc.get_item(user.org_id, ruleset_id, item.id))


@router.patch("/{ruleset_id}/items/{item_id}", response_model=RulesetItemOut)
async def update_item(ruleset_id: UUID, item_id: UUID, payload: RulesetItemUpdate,
                      user: RulesWriter, db: DB) -> RulesetItemOut:
    return RulesetItemOut.model_validate(
        await RulesetService(db).update_item(user, ruleset_id, item_id, payload))


@router.delete("/{ruleset_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(ruleset_id: UUID, item_id: UUID, user: RulesWriter, db: DB) -> None:
    await RulesetService(db).delete_item(user, ruleset_id, item_id)
