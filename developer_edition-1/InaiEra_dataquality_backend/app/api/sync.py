"""Sync API — queue QualityCheckRegistrations for UCS (outbox, at-least-once)."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from app.core.deps import DB, Syncer
from app.services.ucs_sync_service import UcsSyncService

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.post("/ucs")
async def sync_ucs(user: Syncer, db: DB, binding_id: UUID | None = Query(None)) -> dict:
    return await UcsSyncService(db).sync_org(user.org_id, binding_id=binding_id)
