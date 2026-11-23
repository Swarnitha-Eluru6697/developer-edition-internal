"""Register bound checks with UCS as QualityCheckRegistrations.

Registrations are written to the transactional outbox (one per binding x
enabled item x exact rule/ruleset/binding version) and delivered by the
outbox worker with a scoped JWT (ucs.quality.checks.write) and an
Idempotency-Key — never by posting a fabricated quality RESULT, and never
with a standing admin token. check_id (= ruleset item id) is the same id
the FlinkSQL quality job stamps on every result record, so UCS can join
registrations, QualityRuns and gate events.

QualityCheckRegistration (contract "inaiera.dq.check-registration" v1):
  org_id, dataset_fqn, check_id, check_name, rule_id, rule_name,
  rule_version, rule_type, dimension, severity, column_name, threshold,
  ruleset_id, ruleset_version, binding_id, binding_version, job_type,
  job_id, enforcement_mode, artifact_version_id, artifact_content_hash,
  renderer_version
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models import ArtifactVersion, JobBinding, RulesetItem
from app.renderers.params import ParameterError, effective_parameters
from app.services.outbox import KIND_CHECK_REGISTRATION, enqueue
from app.services.ruleset_service import item_dict

REGISTRATION_CONTRACT = "inaiera.dq.check-registration"
REGISTRATION_CONTRACT_VERSION = "1"


async def _latest_standard(db: AsyncSession, binding_id: uuid.UUID) -> ArtifactVersion | None:
    return (await db.execute(
        select(ArtifactVersion).where(ArtifactVersion.binding_id == binding_id,
                                      ArtifactVersion.kind == "standard",
                                      ArtifactVersion.validation_status == "valid")
        .order_by(ArtifactVersion.rendered_at.desc()).limit(1))).scalar_one_or_none()


async def enqueue_registrations(db: AsyncSession, b: JobBinding,
                                version: ArtifactVersion | None = None) -> int:
    version = version or await _latest_standard(db, b.id)
    rows = (await db.execute(
        select(RulesetItem).options(selectinload(RulesetItem.rule))
        .where(RulesetItem.ruleset_id == b.ruleset_id))).scalars().all()
    ruleset_version = b.ruleset.version if b.ruleset else None
    n = 0
    for it in rows:
        if not it.enabled or it.rule is None:
            continue
        d = item_dict(it, it.rule)
        try:
            threshold = effective_parameters(d["rule_type"], d["parameters"],
                                             d["parameter_overrides"], strict=False).get("threshold", 0.0)
        except ParameterError:
            threshold = None
        payload = {
            "contract": REGISTRATION_CONTRACT, "contract_version": REGISTRATION_CONTRACT_VERSION,
            "org_id": b.org_id, "dataset_fqn": b.ucs_fqn, "check_id": d["item_id"],
            "check_name": d["rule_name"] + (f"({d['column_name']})" if d["column_name"] else ""),
            "rule_id": d["rule_id"], "rule_name": d["rule_name"], "rule_version": d["rule_version"],
            "rule_type": d["rule_type"], "dimension": d["dimension"], "severity": d["severity"],
            "column_name": d["column_name"] or None, "threshold": threshold,
            "ruleset_id": str(b.ruleset_id), "ruleset_version": ruleset_version,
            "binding_id": str(b.id), "binding_version": b.version, "job_type": b.job_type,
            "job_id": str(b.job_id), "enforcement_mode": b.enforcement_mode,
            "artifact_version_id": str(version.id) if version else None,
            "artifact_content_hash": version.content_hash if version else None,
            "renderer_version": version.renderer_version if version else None,
        }
        key = (f"checkreg:{b.id}:{d['item_id']}:r{d['rule_version']}:rs{ruleset_version}"
               f":b{b.version}:a{version.id if version else '-'}")
        await enqueue(db, org_id=b.org_id, kind=KIND_CHECK_REGISTRATION, idempotency_key=key,
                      payload=payload)
        n += 1
    return n


class UcsSyncService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def sync_org(self, org_id: str, *, binding_id: uuid.UUID | None = None) -> dict:
        settings = get_settings()
        stmt = (select(JobBinding).options(selectinload(JobBinding.ruleset))
                .where(JobBinding.org_id == org_id))
        if binding_id is not None:
            stmt = stmt.where(JobBinding.id == binding_id)
        bindings = list((await self._db.execute(stmt)).scalars())
        queued = 0
        for b in bindings:
            queued += await enqueue_registrations(self._db, b)
        return {
            "bindings": len(bindings), "registrations_queued": queued,
            "delivery": ("outbox → UCS (at-least-once, Idempotency-Key)"
                         if settings.ucs_contract_v2_enabled else
                         "queued durably; delivery starts once UCS_CONTRACT_V2_ENABLED=true "
                         "(UCS check-registration endpoint with service-JWT auth)"),
        }
