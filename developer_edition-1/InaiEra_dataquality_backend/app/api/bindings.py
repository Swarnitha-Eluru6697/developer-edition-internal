"""Job Bindings API."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import func as sf, select

from app.core.deps import DB, Applier, Binder, Reader, bearer_from_header
from app.models import RulesetItem
from app.schemas import JobBindingCreate, JobBindingOut
from app.services.apply_service import ApplyService
from app.services.binding_service import BindingService

router = APIRouter(prefix="/api/v1/bindings", tags=["bindings"])


async def _with_item_counts(db, bindings: list) -> None:
    ids = {b.ruleset_id for b in bindings if b.ruleset is not None}
    if not ids:
        return
    counts = dict((await db.execute(
        select(RulesetItem.ruleset_id, sf.count()).where(RulesetItem.ruleset_id.in_(ids))
        .group_by(RulesetItem.ruleset_id))).all())
    for b in bindings:
        if b.ruleset is not None:
            b.ruleset.item_count = counts.get(b.ruleset_id, 0)


@router.get("", response_model=list[JobBindingOut])
async def list_bindings(user: Reader, db: DB, job_type: str | None = Query(None),
                        job_id: UUID | None = Query(None), ruleset_id: UUID | None = Query(None)):
    bindings = await BindingService(db).list_for_org(user.org_id, job_type=job_type, job_id=job_id,
                                                     ruleset_id=ruleset_id)
    await _with_item_counts(db, bindings)
    return [JobBindingOut.model_validate(b) for b in bindings]


@router.post("", response_model=JobBindingOut, status_code=status.HTTP_201_CREATED)
async def create_binding(payload: JobBindingCreate, user: Binder, db: DB,
                         authorization: Annotated[str | None, Header()] = None) -> JobBindingOut:
    """DQ-BIND-003: resolves the job via its runner (caller's bearer) and the
    dataset via UCS (existence + dq.bind authorization) before persisting the
    canonical identifiers."""
    svc = BindingService(db)
    b = await svc.create(user, payload, bearer=bearer_from_header(authorization))
    b = await svc.get(user.org_id, b.id)
    await _with_item_counts(db, [b])
    return JobBindingOut.model_validate(b)


@router.get("/{binding_id}", response_model=JobBindingOut)
async def get_binding(binding_id: UUID, user: Reader, db: DB) -> JobBindingOut:
    b = await BindingService(db).get(user.org_id, binding_id)
    await _with_item_counts(db, [b])
    return JobBindingOut.model_validate(b)


@router.post("/{binding_id}/resolve", response_model=JobBindingOut)
async def resolve_binding(binding_id: UUID, user: Binder, db: DB,
                          authorization: Annotated[str | None, Header()] = None) -> JobBindingOut:
    """Re-resolve a binding (required for bindings created before resolution existed)."""
    svc = BindingService(db)
    await svc.resolve(user, binding_id, bearer=bearer_from_header(authorization))
    b = await svc.get(user.org_id, binding_id)
    await _with_item_counts(db, [b])
    return JobBindingOut.model_validate(b)


@router.delete("/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(binding_id: UUID, user: Binder, db: DB) -> None:
    await BindingService(db).delete(user, binding_id)


@router.post("/{binding_id}/apply")
async def apply_binding(binding_id: UUID, user: Applier, db: DB,
                        authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
    """Compile and push this binding's artifact. 200 applied / not_applicable;
    422 APPLY_REFUSED (unsupported blocking rule, incompatible engine, invalid
    artifact, unresolved binding, contract mismatch); 502 runner failure."""
    code, body = await ApplyService(db).apply(user, binding_id,
                                              bearer=bearer_from_header(authorization))
    return JSONResponse(status_code=code, content=body)
