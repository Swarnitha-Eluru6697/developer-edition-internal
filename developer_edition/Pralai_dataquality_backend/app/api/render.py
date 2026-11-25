"""Render API — compiled, immutable ArtifactVersions per binding."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

import yaml
from fastapi import APIRouter, Header

from app.core.deps import DB, Reader, ValidateCaller, bearer_from_header
from app.renderers.capability import is_renderable
from app.renderers.dbt import _test_for, test_config
from app.renderers.flink import violation_condition
from app.renderers.params import ParameterError, validate_parameters
from app.renderers.plan import PlanColumn, PlanTable
from app.renderers.security import RenderSecurityError, validate_identifier
from app.schemas import RenderOut, RenderOverrides, ValidateRuleRequest, ValidateRuleResponse
from app.services.apply_service import fetch_parent_plan
from app.services.render_service import RenderService, to_render_out

router = APIRouter(prefix="/api/v1/render", tags=["render"])


async def _compile(db, user, binding_id: UUID, authorization: str | None, overrides=None):
    svc = RenderService(db)
    b = await svc.binding(user.org_id, binding_id)
    plan = None
    if b.job_type == "flink_job":
        plan = await fetch_parent_plan(b, bearer_from_header(authorization))
    v, cap = await svc.compile(user.org_id, binding_id, plan=plan, overrides=overrides,
                               actor=user.user_id)
    return RenderOut(**to_render_out(b, v, cap))


@router.get("/{binding_id}", response_model=RenderOut)
async def get_render(binding_id: UUID, user: Reader, db: DB,
                     authorization: Annotated[str | None, Header()] = None) -> RenderOut:
    """The binding's STANDARD artifact for its current inputs (reused if an
    identical-input version exists, otherwise compiled and stored)."""
    return await _compile(db, user, binding_id, authorization)


@router.post("/{binding_id}/refresh", response_model=RenderOut)
async def refresh_render(binding_id: UUID, user: Reader, db: DB,
                         authorization: Annotated[str | None, Header()] = None) -> RenderOut:
    """Kept for the Workspace proxy: recompiles from CURRENT inputs. Content-
    addressed, so an unchanged input set returns the same immutable version
    (there is no 'force' that could overwrite or duplicate an artifact)."""
    return await _compile(db, user, binding_id, authorization)


@router.post("/{binding_id}/preview", response_model=RenderOut)
async def preview_render(binding_id: UUID, overrides: RenderOverrides, user: Reader, db: DB,
                         authorization: Annotated[str | None, Header()] = None) -> RenderOut:
    """Compile with typed request-specific overrides -> kind 'preview' artifact
    (hashed separately; never served as or applied instead of a standard one)."""
    return await _compile(db, user, binding_id, authorization,
                          overrides=overrides.model_dump(exclude_none=True))


@router.get("/{binding_id}/versions")
async def list_versions(binding_id: UUID, user: Reader, db: DB) -> list[dict]:
    versions = await RenderService(db).list_versions(user.org_id, binding_id)
    return [{"version_id": str(v.id), "kind": v.kind, "content_hash": v.content_hash,
             "renderer_version": v.renderer_version, "engine_version": v.engine_version,
             "ruleset_version": v.ruleset_version, "validation_status": v.validation_status,
             "rendered_at": v.rendered_at.isoformat() if v.rendered_at else None}
            for v in versions]


@router.get("/{binding_id}/versions/{version_id}", response_model=RenderOut)
async def get_version(binding_id: UUID, version_id: UUID, user: Reader, db: DB) -> RenderOut:
    svc = RenderService(db)
    v = await svc.get_version(user.org_id, binding_id, version_id)
    return RenderOut(**to_render_out(await svc.binding(user.org_id, binding_id), v))


# ── Internal validate (Workspace codegen; scoped service JWT) ─────────────

_PREVIEW_TYPES = {"range": "DOUBLE", "freshness": "TIMESTAMP(3)"}


@router.post("/validate/rule", response_model=ValidateRuleResponse)
async def validate_rule(_: ValidateCaller, payload: ValidateRuleRequest) -> ValidateRuleResponse:
    errors: list[str] = []
    dbt_yaml = flink_sql = None
    col = payload.column_name or ""
    try:
        if col:
            validate_identifier(col, field="column_name")
        params = validate_parameters(payload.rule_type, payload.parameters, strict=True)
    except (ParameterError, RenderSecurityError) as e:
        return ValidateRuleResponse(valid=False, errors=[str(e)])
    item = {"item_id": "preview", "rule_id": "preview", "rule_name": f"preview_{payload.rule_type}",
            "rule_type": payload.rule_type, "rule_version": 1, "dimension": payload.dimension,
            "severity": payload.severity, "parameters": params, "parameter_overrides": {},
            "column_name": col, "enabled": True,
            "review_status": "approved" if payload.rule_type == "custom_sql" else None}
    if reason := is_renderable("dbt_project", item):
        errors.append(f"dbt: {reason}")
    else:
        try:
            _, test = _test_for(item, params, test_config("warn"), {"dq_rule": item["rule_name"]},
                                payload.target_engine)
            dbt_yaml = yaml.safe_dump(test, sort_keys=False)
        except RenderSecurityError as e:
            errors.append(f"dbt: {e}")
    source = PlanTable(name="preview", columns=[PlanColumn(
        name=col or "c", type=_PREVIEW_TYPES.get(payload.rule_type, "STRING"))])
    if reason := is_renderable("flink_job", item, source):
        errors.append(f"flinksql: {reason}")
    else:
        try:
            flink_sql = f"WHERE {violation_condition(item, params, source)}"
        except RenderSecurityError as e:
            errors.append(f"flinksql: {e}")
    # Valid when the definition is well-typed and at least one engine can run it;
    # per-engine limitations are reported in `errors`.
    return ValidateRuleResponse(valid=bool(dbt_yaml or flink_sql), errors=errors, rendered_dbt_yaml=dbt_yaml,
                                rendered_flinksql=flink_sql)
