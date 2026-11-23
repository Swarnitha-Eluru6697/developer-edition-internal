"""Compile a binding into an IMMUTABLE ArtifactVersion (DQ-ART-004).

compile():
  1. load binding + ruleset (version) + items (rule ids/versions/params)
  2. (Flink) take the source table from the runner's StructuredJobPlan
  3. build `hash_inputs` — every input the output can depend on — and its
     sha256 `content_hash`
  4. if an ArtifactVersion with that (binding, hash) exists, return it —
     identical inputs => identical artifact, nothing is ever re-written
  5. otherwise render, structurally validate, and INSERT a new row
     (UPDATE is blocked by a DB trigger; (binding, hash) is unique)

There is no `force` and no free-text DDL override any more: request-
specific renders take a small set of TYPED overrides, become kind
'preview' (kind + overrides are hashed), and apply only ever uses kind
'standard' — a preview can neither overwrite nor be served as a standard
artifact.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import quality_contract as qc
from app.core.config import get_settings
from app.core.errors import DQServiceError, not_found, validation_failed
from app.models import ArtifactVersion, JobBinding, Ruleset, RulesetItem
from app.renderers.capability import classify_items
from app.renderers.compat import (
    HASH_VERSION, RENDERER_VERSION, SUPPORTED_ENGINES, check_engine_compatibility,
)
from app.renderers.dbt import DbtCompileError, dbt_model_name, render_dbt
from app.renderers.flink import (
    DEFAULT_WINDOW_MINUTES, MAX_SAMPLE_SLOTS, FlinkCompileError, flink_sinks, render_flink,
)
from app.renderers.plan import PlanError, StructuredJobPlan, select_quality_source
from app.renderers.python import ON_FAILURE, render_python
from app.renderers.security import RenderSecurityError
from app.renderers.validation import (
    validate_dbt_artifact, validate_flink_artifact, validate_python_artifacts,
)
from app.services.ruleset_service import item_dict

OVERRIDE_SPEC: dict[str, tuple[int, int]] = {
    "window_minutes": (1, 1440),
    "sample_limit": (0, MAX_SAMPLE_SLOTS),
}


def validate_overrides(job_type: str, overrides: dict[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in (overrides or {}).items():
        if v is None:
            continue
        if k not in OVERRIDE_SPEC:
            raise validation_failed(f"overrides.{k}", f"unknown override {k!r}")
        if job_type != "flink_job":
            raise validation_failed(f"overrides.{k}", "overrides only apply to flink_job bindings")
        lo, hi = OVERRIDE_SPEC[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise validation_failed(f"overrides.{k}", f"must be an integer in [{lo}, {hi}]")
        out[k] = v
    return out


def canonical_hash(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
                          .encode()).hexdigest()


def contract_topics() -> dict[str, str]:
    s = get_settings()
    return {"results": s.quality_results_topic, "gate": s.quality_gate_topic,
            "quarantine": s.quality_quarantine_topic}


def engine_version_for(job_type: str, plan: StructuredJobPlan | None) -> str | None:
    s = get_settings()
    if job_type == "dbt_project":
        return s.dbtrunner_engine_version
    if job_type == "flink_job":
        return plan.engine_version if plan else None
    return s.python_engine_version


class RenderService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def binding(self, org_id: str, binding_id: uuid.UUID) -> JobBinding:
        b = (await self._db.execute(
            select(JobBinding).options(selectinload(JobBinding.ruleset))
            .where(JobBinding.org_id == org_id, JobBinding.id == binding_id))).scalar_one_or_none()
        if not b:
            raise not_found("binding", str(binding_id))
        return b

    async def items(self, ruleset_id: uuid.UUID) -> list[dict]:
        rows = (await self._db.execute(
            select(RulesetItem).options(selectinload(RulesetItem.rule))
            .where(RulesetItem.ruleset_id == ruleset_id)
            .order_by(RulesetItem.sort_order, RulesetItem.id)
            .execution_options(populate_existing=True))).scalars().all()
        return [item_dict(i, i.rule) for i in rows]

    async def compile(
        self, org_id: str, binding_id: uuid.UUID, *, plan: StructuredJobPlan | None = None,
        overrides: dict[str, Any] | None = None, actor: str | None = None,
    ) -> tuple[ArtifactVersion, dict[str, Any]]:
        b = await self.binding(org_id, binding_id)
        jt = b.job_type
        ruleset: Ruleset = b.ruleset
        overrides = validate_overrides(jt, overrides)
        kind = "preview" if overrides else "standard"
        items = await self.items(b.ruleset_id)
        dialect = ((b.resolution or {}).get("job") or {}).get("engine") or "trino"

        source = None
        diagnostics: list[dict] = []
        if jt == "flink_job":
            if plan is None:
                raise DQServiceError(
                    409, "STRUCTURED_PLAN_REQUIRED",
                    "A FlinkSQL quality job is compiled from the parent job's structured plan, "
                    "which the FlinkSQL runner provides; call this with a user bearer so the "
                    "parent job can be fetched and validated.")
            try:
                source, diagnostics = select_quality_source(plan, b.target_table)
            except PlanError as e:
                raise DQServiceError(422, "PLAN_UNUSABLE", str(e))

        capability = classify_items(items, jt, b.enforcement_mode, source=source,
                                    flink_fail_stop=b.flink_fail_stop)
        engine_version = engine_version_for(jt, plan)
        topics = contract_topics() if jt == "flink_job" else None
        hash_inputs = {
            "hash_version": HASH_VERSION,
            "kind": kind,
            "renderer_version": RENDERER_VERSION,
            "engine": {"job_type": jt, "engine_version": engine_version,
                       "supported": SUPPORTED_ENGINES[jt]},
            "contract": ({"name": qc.CONTRACT_NAME, "version": qc.CONTRACT_VERSION,
                          "topics": topics} if topics else None),
            "ruleset": {"id": str(ruleset.id), "version": int(ruleset.version or 1)},
            "rules": [{k: it[k] for k in (
                "item_id", "rule_id", "rule_version", "rule_type", "rule_name", "severity",
                "dimension", "column_name", "enabled", "sort_order", "parameters",
                "parameter_overrides", "review_status")} for it in items],
            "binding": {"id": str(b.id), "org_id": b.org_id, "version": int(b.version or 1),
                        "job_type": jt, "job_id": str(b.job_id), "target_table": b.target_table,
                        "ucs_fqn": b.ucs_fqn, "enforcement_mode": b.enforcement_mode,
                        "flink_fail_stop": bool(b.flink_fail_stop), "dialect": dialect},
            "source": ({"plan_version": plan.plan_version, "plan_hash": plan.plan_hash(),
                        "normalized_sql_hash": plan.normalized_sql_hash,
                        "source_table": source.name} if plan is not None and source else None),
            "overrides": overrides,
        }
        content_hash = canonical_hash(hash_inputs)

        existing = await self._find(binding_id, content_hash)
        if existing is not None:
            return existing, capability

        version_id = uuid.uuid4()
        ctx = {"org_id": b.org_id, "ucs_fqn": b.ucs_fqn, "binding_id": str(b.id),
               "artifact_version_id": str(version_id), "enforcement_mode": b.enforcement_mode,
               "flink_fail_stop": bool(b.flink_fail_stop), "parent_job_id": str(b.job_id),
               "job_name": f"dq-quality-{b.id}"}
        dbt_art = flink_art = soda_art = None
        gx_art = None
        validation_errors: list[str] = []
        target_model = b.target_table
        try:
            if jt == "dbt_project":
                target_model = dbt_model_name(b.target_table)
                dbt_art, d = render_dbt(items, target_table=b.target_table,
                                        enforcement_mode=b.enforcement_mode, ctx=ctx,
                                        dialect=dialect)
                diagnostics += d
                if dbt_art:
                    validation_errors = validate_dbt_artifact(
                        dbt_art, model_name=target_model, enforcement_mode=b.enforcement_mode)
            elif jt == "flink_job":
                target_model = source.name
                flink_art, d = render_flink(
                    items, source=source, ctx=ctx, topics=topics,
                    window_minutes=overrides.get("window_minutes", DEFAULT_WINDOW_MINUTES),
                    sample_slots=overrides.get("sample_limit", MAX_SAMPLE_SLOTS))
                diagnostics += d
                if flink_art:
                    validation_errors = validate_flink_artifact(
                        flink_art, sinks=flink_sinks(b.enforcement_mode), topics=topics)
            else:
                gx_art, soda_art, d = render_python(items, target_table=b.target_table,
                                                    enforcement_mode=b.enforcement_mode, ctx=ctx)
                diagnostics += d
                validation_errors = validate_python_artifacts(gx_art, soda_art)
        except (RenderSecurityError, DbtCompileError, FlinkCompileError) as e:
            raise validation_failed("binding", str(e))

        diagnostics += [{"stage": "validate", "severity": "error", "target": jt, "message": m}
                        for m in validation_errors]
        compat = check_engine_compatibility(jt, engine_version)
        if compat:
            diagnostics.append({"stage": "compatibility", "severity": "error", "target": jt,
                                "message": compat["error"], "detail": compat})
        if not (dbt_art or flink_art or gx_art or soda_art):
            status = "empty"
        elif validation_errors:
            status = "invalid"
        else:
            status = "valid"

        version = ArtifactVersion(
            id=version_id, binding_id=b.id, content_hash=content_hash, kind=kind,
            hash_version=HASH_VERSION, hash_inputs=hash_inputs,
            renderer_version=RENDERER_VERSION, engine_version=engine_version or "unknown",
            supported_engine_versions=SUPPORTED_ENGINES[jt],
            ruleset_id=ruleset.id, ruleset_version=int(ruleset.version or 1),
            binding_version=int(b.version or 1),
            rule_versions={it["rule_id"]: it["rule_version"] for it in items},
            target_model=target_model,
            source_plan_hash=hash_inputs["source"]["plan_hash"] if hash_inputs["source"] else None,
            normalized_sql_hash=(hash_inputs["source"]["normalized_sql_hash"]
                                 if hash_inputs["source"] else None),
            contract_version=qc.CONTRACT_VERSION if jt == "flink_job" else None,
            overrides=overrides or None, capability=capability,
            dbt_artifact=dbt_art, flinksql_artifact=flink_art, gx_artifact=gx_art,
            soda_artifact=soda_art, validation_diagnostics=diagnostics,
            validation_status=status, created_by=actor,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(version)
                await self._db.flush()
        except IntegrityError:
            # A concurrent compile of identical inputs won the insert.
            existing = await self._find(binding_id, content_hash)
            if existing is None:
                raise
            return existing, capability
        await self._db.refresh(version)
        return version, capability

    async def _find(self, binding_id: uuid.UUID, content_hash: str) -> ArtifactVersion | None:
        return (await self._db.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.binding_id == binding_id,
                ArtifactVersion.content_hash == content_hash,
                ArtifactVersion.hash_version.is_not(None)))).scalar_one_or_none()

    async def list_versions(self, org_id: str, binding_id: uuid.UUID) -> list[ArtifactVersion]:
        await self.binding(org_id, binding_id)
        return list((await self._db.execute(
            select(ArtifactVersion).where(ArtifactVersion.binding_id == binding_id)
            .order_by(ArtifactVersion.rendered_at.desc()))).scalars())

    async def get_version(self, org_id: str, binding_id: uuid.UUID,
                          version_id: uuid.UUID) -> ArtifactVersion:
        await self.binding(org_id, binding_id)
        v = (await self._db.execute(select(ArtifactVersion).where(
            ArtifactVersion.binding_id == binding_id, ArtifactVersion.id == version_id))
             ).scalar_one_or_none()
        if not v:
            raise not_found("artifact_version", str(version_id))
        return v


def to_render_out(b: JobBinding, v: ArtifactVersion, capability: dict | None = None) -> dict:
    ops_pins = get_settings().supported_engine_versions
    compat = (check_engine_compatibility(b.job_type, v.engine_version, ops_pins)
              if v.hash_version else {"reason": "legacy_artifact",
                                      "error": "legacy artifact (pre-0003); never applied"})
    return {
        "binding_id": str(b.id), "version_id": str(v.id), "kind": v.kind,
        "job_type": b.job_type, "target_table": b.target_table, "target_model": v.target_model,
        "ucs_fqn": b.ucs_fqn, "enforcement_mode": b.enforcement_mode,
        "on_failure": ON_FAILURE.get(b.enforcement_mode) if b.job_type == "python_job" else None,
        "content_hash": v.content_hash, "hash_version": v.hash_version,
        "renderer_version": v.renderer_version, "engine_version": v.engine_version,
        "supported_engine_versions": v.supported_engine_versions or {},
        "compatibility": {"compatible": compat is None, "diagnostic": compat},
        "contract_version": v.contract_version,
        "ruleset_id": str(v.ruleset_id) if v.ruleset_id else None,
        "ruleset_version": v.ruleset_version, "binding_version": v.binding_version,
        "rule_versions": v.rule_versions or {},
        "source_plan_hash": v.source_plan_hash, "normalized_sql_hash": v.normalized_sql_hash,
        "overrides": v.overrides or {},
        "validation_status": v.validation_status,
        "validation_diagnostics": v.validation_diagnostics or [],
        "capability": capability or v.capability or {"supported": [], "unsupported": [],
                                                      "degraded": []},
        "rendered_at": v.rendered_at.isoformat() if v.rendered_at else None,
        "dbt_artifact": v.dbt_artifact, "flinksql_artifact": v.flinksql_artifact,
        "gx_artifact": v.gx_artifact, "soda_artifact": v.soda_artifact,
    }
