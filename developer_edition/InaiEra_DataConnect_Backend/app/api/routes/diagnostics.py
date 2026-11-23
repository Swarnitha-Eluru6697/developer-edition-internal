"""
GET /diagnostics/bundle — the redacted support bundle for the caller's organization (see app/services/diagnostics.py for what it contains
and, as importantly, what it never does). Admin only: it reveals how the deployment is configured and failing, even with every secret masked.
"""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from gateway_auth import Principal

from app.core.permissions import require
from app.core.tenant import get_org_id
from app.services.diagnostics import build_diagnostics_bundle

router = APIRouter()


@router.get("/bundle", response_model=dict[str, Any], summary="Redacted support diagnostics bundle for this organization")
async def diagnostics_bundle(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:settings:edit")),
) -> JSONResponse:
    bundle = await build_diagnostics_bundle(org_id=org_id)
    stamp = bundle["generated_at"].replace(":", "").replace("-", "")[:15]
    return JSONResponse(bundle, headers={"Content-Disposition": f'attachment; filename="dataconnect-diagnostics-{stamp}.json"', "Cache-Control": "no-store"})
