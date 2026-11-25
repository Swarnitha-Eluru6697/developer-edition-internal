"""Discovery schemas (GAP-10) — see app/models/discovery.py."""

from datetime import datetime

from pydantic import BaseModel


class DiscoveryRun(BaseModel):
    """The pollable summary GET /discovery/{id} returns — never the objects
    themselves, see DiscoveryObject/DiscoveryObjectPage for those."""

    id: str
    org_id: str
    connection_id: str
    operation_id: str | None = None
    status: str
    object_count: int
    continuation: str | None = None
    error_message: str | None = None
    truncated: bool = False
    created_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class DiscoveryObject(BaseModel):
    id: str
    object_type: str
    schema_name: str | None = None
    name: str

    model_config = {"from_attributes": True}


class DiscoveryObjectPage(BaseModel):
    """GET /discovery/{id}/objects's response — `next_cursor` is an opaque
    token (pass it back as `cursor` to fetch the next page); null means
    there's nothing more to page through."""

    objects: list[DiscoveryObject]
    next_cursor: str | None = None


class DiscoveryFacets(BaseModel):
    """GET /discovery/{id}/facets's response — the distinct schema names and
    object types a run actually produced, so the UI can offer them as filter
    options (FE-BUG-12) without paging through every object first. Computed
    with SQL DISTINCT rather than by scanning pages client-side, so it stays
    cheap even for a run with tens of thousands of objects."""

    schema_names: list[str]
    object_types: list[str]
