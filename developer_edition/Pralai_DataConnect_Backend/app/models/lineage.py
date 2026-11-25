"""Lineage models — data asset catalog and directional lineage graph."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPk


class Asset(Base, UuidPk, TimestampMixin):
    """
    A node in the data lineage graph — any named, addressable data artifact.

    asset_type values used by the lineage service:
        source          — upstream source system table / endpoint
        connector       — Kafka Connect connector instance
        kafka_topic     — Kafka topic (raw or prepared zone)
        processor       — quality processor, transform, or stream job
        sink            — downstream database table, data warehouse, etc.

    qualified_name is a globally unique identifier within an org:
        <org_id>:<asset_type>:<domain>:<system>:<entity>
    e.g.: org_1001:topic:sales.salesforce.opportunity.cdc.v1
    """

    __tablename__ = "dc_assets"
    __table_args__ = (
        UniqueConstraint("org_id", "qualified_name"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(500))
    qualified_name: Mapped[str] = mapped_column(String(1000))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class LineageEdge(Base, UuidPk):
    """
    A directed edge between two Assets in the lineage graph.

    edge_type values:
        CONNECTS_TO     — source → connector
        INGESTS_TO      — connector → raw topic
        VALIDATED_BY    — raw topic → quality processor (only for a pipeline that has a quality policy)
        QUARANTINES_TO  — quality processor → quarantine topic (the policy routes failures to quarantine)
        EMITS_TO        — quality processor → prepared topic (the pipeline ENFORCEs its policy)
        WRITTEN_TO      — prepared topic → sink table (runtime), or connector → sink target (a sink pipeline)
        CONSUMED_BY     — input topic → connector (a sink pipeline reads existing topics)

    pipeline_id links the edge back to the pipeline that created it,
    enabling per-pipeline lineage subgraph queries.

    The (org_id, from_asset_id, to_asset_id, edge_type, pipeline_id) unique
    constraint is what makes repeated pipeline deploys/reconciliation
    idempotent at the database level (see lineage_service.py's module
    docstring) — a get-then-insert that loses a race to a concurrent
    materialize call hits this constraint instead of silently duplicating
    the edge. Note: Postgres treats each NULL pipeline_id as distinct for
    uniqueness purposes, so this constraint only fully dedups edges that
    carry a real pipeline_id — true for every edge
    materialize_pipeline_lineage() creates, not necessarily for a
    consume_runtime_event() edge whose event omitted pipeline_id.
    """

    __tablename__ = "dc_lineage_edges"
    __table_args__ = (
        UniqueConstraint("org_id", "from_asset_id", "to_asset_id", "edge_type", "pipeline_id"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("dc_organizations.org_id"), index=True)
    from_asset_id: Mapped[str] = mapped_column(ForeignKey("dc_assets.id"), index=True)
    to_asset_id: Mapped[str] = mapped_column(ForeignKey("dc_assets.id"), index=True)
    edge_type: Mapped[str] = mapped_column(String(100))
    # SET NULL, not the FK's implicit RESTRICT default: an edge is lineage
    # history for an asset, not owned by the pipeline that created it, so
    # deleting the pipeline must be able to proceed — see the sibling
    # ondelete on QualityResult.pipeline_id for the same reasoning.
    pipeline_id: Mapped[str | None] = mapped_column(
        ForeignKey("dc_pipelines.id", ondelete="SET NULL"), index=True
    )
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
