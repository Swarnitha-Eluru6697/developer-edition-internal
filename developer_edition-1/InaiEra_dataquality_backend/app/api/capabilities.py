"""Engine capability API (DQ-MODEL-002 / DQ-014)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core import quality_contract as qc
from app.core.deps import Reader
from app.core.errors import not_found
from app.renderers.capability import ENGINE_CAPABILITY_MATRIX
from app.renderers.compat import RENDERER_VERSION, SUPPORTED_ENGINES

router = APIRouter(prefix="/api/v1/capabilities", tags=["capabilities"])


def _entry(job_type: str) -> dict:
    return {"job_type": job_type, **ENGINE_CAPABILITY_MATRIX[job_type],
            "supported_engine_versions": SUPPORTED_ENGINES[job_type]}


@router.get("")
async def capabilities(user: Reader) -> dict:
    return {"renderer_version": RENDERER_VERSION,
            "quality_contract": {"name": qc.CONTRACT_NAME, "version": qc.CONTRACT_VERSION},
            "engines": {jt: _entry(jt) for jt in ENGINE_CAPABILITY_MATRIX}}


@router.get("/{job_type}")
async def capability(job_type: str, user: Reader) -> dict:
    if job_type not in ENGINE_CAPABILITY_MATRIX:
        raise not_found("job_type", job_type)
    return {"renderer_version": RENDERER_VERSION, **_entry(job_type)}
