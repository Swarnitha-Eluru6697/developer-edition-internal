from typing import Literal

from fastapi import APIRouter, Depends, Query
from gateway_auth import Principal

from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.lineage import Asset, LineageGraph, LineageRuntimeEvent
from app.services.lineage_service import lineage_service

router = APIRouter()


@router.get("/assets", response_model=list[Asset])
async def list_assets(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:lineage:view")),
) -> list[dict]:
    return await lineage_service.list_assets(org_id)


@router.get("/lineage/{asset_id}", response_model=LineageGraph, summary="Bounded expansion from one asset")
async def get_lineage(
    asset_id: str,
    # FE-BUG-20: bounded, directional, optionally type-filtered expansion —
    # never the whole lineage graph in one response. depth/direction match
    # the "expand upstream/downstream" interaction the frontend builds on
    # top of this; asset_type stays a plain string (not a Literal) because
    # Asset.asset_type itself is an unconstrained column (consume_runtime_event
    # accepts caller-supplied asset types) — constraining the filter tighter
    # than the data it filters would just reject legitimate values.
    depth: int = Query(default=1, ge=0, le=lineage_service.MAX_LINEAGE_DEPTH),
    direction: Literal["upstream", "downstream", "both"] = Query(default="both"),
    asset_type: str | None = Query(default=None),
    # Width bound: at most `limit` edges per hop (default settings.lineage_max_edges_per_hop);
    # `offset` pages through the first hop's edges ("show more" on one node) and is
    # meant for depth=1 requests.
    limit: int | None = Query(default=None, ge=1, le=lineage_service.MAX_EDGES_PER_HOP_LIMIT),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:lineage:view")),
) -> dict:
    return await lineage_service.graph_for_asset(
        org_id, asset_id, depth=depth, direction=direction, asset_type=asset_type, limit=limit, offset=offset,
    )


@router.get("/lineage/pipeline/{pipeline_id}", response_model=LineageGraph)
async def get_pipeline_lineage(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:lineage:view")),
) -> dict:
    return await lineage_service.graph_for_pipeline(org_id, pipeline_id)


@router.post("/lineage/events", response_model=LineageGraph)
async def consume_lineage_event(
    payload: LineageRuntimeEvent,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:lineage:edit")),
) -> dict:
    event = payload.model_dump()
    if event["org_id"] != org_id:
        event["org_id"] = org_id
    return await lineage_service.consume_runtime_event(event)
