"""Ruleset and RulesetItem CRUD. Every change bumps Ruleset.version (part of
every ArtifactVersion hash) and is audited (DQ-013)."""
from __future__ import annotations

import uuid

from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.errors import conflict, not_found, validation_failed
from app.models import JobBinding, Rule, Ruleset, RulesetItem
from app.renderers.capability import classify_items, degraded_reason, ENGINE_CAPABILITY_MATRIX
from app.renderers.params import ParameterError, validate_parameters
from app.renderers.security import RenderSecurityError, validate_identifier
from app.schemas import RulesetCreate, RulesetItemCreate, RulesetItemUpdate, RulesetUpdate
from app.services import audit


def item_dict(item: RulesetItem, rule: Rule) -> dict:
    """The one item shape every compiler/classifier consumes."""
    return {
        "item_id": str(item.id), "rule_id": str(rule.id), "rule_name": rule.name,
        "rule_type": rule.rule_type, "rule_version": int(rule.version or 1),
        "dimension": rule.dimension, "severity": rule.severity,
        "parameters": dict(rule.parameters or {}),
        "parameter_overrides": dict(item.parameter_overrides or {}),
        "column_name": item.column_name or "", "enabled": bool(item.enabled),
        "sort_order": int(item.sort_order or 0), "review_status": rule.review_status,
    }


def _check_column(column_name: str) -> None:
    if column_name:
        try:
            validate_identifier(column_name, field="column_name")
        except RenderSecurityError as e:
            raise validation_failed("column_name", str(e))


def _check_overrides(rule: Rule, overrides: dict) -> None:
    try:
        validate_parameters(rule.rule_type, {**(rule.parameters or {}), **(overrides or {})},
                            strict=False)
    except ParameterError as e:
        raise validation_failed(f"parameter_overrides.{e.field}", e.message)


class RulesetService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _audit(self, principal: Principal, rs: Ruleset, operation: str, **detail) -> None:
        await audit.record(self._db, org_id=principal.org_id, actor=principal.user_id,
                           resource_type="ruleset", resource_id=rs.id, operation=operation,
                           resource_version=rs.version, object_name=rs.name, detail=detail)

    async def _bump(self, rs: Ruleset) -> None:
        rs.version = (rs.version or 1) + 1
        await self._db.flush()
        await self._db.refresh(rs)

    async def create(self, principal: Principal, req: RulesetCreate) -> Ruleset:
        settings = get_settings()
        org_id = principal.org_id
        count = (await self._db.execute(
            select(sqlfunc.count()).select_from(Ruleset).where(Ruleset.org_id == org_id))).scalar_one()
        if count >= settings.max_rulesets_per_org:
            raise validation_failed("name", f"Max {settings.max_rulesets_per_org} rulesets per org")
        if (await self._db.execute(select(Ruleset).where(
                Ruleset.org_id == org_id, Ruleset.name == req.name))).scalar_one_or_none():
            raise conflict(f"Ruleset '{req.name}' already exists")
        rs = Ruleset(org_id=org_id, name=req.name, description=req.description,
                     created_by=principal.user_id, version=1)
        self._db.add(rs)
        await self._db.flush()
        await self._db.refresh(rs)
        await self._audit(principal, rs, "create")
        return rs

    async def list_for_org(self, org_id: str) -> list[Ruleset]:
        return list((await self._db.execute(
            select(Ruleset).where(Ruleset.org_id == org_id).order_by(Ruleset.name))).scalars())

    async def get(self, org_id: str, ruleset_id: uuid.UUID, *, with_items: bool = False) -> Ruleset:
        if with_items:
            rs = (await self._db.execute(
                select(Ruleset).options(selectinload(Ruleset.items).selectinload(RulesetItem.rule))
                .where(Ruleset.org_id == org_id, Ruleset.id == ruleset_id)
                .execution_options(populate_existing=True))).scalar_one_or_none()
        else:
            rs = await self._db.get(Ruleset, ruleset_id)
            if rs and rs.org_id != org_id:
                rs = None
        if not rs:
            raise not_found("ruleset", str(ruleset_id))
        return rs

    async def update(self, principal: Principal, ruleset_id: uuid.UUID, req: RulesetUpdate) -> Ruleset:
        rs = await self.get(principal.org_id, ruleset_id)
        if req.name is not None and req.name != rs.name:
            if (await self._db.execute(select(Ruleset).where(
                    Ruleset.org_id == principal.org_id, Ruleset.name == req.name,
                    Ruleset.id != ruleset_id))).scalar_one_or_none():
                raise conflict(f"Ruleset '{req.name}' already exists")
            rs.name = req.name
        if req.description is not None:
            rs.description = req.description
        await self._bump(rs)
        await self._audit(principal, rs, "update")
        return rs

    async def capability(self, org_id: str, ruleset_id: uuid.UUID, job_type: str,
                         enforcement_mode: str = "warn", flink_fail_stop: bool = False) -> dict:
        """DQ-CAP-007 before binding: what would be enforced for this
        job_type/mode, using rule types, typed params and review state.
        (Flink column/type checks need the job's plan — done at compile.)"""
        rs = await self.get(org_id, ruleset_id, with_items=True)
        items = [item_dict(i, i.rule) for i in rs.items]
        result = classify_items(items, job_type, enforcement_mode, flink_fail_stop=flink_fail_stop)
        return {**result, "job_type": job_type, "enforcement_mode": enforcement_mode,
                "ruleset_version": rs.version,
                "mode_semantics": ENGINE_CAPABILITY_MATRIX[job_type]["modes"][enforcement_mode],
                "degraded_reason": degraded_reason(job_type, enforcement_mode, flink_fail_stop),
                "blocking_applicable": not (enforcement_mode == "blocking" and result["unsupported"])}

    async def delete(self, principal: Principal, ruleset_id: uuid.UUID) -> None:
        rs = await self.get(principal.org_id, ruleset_id)
        bound = (await self._db.execute(select(sqlfunc.count()).select_from(JobBinding)
                                        .where(JobBinding.ruleset_id == ruleset_id))).scalar_one()
        if bound > 0:
            raise validation_failed("ruleset_id",
                                    f"Cannot delete: ruleset is bound to {bound} job(s). Remove bindings first.")
        await self._audit(principal, rs, "delete")
        await self._db.delete(rs)

    # ── Items ─────────────────────────────────────────────────────────────

    async def add_item(self, principal: Principal, ruleset_id: uuid.UUID,
                       req: RulesetItemCreate) -> RulesetItem:
        settings = get_settings()
        rs = await self.get(principal.org_id, ruleset_id)
        rule = await self._db.get(Rule, req.rule_id)
        if not rule or rule.org_id != principal.org_id:
            raise not_found("rule", str(req.rule_id))
        _check_column(req.column_name)
        _check_overrides(rule, req.parameter_overrides)
        n = (await self._db.execute(select(sqlfunc.count()).select_from(RulesetItem)
                                    .where(RulesetItem.ruleset_id == ruleset_id))).scalar_one()
        if n >= settings.max_items_per_ruleset:
            raise validation_failed("rule_id", f"Max {settings.max_items_per_ruleset} items per ruleset")
        if (await self._db.execute(select(RulesetItem).where(
                RulesetItem.ruleset_id == ruleset_id, RulesetItem.rule_id == req.rule_id,
                RulesetItem.column_name == req.column_name))).scalar_one_or_none():
            raise conflict(f"Rule '{rule.name}' already in this ruleset for column '{req.column_name}'")
        item = RulesetItem(ruleset_id=ruleset_id, rule_id=req.rule_id, column_name=req.column_name,
                           parameter_overrides=req.parameter_overrides, enabled=req.enabled,
                           sort_order=req.sort_order)
        self._db.add(item)
        await self._db.flush()
        await self._bump(rs)
        await self._audit(principal, rs, "item_add", item_id=str(item.id), rule_id=str(rule.id),
                          rule_version=rule.version)
        return item

    async def get_item(self, org_id: str, ruleset_id: uuid.UUID, item_id: uuid.UUID) -> RulesetItem:
        await self.get(org_id, ruleset_id)
        item = (await self._db.execute(
            select(RulesetItem).options(selectinload(RulesetItem.rule))
            .where(RulesetItem.id == item_id, RulesetItem.ruleset_id == ruleset_id)
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if not item:
            raise not_found("ruleset_item", str(item_id))
        return item

    async def update_item(self, principal: Principal, ruleset_id: uuid.UUID, item_id: uuid.UUID,
                          req: RulesetItemUpdate) -> RulesetItem:
        item = await self.get_item(principal.org_id, ruleset_id, item_id)
        if req.column_name is not None:
            _check_column(req.column_name)
            item.column_name = req.column_name
        if req.parameter_overrides is not None:
            _check_overrides(item.rule, req.parameter_overrides)
            item.parameter_overrides = req.parameter_overrides
        if req.enabled is not None:
            item.enabled = req.enabled
        if req.sort_order is not None:
            item.sort_order = req.sort_order
        await self._db.flush()
        rs = await self.get(principal.org_id, ruleset_id)
        await self._bump(rs)
        await self._audit(principal, rs, "item_update", item_id=str(item.id))
        return await self.get_item(principal.org_id, ruleset_id, item_id)

    async def delete_item(self, principal: Principal, ruleset_id: uuid.UUID, item_id: uuid.UUID) -> None:
        item = await self.get_item(principal.org_id, ruleset_id, item_id)
        await self._db.delete(item)
        await self._db.flush()
        rs = await self.get(principal.org_id, ruleset_id)
        await self._bump(rs)
        await self._audit(principal, rs, "item_delete", item_id=str(item_id))
