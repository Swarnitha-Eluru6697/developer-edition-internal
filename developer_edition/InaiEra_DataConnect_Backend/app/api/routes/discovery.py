"""
Discovery router (GAP-10) — GET /discovery/{id} polls a connection-discover
run's small summary (status/object_count), and GET /discovery/{id}/objects
pages through the objects it actually found (search + cursor pagination,
plus object_type/schema_name filters — FE-BUG-12), instead of reading them
all back at once the way Operation.result used to. GET /discovery/{id}/facets
(FE-BUG-12) hands back the distinct schema names/object types for a run, so
the UI can populate those filters' dropdowns without paging through
everything first.

Requires dataconnect:connections:test, the same permission POST
/connections/{id}/discover requires — reading back a discovery run's own
result is part of the same action, not a separate permission.
"""

from fastapi import APIRouter, Depends, Query
from gateway_auth import Principal

from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.discovery import DiscoveryFacets, DiscoveryObjectPage, DiscoveryRun
from app.services.discovery_service import discovery_service

router = APIRouter()


@router.get("/{discovery_run_id}", response_model=DiscoveryRun, summary="Get discovery run status")
async def get_discovery_run(
    discovery_run_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    return await discovery_service.get_run_or_404(org_id, discovery_run_id)


@router.get(
    "/{discovery_run_id}/facets",
    response_model=DiscoveryFacets,
    summary="Distinct schema names and object types for a discovery run's filter dropdowns",
)
async def get_discovery_facets(
    discovery_run_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    return await discovery_service.get_facets(org_id, discovery_run_id)


@router.get(
    "/{discovery_run_id}/objects",
    response_model=DiscoveryObjectPage,
    summary="Page through a discovery run's discovered schemas/tables",
)
async def list_discovery_objects(
    discovery_run_id: str,
    search: str | None = Query(default=None, description="Case-insensitive substring match on schema/object name"),
    object_type: str | None = Query(default=None, description="Exact match, e.g. 'schema' or 'table'"),
    schema_name: str | None = Query(default=None, description="Exact match on schema_name"),
    limit: int = Query(default=100, ge=1, le=1000),
    cursor: str | None = Query(default=None, description="Opaque token from a previous page's next_cursor"),
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    objects, next_cursor = await discovery_service.list_objects(
        org_id, discovery_run_id, search=search, limit=limit, cursor=cursor,
        object_type=object_type, schema_name=schema_name,
    )
    return {"objects": objects, "next_cursor": next_cursor}
