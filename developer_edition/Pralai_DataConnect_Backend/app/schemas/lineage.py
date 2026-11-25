from typing import Any

from pydantic import BaseModel, Field


class Asset(BaseModel):
    id: str
    org_id: str
    asset_type: str
    name: str
    qualified_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class LineageEdge(BaseModel):
    id: str
    org_id: str
    from_asset_id: str
    to_asset_id: str
    edge_type: str
    pipeline_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LineageGraph(BaseModel):
    nodes: list[Asset] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
    #: FE-BUG-20: true when an expansion hop had more edges than the per-hop
    #: limit and the rest were left out. `omitted_edges` is how many, and
    #: `next_offset` is the `offset` to pass (with the same depth=1 request) to
    #: get the next page; null when nothing was left out.
    truncated: bool = False
    omitted_edges: int = 0
    next_offset: int | None = None


class LineageRuntimeEvent(BaseModel):
    org_id: str
    event_type: str
    from_asset: dict[str, Any]
    to_asset: dict[str, Any]
    edge_type: str
    pipeline_id: str | None = None
    connector_id: str | None = None
    transform_policy_id: str | None = None
    schema_version_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
