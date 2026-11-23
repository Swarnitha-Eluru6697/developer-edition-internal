"""Internal validate endpoint — called by DQ (a short-lived scoped
'flink.jobs.validate' JWT, DQ-AUTH-012) and Workspace codegen chat (still
the legacy static token — require_service_scope() falls back to it)."""
from typing import Annotated
from fastapi import APIRouter, Depends
from app.core.service_auth import require_service_scope
from app.schemas import ValidateRequest, ValidateResponse
from app.services.validate_service import validate_endpoint

router = APIRouter(tags=["validate"])

@router.post("/api/v1/validate", response_model=ValidateResponse)
async def validate_flink_sql(
    payload: ValidateRequest,
    _claims: Annotated[dict | None, Depends(require_service_scope("flink.jobs.validate"))],
) -> ValidateResponse:
    """Validate FlinkSQL syntax via the Flink SQL Gateway EXPLAIN path.
    Auth: X-Internal-Service-Token only (a scoped JWT or the legacy static
    token — see app.core.service_auth). Never accepts user JWTs."""
    return await validate_endpoint(payload)
