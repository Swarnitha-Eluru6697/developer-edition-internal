"""Job bindings (DQ-BIND-003).

A binding is persisted only after:
  * the caller holds DQ permission dq.bind (API dependency);
  * the JOB resolves through its runner using the caller's own bearer (the
    runner applies its org scoping + RBAC to the caller), belongs to the
    caller's org, and is the right kind (a dbt project for dbt_project; a
    *transform* FlinkSQL job for flink_job — binding to a quality job or to
    an id of the wrong type is rejected);
  * the DATASET resolves through UCS to a canonical FQN of a dataset-like
    object in the caller's org, and UCS authorises the configured bind
    action (default "dq.bind") on it for the caller.
The canonical ids/FQN/names returned by those systems are what is stored.
python_job has no runner registry: its job id cannot be resolved, which is
recorded explicitly in `resolution`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func as sqlfunc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.errors import DQServiceError, conflict, forbidden, not_found, validation_failed
from app.integrations.common import UpstreamError, UpstreamForbidden, UpstreamNotFound
from app.integrations.dbt_runner import DbtRunnerClient
from app.integrations.flink_runner import FlinkRunnerClient
from app.integrations.ucs import UcsClient, UcsContractUnavailable
from app.models import JobBinding, Ruleset
from app.renderers.security import RenderSecurityError, validate_fqn, validate_identifier
from app.schemas import JobBindingCreate
from app.services import audit

log = structlog.get_logger()

DATASET_OBJECT_TYPES = {"table", "view", "materialized_view", "stream", "topic", "dataset"}


def _upstream(e: UpstreamError, what: str, ident: str) -> DQServiceError:
    if isinstance(e, UpstreamNotFound):
        return not_found(what, ident)
    if isinstance(e, UpstreamForbidden):
        return forbidden(f"{e.service} denied access to {what} {ident}")
    return e.to_http()


async def resolve_job(principal: Principal, job_type: str, job_id: uuid.UUID,
                      bearer: str | None) -> dict[str, Any]:
    settings = get_settings()
    if job_type == "python_job":
        return {"verified": False, "kind": "python_job",
                "reason": "no Python job registry exists to resolve the job id against"}
    enabled = settings.dbtrunner_enabled if job_type == "dbt_project" else settings.flinksql_enabled
    if not enabled:
        raise validation_failed("job_type", f"the {job_type} integration is disabled on this "
                                            "deployment; cannot bind to it")
    if not bearer:
        raise DQServiceError(401, "BEARER_REQUIRED",
                             "the caller's bearer is required to resolve the job via its runner")
    try:
        if job_type == "dbt_project":
            job = await DbtRunnerClient(settings).get_project(job_id, bearer)
            kind, name = "dbt_project", job.get("name")
            extra = {"engine": job.get("engine"), "git_ref": job.get("git_ref")}
        else:
            job = await FlinkRunnerClient(settings).get_job(job_id, bearer)
            kind, name = job.get("job_kind") or "transform", job.get("name")
            extra = {"sink_engine": job.get("sink_engine")}
            if kind != "transform":
                raise DQServiceError(422, "JOB_KIND_MISMATCH",
                                     f"job {job_id} is a {kind!r} FlinkSQL job; quality rules can "
                                     "only be bound to transform jobs")
    except UpstreamError as e:
        raise _upstream(e, job_type, str(job_id))
    if str(job.get("id")) != str(job_id):
        raise DQServiceError(502, "UPSTREAM_INCONSISTENT",
                             f"runner returned job {job.get('id')!r} for {job_id}")
    if job.get("org_id") != principal.org_id:
        log.warning("dq_bind.cross_org_job", job_id=str(job_id), job_org=job.get("org_id"))
        raise forbidden(f"{job_type} {job_id} does not belong to your organization")
    return {"verified": True, "id": str(job_id), "kind": kind, "name": name,
            "org_id": job.get("org_id"), **{k: v for k, v in extra.items() if v is not None}}


async def resolve_dataset(principal: Principal, fqn: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.ucs_enabled:
        raise validation_failed("ucs_fqn", "UCS integration is disabled; cannot verify the dataset")
    client = UcsClient(settings)
    try:
        ds = await client.resolve_dataset(fqn, principal=principal)
        canonical = ds.get("fqn")
        obj_type = str(ds.get("object_type") or "").lower()
        if not canonical:
            raise not_found("dataset", fqn)
        if obj_type not in DATASET_OBJECT_TYPES:
            raise DQServiceError(422, "DATASET_TYPE_MISMATCH",
                                 f"{fqn} is a {obj_type or 'unknown'!s} object, not a dataset")
        if ds.get("org_id") not in (None, principal.org_id):
            raise forbidden(f"dataset {fqn} does not belong to your organization")
        decision = await client.authorize(principal=principal, action=settings.ucs_bind_action,
                                          obj=canonical)
    except UcsContractUnavailable as e:
        if settings.ucs_bind_dev_bypass and settings.environment == "dev":
            log.warning("dq_bind.dataset_check_bypassed_dev", fqn=fqn)
            return {"verified": False, "fqn": fqn, "object_type": None,
                    "bypass": "UCS_BIND_DEV_BYPASS (dev only)", "reason": str(e)}
        raise DQServiceError(503, "UCS_CONTRACT_UNAVAILABLE",
                             f"cannot verify and authorize dataset {fqn}: {e}")
    except UpstreamError as e:
        raise _upstream(e, "dataset", fqn)
    if decision.get("decision") != "allow":
        raise forbidden(decision.get("reason")
                        or f"not authorized to {settings.ucs_bind_action} on {canonical}")
    return {"verified": True, "fqn": canonical, "object_type": obj_type,
            "action": settings.ucs_bind_action}


class BindingService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, principal: Principal, req: JobBindingCreate, *,
                     bearer: str | None) -> JobBinding:
        settings = get_settings()
        org_id = principal.org_id
        rs = await self._db.get(Ruleset, req.ruleset_id)
        if not rs or rs.org_id != org_id:
            raise not_found("ruleset", str(req.ruleset_id))
        try:
            validate_identifier(req.target_table, field="target_table")
            validate_fqn(req.ucs_fqn)
        except RenderSecurityError as e:
            raise validation_failed("target_table/ucs_fqn", str(e))
        if req.flink_fail_stop and not (req.job_type == "flink_job"
                                        and req.enforcement_mode == "blocking"):
            raise validation_failed("flink_fail_stop",
                                    "fail-stop is only meaningful for blocking flink_job bindings")
        n = (await self._db.execute(select(sqlfunc.count()).select_from(JobBinding)
                                    .where(JobBinding.org_id == org_id))).scalar_one()
        if n >= settings.max_bindings_per_org:
            raise validation_failed("ruleset_id", f"Max {settings.max_bindings_per_org} bindings per org")
        if (await self._db.execute(select(JobBinding).where(
                JobBinding.ruleset_id == req.ruleset_id, JobBinding.job_type == req.job_type,
                JobBinding.job_id == req.job_id, JobBinding.target_table == req.target_table)
        )).scalar_one_or_none():
            raise conflict(f"Binding already exists for {req.job_type}/{req.job_id}/{req.target_table}")

        job = await resolve_job(principal, req.job_type, req.job_id, bearer)
        dataset = await resolve_dataset(principal, req.ucs_fqn)
        b = JobBinding(
            org_id=org_id, ruleset_id=req.ruleset_id, job_type=req.job_type, job_id=req.job_id,
            target_table=req.target_table, ucs_fqn=dataset["fqn"],
            enforcement_mode=req.enforcement_mode, flink_fail_stop=req.flink_fail_stop,
            version=1, created_by=principal.user_id,
        )
        self._apply_resolution(b, job, dataset)
        self._db.add(b)
        await self._db.flush()
        await self._db.refresh(b)
        await audit.record(self._db, org_id=org_id, actor=principal.user_id, resource_type="binding",
                           resource_id=b.id, operation="create", resource_version=b.version,
                           object_name=b.ucs_fqn,
                           detail={"job_type": b.job_type, "job_id": str(b.job_id),
                                   "ruleset_id": str(b.ruleset_id), "ruleset_version": rs.version,
                                   "enforcement_mode": b.enforcement_mode,
                                   "resolution": b.resolution})
        return b

    @staticmethod
    def _apply_resolution(b: JobBinding, job: dict, dataset: dict) -> None:
        b.job_name = job.get("name")
        b.job_kind = job.get("kind")
        b.job_org_id = job.get("org_id")
        b.dataset_fqn = dataset.get("fqn")
        b.dataset_object_type = dataset.get("object_type")
        b.resolution = {"job": job, "dataset": dataset}
        b.resolved_at = datetime.now(timezone.utc)

    async def resolve(self, principal: Principal, binding_id: uuid.UUID, *,
                      bearer: str | None) -> JobBinding:
        """Re-resolve an existing (e.g. pre-0003, unresolved) binding."""
        b = await self.get(principal.org_id, binding_id)
        job = await resolve_job(principal, b.job_type, b.job_id, bearer)
        dataset = await resolve_dataset(principal, b.ucs_fqn)
        self._apply_resolution(b, job, dataset)
        b.ucs_fqn = dataset["fqn"]
        b.version = (b.version or 1) + 1
        await self._db.flush()
        await self._db.refresh(b)
        await audit.record(self._db, org_id=b.org_id, actor=principal.user_id,
                           resource_type="binding", resource_id=b.id, operation="resolve",
                           resource_version=b.version, object_name=b.ucs_fqn,
                           detail={"resolution": b.resolution})
        return b

    async def list_for_org(self, org_id: str, *, job_type: str | None = None,
                           job_id: uuid.UUID | None = None,
                           ruleset_id: uuid.UUID | None = None) -> list[JobBinding]:
        stmt = (select(JobBinding).options(selectinload(JobBinding.ruleset))
                .where(JobBinding.org_id == org_id))
        if job_type:
            stmt = stmt.where(JobBinding.job_type == job_type)
        if job_id:
            stmt = stmt.where(JobBinding.job_id == job_id)
        if ruleset_id:
            stmt = stmt.where(JobBinding.ruleset_id == ruleset_id)
        return list((await self._db.execute(stmt.order_by(JobBinding.created_at.desc()))).scalars())

    async def get(self, org_id: str, binding_id: uuid.UUID) -> JobBinding:
        b = (await self._db.execute(
            select(JobBinding).options(selectinload(JobBinding.ruleset))
            .where(JobBinding.org_id == org_id, JobBinding.id == binding_id)
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if not b:
            raise not_found("binding", str(binding_id))
        return b

    async def delete(self, principal: Principal, binding_id: uuid.UUID) -> None:
        b = await self.get(principal.org_id, binding_id)
        await audit.record(self._db, org_id=b.org_id, actor=principal.user_id,
                           resource_type="binding", resource_id=b.id, operation="delete",
                           resource_version=b.version, object_name=b.ucs_fqn)
        await self._db.delete(b)
