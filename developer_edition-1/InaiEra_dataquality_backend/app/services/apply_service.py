"""Apply a binding's compiled, immutable ArtifactVersion to its runner.

Gates (each refusal -> HTTP 422 APPLY_REFUSED with an actionable body, and
an ArtifactApplication row recording it):
  1. binding resolved (DQ-BIND-003)
  2. Flink: parent job fetched (caller bearer) and validated by the runner
     (scoped JWT) -> StructuredJobPlan; the runner's quality contract must
     match DQ's (DQ-CONTRACT-005)
  3. compile -> kind 'standard' ArtifactVersion; validation_status 'valid'
  4. DQ-CAP-007: no unsupported rule in a blocking binding
  5. DQ-014: target engine version within the artifact's declared range
     (and ops pins)
  6. Flink: the compiled quality SQL itself validates on the runner
Then push (idempotency key "dq-apply:<binding>:<artifact>"):
  dbt     POST schema-tests with the ApplyDqArtifact body + scoped JWT
  flink   create/update the quality job under the caller's bearer
  python  nothing to push (pull model) — reported as not_applicable
On success: application row, audit event, UCS check registrations (outbox).
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import quality_contract as qc
from app.core.auth import Principal
from app.core.config import get_settings
from app.core.errors import DQServiceError
from app.integrations.common import UpstreamError
from app.integrations.dbt_runner import DbtRunnerClient
from app.integrations.flink_runner import FlinkRunnerClient
from app.models import ArtifactApplication, ArtifactVersion, JobBinding
from app.renderers.compat import RENDERER_VERSION, SUPPORTED_ENGINES, check_engine_compatibility
from app.renderers.flink import flink_sinks
from app.renderers.plan import PlanError, StructuredJobPlan
from app.services import audit
from app.services.render_service import RenderService, contract_topics
from app.services.ucs_sync_service import enqueue_registrations

log = structlog.get_logger()


class ApplyRefused(Exception):
    def __init__(self, reason: str, message: str, **detail: Any):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


async def fetch_parent_plan(binding: JobBinding, bearer: str | None) -> StructuredJobPlan:
    """Parent job SQL (caller bearer) -> runner validate (scoped JWT) -> plan."""
    if not bearer:
        raise DQServiceError(401, "BEARER_REQUIRED",
                             "the caller's bearer is required to read the parent FlinkSQL job")
    client = FlinkRunnerClient()
    try:
        job = await client.get_job(binding.job_id, bearer)
        if job.get("org_id") != binding.org_id:
            raise DQServiceError(403, "FORBIDDEN", "parent job belongs to another organization")
        sql = job.get("sql") or ""
        if not sql:
            raise DQServiceError(422, "PLAN_UNUSABLE", "the parent FlinkSQL job has no SQL")
        valid, errors, plan = await client.validate(sql, org_id=binding.org_id)
    except UpstreamError as e:
        raise e.to_http()
    except PlanError as e:
        raise DQServiceError(502, "PLAN_UNAVAILABLE", str(e))
    if not valid:
        raise DQServiceError(422, "PARENT_JOB_INVALID",
                             "the FlinkSQL runner could not validate the parent job",
                             {"errors": errors[:20]})
    return plan


def check_runner_contract(plan: StructuredJobPlan) -> None:
    rc = plan.quality_contract
    if rc is None or not qc.is_compatible_version(rc.version):
        raise ApplyRefused(
            "quality_contract_mismatch",
            f"the FlinkSQL runner implements quality contract "
            f"{rc.version if rc else 'none'!s}, DQ renders {qc.CONTRACT_VERSION}",
            runner_contract=rc.model_dump() if rc else None,
            dq_contract={"version": qc.CONTRACT_VERSION, "topics": contract_topics()},
            remediation="Deploy matching DQ and FlinkSQL runner releases (shared "
                        "app/contracts/quality_contract.json).")
    if rc.topics and rc.topics != contract_topics():
        raise ApplyRefused(
            "quality_topics_mismatch",
            "the FlinkSQL runner consumes different quality topics than DQ would write",
            runner_topics=rc.topics, dq_topics=contract_topics(),
            remediation="Set the same QUALITY_*_TOPIC values on both services.")


class ApplyService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def apply(self, principal: Principal, binding_id: uuid.UUID, *,
                    bearer: str | None) -> tuple[int, dict]:
        renderer = RenderService(self._db)
        b = await renderer.binding(principal.org_id, binding_id)
        version: ArtifactVersion | None = None
        try:
            if b.resolved_at is None:
                raise ApplyRefused(
                    "binding_unresolved",
                    "this binding was never resolved against its runner and UCS",
                    remediation=f"POST /api/v1/bindings/{b.id}/resolve first.")
            plan = None
            if b.job_type == "flink_job":
                plan = await fetch_parent_plan(b, bearer)
                check_runner_contract(plan)
            version, capability = await renderer.compile(principal.org_id, b.id, plan=plan,
                                                         actor=principal.user_id)
            if version.validation_status != "valid":
                raise ApplyRefused(
                    f"artifact_{version.validation_status}",
                    f"the compiled artifact is {version.validation_status}; nothing to apply"
                    if version.validation_status == "empty" else
                    "the compiled artifact failed structural validation",
                    diagnostics=version.validation_diagnostics)
            if b.enforcement_mode == "blocking" and capability["unsupported"]:
                names = ", ".join(f"{u['rule_name']} ({u['column_name'] or 'table-level'})"
                                  for u in capability["unsupported"])
                raise ApplyRefused(
                    "unsupported_blocking_rules",
                    f"{len(capability['unsupported'])} blocking rule(s) cannot be enforced by "
                    f"{b.job_type}: {names}",
                    unsupported_rules=capability["unsupported"],
                    remediation="Remove them, bind them to an engine that supports them, or "
                                "use enforcement_mode warn/async.")
            compat = check_engine_compatibility(b.job_type, version.engine_version,
                                                get_settings().supported_engine_versions)
            if compat:
                raise ApplyRefused(compat["reason"], compat["error"],
                                   **{k: v for k, v in compat.items()
                                      if k not in ("reason", "error")})
            if b.job_type == "flink_job":
                await self._validate_compiled_flink(b, version)

            key = f"dq-apply:{b.id}:{version.id}"
            prior = (await self._db.execute(select(ArtifactApplication).where(
                ArtifactApplication.idempotency_key == key))).scalar_one_or_none()
            if prior is not None and prior.status == "applied":
                return 200, {"applied": True, "idempotent_replay": True, "target": b.job_type,
                             "artifact_version_id": str(version.id),
                             "content_hash": version.content_hash, **(prior.detail or {})}

            if b.job_type == "dbt_project":
                result = await self._push_dbt(b, version, key)
            elif b.job_type == "flink_job":
                result = await self._push_flink(b, version, bearer)
            else:
                result = {"applied": False, "status": "not_applicable",
                          "message": "Python jobs pull their GX/Soda artifacts — fetch "
                                     f"GET /api/v1/render/{b.id}/versions/{version.id}."}
        except ApplyRefused as e:
            await self._record(b, version, "refused", {"reason": e.reason, **_jsonable(e.detail)},
                               principal, key=None)
            return 422, {"applied": False, "target": b.job_type, "reason": e.reason,
                         "error": e.message,
                         "artifact_version_id": str(version.id) if version else None,
                         **_jsonable(e.detail)}

        status = "applied" if result.get("applied") else result.get("status", "failed")
        await self._record(b, version, status, result, principal, key=key if status == "applied" else None)
        if not result.get("applied"):
            code = 200 if status == "not_applicable" else 502
            return code, {"target": b.job_type, "artifact_version_id": str(version.id), **result}

        await audit.record(
            self._db, org_id=b.org_id, actor=principal.user_id, resource_type="artifact",
            resource_id=version.id, operation="apply", resource_version=version.content_hash,
            object_name=b.ucs_fqn,
            detail={"binding_id": str(b.id), "binding_version": version.binding_version,
                    "job_type": b.job_type, "job_id": str(b.job_id),
                    "ruleset_id": str(version.ruleset_id), "ruleset_version": version.ruleset_version,
                    "rule_versions": version.rule_versions,
                    "renderer_version": version.renderer_version,
                    "engine_version": version.engine_version,
                    "source_plan_hash": version.source_plan_hash,
                    "idempotency_key": key})
        await enqueue_registrations(self._db, b, version)
        return 200, {"applied": True, "target": b.job_type, "artifact_version_id": str(version.id),
                     "content_hash": version.content_hash, **result}

    async def _record(self, b: JobBinding, version: ArtifactVersion | None, status: str,
                      detail: dict, principal: Principal, *, key: str | None) -> None:
        if version is None:
            return
        self._db.add(ArtifactApplication(
            org_id=b.org_id, binding_id=b.id, artifact_version_id=version.id, target=b.job_type,
            idempotency_key=key or f"attempt:{uuid.uuid4()}", status=status,
            detail=_jsonable(detail), actor=principal.user_id))
        await self._db.flush()

    async def _validate_compiled_flink(self, b: JobBinding, version: ArtifactVersion) -> None:
        try:
            valid, errors, plan = await FlinkRunnerClient().validate(version.flinksql_artifact,
                                                                     org_id=b.org_id)
        except (UpstreamError, PlanError) as e:
            raise ApplyRefused("runner_validation_unavailable",
                               f"the FlinkSQL runner could not validate the quality job: {e}")
        expected = set(flink_sinks(b.enforcement_mode))
        written = {t.name for t in plan.sinks}
        if not valid or written != expected:
            raise ApplyRefused("compiled_artifact_rejected",
                               "the FlinkSQL runner rejected the compiled quality job",
                               errors=errors[:20], sinks=sorted(written),
                               expected_sinks=sorted(expected))

    async def _push_dbt(self, b: JobBinding, version: ArtifactVersion, key: str) -> dict:
        artifact = {
            "artifact_version_id": str(version.id), "binding_id": str(b.id),
            "project_id": str(b.job_id), "org_id": b.org_id,
            "target_model": version.target_model, "dbt_schema_fragment": version.dbt_artifact,
            "idempotency_key": key, "content_hash": version.content_hash,
            "renderer_version": RENDERER_VERSION,
            "engine_requirements": SUPPORTED_ENGINES["dbt_project"],
        }
        try:
            r = await DbtRunnerClient().apply_schema_tests(org_id=b.org_id, project_id=b.job_id,
                                                           artifact=artifact, idempotency_key=key)
        except UpstreamError as e:
            return {"applied": False, "status": "failed", "error": str(e)}
        if 200 <= r.status_code < 300:
            log.info("dq_apply.dbt", binding=str(b.id), artifact=str(version.id))
            return {"applied": True, "project_id": str(b.job_id),
                    "target_model": version.target_model,
                    "message": "Tests stored on the project; the runner merges them into the "
                               "next dbt build (async tests run as a separate dbt test job)."}
        body: Any
        try:
            body = r.json()
        except ValueError:
            body = r.text[:300]
        detail = body.get("detail") if isinstance(body, dict) else None
        if r.status_code == 422 and isinstance(detail, dict) \
                and detail.get("code") == "ENGINE_INCOMPATIBLE":
            return {"applied": False, "status": "refused", "reason": "engine_version_incompatible",
                    "error": "the dbt runner reports its dbt-core version is outside the "
                             "artifact's declared range", "runner": body}
        return {"applied": False, "status": "failed",
                "error": f"dbt runner returned HTTP {r.status_code}", "runner": body}

    async def _push_flink(self, b: JobBinding, version: ArtifactVersion, bearer: str | None) -> dict:
        if not bearer:
            return {"applied": False, "status": "failed",
                    "error": "the caller's bearer is required to create the quality job"}
        name = f"dq-quality-{b.id}"
        payload = {"name": name,
                   "description": f"DQ quality checks for {b.ucs_fqn} (artifact {version.id})",
                   "sql": version.flinksql_artifact, "job_kind": "quality", "ucs_fqn": b.ucs_fqn}
        try:
            body, created = await FlinkRunnerClient().upsert_quality_job(
                bearer=bearer, name=name, payload=payload)
        except UpstreamError as e:
            return {"applied": False, "status": "failed", "error": str(e)}
        return {"applied": True, "job_id": body.get("id"), "job_name": name, "created": created,
                "message": f"Quality job {'created' if created else 'updated'}; deploy it from "
                           "FlinkSQL → Jobs. Results flow through the runner's quality consumer."}


def _jsonable(d: Any) -> Any:
    import json
    return json.loads(json.dumps(d, default=str))

