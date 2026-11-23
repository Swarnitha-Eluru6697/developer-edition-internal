"""Rule CRUD.

Versioning: `Rule.version` increments on every change that can affect a
compiled artifact (parameters, severity, review approval) and is part of
every ArtifactVersion hash and audit event.

DQ-SEC-006 custom_sql is PRIVILEGED: creating, changing, approving or
deleting one requires the dq.custom_sql.write permission AND an explicit
`acknowledged_privileged=true`; the rule is then stored with
review_status='approved' + reviewer + time. Rules whose review is
'pending' (e.g. pre-existing ones, see migration 0003) never compile.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.errors import conflict, not_found, system_rule_immutable, validation_failed
from app.core.permissions import ensure_permission
from app.models import Rule
from app.renderers.params import ParameterError, validate_parameters
from app.schemas import RuleCreate, RuleUpdate
from app.services import audit

PRIVILEGED_RULE_TYPES = {"custom_sql"}

SYSTEM_RULES: list[dict] = [
    {"name": "not_null", "display_name": "Not Null", "rule_type": "not_null",
     "dimension": "nulls", "severity": "ERROR",
     "description": "Column must not contain NULL values.", "parameters": {}},
    {"name": "unique", "display_name": "Unique", "rule_type": "unique",
     "dimension": "uniqueness", "severity": "ERROR",
     "description": "Column values must be unique across the dataset.", "parameters": {}},
    {"name": "accepted_values", "display_name": "Accepted Values",
     "rule_type": "accepted_values", "dimension": "validity", "severity": "ERROR",
     "description": "Column values must be in the specified set.", "parameters": {"values": []}},
    {"name": "range_check", "display_name": "Range Check",
     "rule_type": "range", "dimension": "validity", "severity": "ERROR",
     "description": "Column numeric values must be within min/max bounds.",
     "parameters": {"min": None, "max": None}},
    {"name": "regex_match", "display_name": "Regex Match",
     "rule_type": "regex", "dimension": "validity", "severity": "ERROR",
     "description": "Column values must match the regex pattern.", "parameters": {"pattern": ".*"}},
    {"name": "freshness_check", "display_name": "Freshness Check",
     "rule_type": "freshness", "dimension": "freshness", "severity": "WARNING",
     "description": "Timestamp column must not be older than max_age_hours.",
     "parameters": {"max_age_hours": 6}},
    {"name": "row_count_min", "display_name": "Minimum Row Count",
     "rule_type": "row_count", "dimension": "volume", "severity": "ERROR",
     "description": "Table must have at least min_rows rows.", "parameters": {"min_rows": 1}},
    {"name": "row_count_anomaly", "display_name": "Row Count Anomaly",
     "rule_type": "row_count_anomaly", "dimension": "volume", "severity": "WARNING",
     "description": "Row count must not deviate more than pct_threshold% from the 7-day average.",
     "parameters": {"pct_threshold": 20}},
]


def _validate(rule_type: str, parameters: dict) -> dict:
    try:
        return validate_parameters(rule_type, parameters, strict=False)
    except ParameterError as e:
        raise validation_failed(f"parameters.{e.field}", e.message)


def _require_privilege(principal: Principal, acknowledged: bool, rule_type: str) -> None:
    ensure_permission(principal, "dq.custom_sql.write")
    if not acknowledged:
        raise validation_failed(
            "acknowledged_privileged",
            f"rule_type '{rule_type}' compiles its condition into generated SQL — set "
            "acknowledged_privileged=true to confirm this predicate has been reviewed")


def _approve(rule: Rule, principal: Principal) -> None:
    rule.privileged = True
    rule.review_status = "approved"
    rule.reviewed_by = principal.user_id
    rule.reviewed_at = datetime.now(timezone.utc)


class RuleService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def seed_system_rules(self, org_id: str) -> int:
        created = 0
        for r in SYSTEM_RULES:
            exists = (await self._db.execute(
                select(Rule).where(Rule.org_id == org_id, Rule.name == r["name"])
            )).scalar_one_or_none()
            if not exists:
                self._db.add(Rule(
                    org_id=org_id, name=r["name"], display_name=r["display_name"],
                    description=r["description"], rule_type=r["rule_type"],
                    dimension=r["dimension"], severity=r["severity"],
                    parameters=r["parameters"], tags=[], is_system_rule=True, version=1,
                ))
                created += 1
        await self._db.flush()
        return created

    async def create(self, principal: Principal, req: RuleCreate) -> Rule:
        org_id = principal.org_id
        settings = get_settings()
        count = (await self._db.execute(
            select(sqlfunc.count()).select_from(Rule).where(Rule.org_id == org_id))).scalar_one()
        if count >= settings.max_rules_per_org:
            raise validation_failed("name", f"Max {settings.max_rules_per_org} rules per org")
        if (await self._db.execute(
                select(Rule).where(Rule.org_id == org_id, Rule.name == req.name))).scalar_one_or_none():
            raise conflict(f"A rule named '{req.name}' already exists in this org")
        params = _validate(req.rule_type, req.parameters)
        rule = Rule(
            org_id=org_id, name=req.name, display_name=req.display_name or req.name,
            description=req.description, rule_type=req.rule_type, dimension=req.dimension,
            severity=req.severity, parameters=params, tags=req.tags, is_system_rule=False,
            created_by=principal.user_id, version=1,
        )
        if req.rule_type in PRIVILEGED_RULE_TYPES:
            _require_privilege(principal, req.acknowledged_privileged, req.rule_type)
            _approve(rule, principal)
        self._db.add(rule)
        await self._db.flush()
        await self._db.refresh(rule)
        await audit.record(self._db, org_id=org_id, actor=principal.user_id, resource_type="rule",
                           resource_id=rule.id, operation="create", resource_version=rule.version,
                           object_name=rule.name,
                           detail={"rule_type": rule.rule_type, "privileged": rule.privileged,
                                   "review_status": rule.review_status})
        return rule

    async def list_for_org(self, org_id: str, *, dimension: str | None = None,
                           rule_type: str | None = None, tag: str | None = None) -> list[Rule]:
        stmt = select(Rule).where(Rule.org_id == org_id)
        if dimension:
            stmt = stmt.where(Rule.dimension == dimension)
        if rule_type:
            stmt = stmt.where(Rule.rule_type == rule_type)
        rules = list((await self._db.execute(
            stmt.order_by(Rule.is_system_rule.desc(), Rule.name))).scalars())
        if tag:
            rules = [r for r in rules if tag in (r.tags or [])]
        return rules

    async def get(self, org_id: str, rule_id: uuid.UUID) -> Rule:
        rule = await self._db.get(Rule, rule_id)
        if not rule or rule.org_id != org_id:
            raise not_found("rule", str(rule_id))
        return rule

    async def update(self, principal: Principal, rule_id: uuid.UUID, req: RuleUpdate) -> Rule:
        rule = await self.get(principal.org_id, rule_id)
        if rule.is_system_rule:
            raise system_rule_immutable()
        bump = False
        if req.display_name is not None:
            rule.display_name = req.display_name
        if req.description is not None:
            rule.description = req.description
        if req.tags is not None:
            rule.tags = req.tags
        if req.severity is not None and req.severity != rule.severity:
            rule.severity = req.severity
            bump = True
        if req.parameters is not None:
            params = _validate(rule.rule_type, req.parameters)
            if params != rule.parameters:
                if rule.rule_type in PRIVILEGED_RULE_TYPES:
                    _require_privilege(principal, req.acknowledged_privileged, rule.rule_type)
                    _approve(rule, principal)
                rule.parameters = params
                bump = True
        if bump:
            rule.version = (rule.version or 1) + 1
        await self._db.flush()
        await self._db.refresh(rule)
        await audit.record(self._db, org_id=principal.org_id, actor=principal.user_id,
                           resource_type="rule", resource_id=rule.id, operation="update",
                           resource_version=rule.version, object_name=rule.name,
                           detail={"version_bumped": bump})
        return rule

    async def approve(self, principal: Principal, rule_id: uuid.UUID, acknowledged: bool) -> Rule:
        rule = await self.get(principal.org_id, rule_id)
        if rule.rule_type not in PRIVILEGED_RULE_TYPES:
            raise validation_failed("rule_type", "only privileged (custom_sql) rules need approval")
        _require_privilege(principal, acknowledged, rule.rule_type)
        _validate(rule.rule_type, rule.parameters)
        _approve(rule, principal)
        rule.version = (rule.version or 1) + 1
        await self._db.flush()
        await self._db.refresh(rule)
        await audit.record(self._db, org_id=principal.org_id, actor=principal.user_id,
                           resource_type="rule", resource_id=rule.id, operation="approve",
                           resource_version=rule.version, object_name=rule.name)
        return rule

    async def delete(self, principal: Principal, rule_id: uuid.UUID) -> None:
        rule = await self.get(principal.org_id, rule_id)
        if rule.is_system_rule:
            raise system_rule_immutable()
        if rule.rule_type in PRIVILEGED_RULE_TYPES:
            ensure_permission(principal, "dq.custom_sql.write")
        await audit.record(self._db, org_id=principal.org_id, actor=principal.user_id,
                           resource_type="rule", resource_id=rule.id, operation="delete",
                           resource_version=rule.version, object_name=rule.name)
        await self._db.delete(rule)
