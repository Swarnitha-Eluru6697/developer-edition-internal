"""
Lineage graph — Postgres-backed asset catalog and directional edges.

Previously this whole service was memory-store-backed: materialize_pipeline_lineage()
(called every time a pipeline deploys — see PipelineService._lifecycle) and
consume_runtime_event() each did their own get-or-create against a process-local
in-memory dict, deduplicated purely by a linear scan with no locking and no
underlying database constraint. That happened to prevent duplicates for a
single sequential process, but gave no real guarantee under any genuine
concurrency (two overlapping deploy calls, multiple worker processes, or a
crash-and-retry mid-materialize) — and it left the Asset/LineageEdge Postgres
models this feature was actually built against (Asset already had a real
UniqueConstraint(org_id, qualified_name)) sitting unused the whole time.

Idempotency now rests on two things, mirroring this codebase's existing
get-or-create convention (see RuntimeReconciler.record_desired) rather than a
dialect-specific ON CONFLICT clause (not used anywhere else in this codebase):

  1. Stable, deterministic qualified names / edge-identity tuples — unchanged
     from before. Every qualified_name is built only from a pipeline's
     immutable identity fields (domain/source_system/entity/connector_name/
     topic_name/id — see PipelineUpdate's docstring for why those can't drift
     between deploys of the same pipeline), never a timestamp or random id,
     so calling materialize_pipeline_lineage() twice for the same pipeline
     always targets the same rows.

  2. Real database uniqueness constraints. Asset already had
     UniqueConstraint(org_id, qualified_name); LineageEdge gained an
     equivalent UniqueConstraint(org_id, from_asset_id, to_asset_id,
     edge_type, pipeline_id) (see app/models/lineage.py). A get-then-insert
     is still how the "upsert" is expressed, but it's no longer just an
     in-memory linear scan: if two calls race and both miss the initial
     lookup, the loser's insert hits the unique constraint (IntegrityError,
     caught via a SAVEPOINT so the rest of the transaction survives) instead
     of silently creating a duplicate row, and recovers by re-fetching and
     updating the winning row — see _get_or_create_asset / _get_or_create_edge.
"""

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.lineage import Asset, LineageEdge
from app.repositories.dataconnect import AssetRepository, LineageEdgeRepository
from app.services.pipeline_topics import input_topics, is_sink_pipeline
from app.services.quality_mode import prepared_topic_for
from app.services.topic_naming import validation_quarantine_topic_for
from app.services.ucs_lineage_sync import emit_runtime_edge_lineage


def _asset_to_dict(row: Asset) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "asset_type": row.asset_type,
        "name": row.name,
        "qualified_name": row.qualified_name,
        "metadata": row.metadata_json,
    }


def _edge_to_dict(row: LineageEdge) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "from_asset_id": row.from_asset_id,
        "to_asset_id": row.to_asset_id,
        "edge_type": row.edge_type,
        "pipeline_id": row.pipeline_id,
        "metadata": row.metadata_json,
    }


class LineageService:
    async def list_assets(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await AssetRepository(session).list_for_org(org_id)
            return [_asset_to_dict(row) for row in rows]

    # FE-BUG-20: caps a single graph_for_asset() call's traversal even if an
    # org's graph has grown large from accumulated consume_runtime_event()
    # calls — a caller wanting to keep going re-expands from the resulting
    # frontier nodes with another call, rather than this one request ever
    # walking an unbounded number of hops.
    MAX_LINEAGE_DEPTH = 5
    # Upper bound a caller may request for `limit` (the default is settings.lineage_max_edges_per_hop).
    MAX_EDGES_PER_HOP_LIMIT = 500

    async def graph_for_asset(
        self, org_id: str, asset_id: str, *, depth: int = 1, direction: str = "both", asset_type: str | None = None,
        limit: int | None = None, offset: int = 0,
    ) -> dict:
        """Bounded BFS outward from `asset_id`, up to `depth` hops — never
        the org's whole lineage graph the way this used to work (it
        previously returned only the seed asset as `nodes` while `edges`
        included every edge touching it in either direction, including ones
        pointing at neighbor ids that were never in `nodes` — a real bug,
        not just unbounded).

        `direction`:
          - "downstream": follow edges forward (from_asset_id -> to_asset_id)
            — what this asset feeds into.
          - "upstream": follow edges backward (to_asset_id -> from_asset_id)
            — what feeds into this asset.
          - "both": either direction, one hop at a time.

        `asset_type`, if set, only continues expansion through (and
        includes) neighbor nodes of that type — the seed node is always
        included regardless of its own type. An edge is only kept once
        both its endpoints have survived this filtering, so a type-filtered-
        out neighbor never leaves a dangling edge in the response.

        Width is bounded too, not just depth: each hop reads at most `limit`
        edges (settings.lineage_max_edges_per_hop by default), already
        filtered by `direction` in SQL, so a hub asset with thousands of links
        is never loaded whole. When a hop had more, the response says so
        (`truncated`, `omitted_edges`) and `next_offset` pages on to the rest;
        `offset` skips edges on the FIRST hop only (the seed's own), so
        "show more" is a depth=1 call. The cap applies to edges before
        `asset_type` filtering, so a hub's neighbors of the wanted type may be
        beyond the first page.
        """
        depth = max(0, min(depth, self.MAX_LINEAGE_DEPTH))
        limit = max(1, min(limit or settings.lineage_max_edges_per_hop, self.MAX_EDGES_PER_HOP_LIMIT))
        offset = max(0, offset)
        omitted_edges = 0
        next_offset: int | None = None
        async with AsyncSessionLocal() as session:
            asset_repo = AssetRepository(session)
            edge_repo = LineageEdgeRepository(session)

            seed = await asset_repo.get_for_org(org_id, asset_id)
            if seed is None:
                return {"nodes": [], "edges": []}

            nodes_by_id: dict[str, Asset] = {seed.id: seed}
            edges_by_id: dict[str, LineageEdge] = {}
            frontier = {seed.id}
            for hop in range(depth):
                if not frontier:
                    break
                hop_offset = offset if hop == 0 else 0
                hop_edges = await edge_repo.list_touching(
                    org_id, frontier, direction=direction, limit=limit + 1, offset=hop_offset,
                )
                if len(hop_edges) > limit:
                    hop_edges = hop_edges[:limit]
                    total = await edge_repo.count_touching(org_id, frontier, direction=direction)
                    omitted_edges += max(0, total - hop_offset - limit)
                    if hop == 0:
                        next_offset = hop_offset + limit
                neighbor_ids: set[str] = set()
                for edge in hop_edges:
                    if edge.from_asset_id in frontier and direction in ("both", "downstream"):
                        neighbor_ids.add(edge.to_asset_id)
                    if edge.to_asset_id in frontier and direction in ("both", "upstream"):
                        neighbor_ids.add(edge.from_asset_id)

                new_ids = neighbor_ids - nodes_by_id.keys()
                new_assets = await asset_repo.get_by_ids(org_id, list(new_ids))
                next_frontier: set[str] = set()
                for a in new_assets:
                    if asset_type and a.asset_type != asset_type:
                        continue
                    nodes_by_id[a.id] = a
                    next_frontier.add(a.id)

                for edge in hop_edges:
                    if edge.from_asset_id in nodes_by_id and edge.to_asset_id in nodes_by_id:
                        edges_by_id[edge.id] = edge
                frontier = next_frontier

            return {
                "nodes": [_asset_to_dict(a) for a in nodes_by_id.values()],
                "edges": [_edge_to_dict(e) for e in edges_by_id.values()],
                "truncated": omitted_edges > 0,
                "omitted_edges": omitted_edges,
                "next_offset": next_offset,
            }

    async def graph_for_pipeline(self, org_id: str, pipeline_id: str) -> dict:
        """The ticket's own default view — the fixed 5-node chain
        materialize_pipeline_lineage() creates per pipeline. Resolves only
        the asset ids this pipeline's own edges actually reference (via
        get_by_ids), not the org's whole asset table — FE-BUG-20."""
        async with AsyncSessionLocal() as session:
            edges = await LineageEdgeRepository(session).list_for_pipeline(org_id, pipeline_id)
            asset_ids = {edge.from_asset_id for edge in edges} | {edge.to_asset_id for edge in edges}
            assets = await AssetRepository(session).get_by_ids(org_id, list(asset_ids))
            return {"nodes": [_asset_to_dict(a) for a in assets], "edges": [_edge_to_dict(e) for e in edges]}

    async def _materialize_sink_lineage(self, pipeline: dict) -> dict:
        """GAP-12: a sink reads existing topics and writes a table/object — it has no generated topic. Its chain is: input topic(s) ->
        connector -> the sink target. (Drawing it with a source node and a generated raw/prepared topic pair, as for a source, invented an
        output the sink never produces.) With a quality policy, its quality processor checks the same input topics and sends failing records
        to the sink's validation quarantine topic — drawn like a source's. An ENFORCE sink's passing records reach its connector through an
        internal hand-off (quality_mode.SINK_HANDOFF_PREFIX), which is not part of the data's lineage, so no prepared topic is drawn. This is
        the same flow the UCS lineage shows (ucs_lineage_sync: the sink job and its `<name>:quality` job)."""
        org_id = pipeline["org_id"]
        async with AsyncSessionLocal() as session:
            connector_asset = await self._get_or_create_asset(
                session, org_id=org_id, asset_type="connector", name=pipeline["connector_name"],
                qualified_name=f"{org_id}:connector:{pipeline['connector_name']}",
                metadata={"mode": pipeline["mode"], "runtime_pool_id": pipeline.get("runtime_pool_id")},
            )
            sink_asset = await self._get_or_create_asset(
                session, org_id=org_id, asset_type="sink", name=pipeline["entity"],
                qualified_name=f"{org_id}:sink:{pipeline['domain']}:{pipeline['source_system']}:{pipeline['entity']}",
                metadata={"source_system": pipeline["source_system"], "entity": pipeline["entity"]},
            )
            edge_metadata = {
                "connector_id": pipeline["connector_name"],
                "transform_policy_id": pipeline.get("transform_policy_id"),
                "schema_version_id": pipeline.get("schema_version_id"),
            }
            nodes, edges = [connector_asset, sink_asset], []
            policy = await self._quality_policy_of(pipeline)
            quality_asset = quarantine_topic_asset = None
            if policy is not None:
                quality_asset = await self._get_or_create_asset(
                    session, org_id=org_id, asset_type="processor",
                    name=f"{policy.get('name') or 'Quality policy'} (quality policy)",
                    qualified_name=f"{org_id}:processor:{pipeline['id']}:quality",
                    metadata={"quality_policy_id": pipeline.get("quality_policy_id"), "quality_mode": pipeline.get("quality_mode"),
                              "on_failure": policy.get("on_failure")},
                )
                nodes.append(quality_asset)
                if policy.get("on_failure", "route_to_quarantine") == "route_to_quarantine" and policy.get("quarantine_sink", "kafka") == "kafka":
                    quarantine_topic = validation_quarantine_topic_for(pipeline)
                    quarantine_topic_asset = await self._get_or_create_asset(
                        session, org_id=org_id, asset_type="kafka_topic", name=quarantine_topic,
                        qualified_name=f"{org_id}:topic:{quarantine_topic}", metadata={"zone": "quarantine"},
                    )
                    nodes.append(quarantine_topic_asset)
            for topic in input_topics(pipeline):
                topic_asset = await self._get_or_create_asset(
                    session, org_id=org_id, asset_type="kafka_topic", name=topic,
                    qualified_name=f"{org_id}:topic:{topic}", metadata={},
                )
                nodes.insert(0, topic_asset)
                edges.append(await self._get_or_create_edge(
                    session, org_id=org_id, from_asset_id=topic_asset.id, to_asset_id=connector_asset.id,
                    edge_type="CONSUMED_BY", pipeline_id=pipeline["id"], metadata=edge_metadata,
                ))
                if quality_asset is not None:
                    edges.append(await self._get_or_create_edge(
                        session, org_id=org_id, from_asset_id=topic_asset.id, to_asset_id=quality_asset.id,
                        edge_type="VALIDATED_BY", pipeline_id=pipeline["id"], metadata=edge_metadata,
                    ))
            edges.append(await self._get_or_create_edge(
                session, org_id=org_id, from_asset_id=connector_asset.id, to_asset_id=sink_asset.id,
                edge_type="WRITTEN_TO", pipeline_id=pipeline["id"], metadata=edge_metadata,
            ))
            if quality_asset is not None and quarantine_topic_asset is not None:
                edges.append(await self._get_or_create_edge(
                    session, org_id=org_id, from_asset_id=quality_asset.id, to_asset_id=quarantine_topic_asset.id,
                    edge_type="QUARANTINES_TO", pipeline_id=pipeline["id"], metadata=edge_metadata,
                ))
            # A policy removed (or changed to one that quarantines nothing) leaves no stale quality nodes behind — as for a source.
            await self._prune_stale_edges(session, org_id=org_id, pipeline_id=pipeline["id"], keep={e.id for e in edges})
            await session.commit()
            return {"nodes": [_asset_to_dict(a) for a in nodes], "edges": [_edge_to_dict(e) for e in edges]}

    async def materialize_pipeline_lineage(self, pipeline: dict, connection: dict | None = None) -> dict:
        if is_sink_pipeline(pipeline):
            return await self._materialize_sink_lineage(pipeline)
        org_id = pipeline["org_id"]
        source_name = (
            connection["name"] if connection else f"{pipeline['source_system']}.{pipeline['entity']}"
        )
        async with AsyncSessionLocal() as session:
            source_asset = await self._get_or_create_asset(
                session,
                org_id=org_id,
                asset_type="source",
                name=source_name,
                qualified_name=(
                    f"{org_id}:source:{pipeline['domain']}:{pipeline['source_system']}:{pipeline['entity']}"
                ),
                metadata={"source_system": pipeline["source_system"], "entity": pipeline["entity"]},
            )
            connector_asset = await self._get_or_create_asset(
                session,
                org_id=org_id,
                asset_type="connector",
                name=pipeline["connector_name"],
                qualified_name=f"{org_id}:connector:{pipeline['connector_name']}",
                metadata={"mode": pipeline["mode"], "runtime_pool_id": pipeline.get("runtime_pool_id")},
            )
            raw_topic_asset = await self._get_or_create_asset(
                session,
                org_id=org_id,
                asset_type="kafka_topic",
                name=pipeline["topic_name"],
                qualified_name=f"{org_id}:topic:{pipeline['topic_name']}",
                metadata={"zone": pipeline["zone"]},
            )
            # The quality processor, and what hangs off it, exist ONLY when the pipeline has a quality policy: a pipeline with none has the plain
            # source -> connector -> topic flow and nothing invented after it.
            policy = await self._quality_policy_of(pipeline)
            quality_asset = quarantine_topic_asset = prepared_topic_asset = None
            if policy is not None:
                quality_asset = await self._get_or_create_asset(
                    session,
                    org_id=org_id,
                    asset_type="processor",
                    name=f"{policy.get('name') or 'Quality policy'} (quality policy)",
                    qualified_name=f"{org_id}:processor:{pipeline['id']}:quality",
                    metadata={"quality_policy_id": pipeline.get("quality_policy_id"), "quality_mode": pipeline.get("quality_mode"),
                              "on_failure": policy.get("on_failure")},
                )
                # Failing records are routed to the validation quarantine topic (the same name the quality worker writes to).
                if policy.get("on_failure", "route_to_quarantine") == "route_to_quarantine" and policy.get("quarantine_sink", "kafka") == "kafka":
                    quarantine_topic = validation_quarantine_topic_for(pipeline)
                    quarantine_topic_asset = await self._get_or_create_asset(
                        session, org_id=org_id, asset_type="kafka_topic", name=quarantine_topic,
                        qualified_name=f"{org_id}:topic:{quarantine_topic}", metadata={"zone": "quarantine"},
                    )
                # NEW-01: the prepared topic is part of the flow only when the pipeline ENFORCEs its quality policy. In OBSERVE mode the worker
                # writes nothing to it, so drawing quality -> prepared would show an output that never receives data.
                prepared_topic = prepared_topic_for(pipeline)
                if prepared_topic:
                    prepared_topic_asset = await self._get_or_create_asset(
                        session,
                        org_id=org_id,
                        asset_type="kafka_topic",
                        name=prepared_topic,
                        qualified_name=f"{org_id}:topic:{prepared_topic}",
                        metadata={"zone": "prepared"},
                    )
            edge_metadata = {
                "connector_id": pipeline["connector_name"],
                "transform_policy_id": pipeline.get("transform_policy_id"),
                "schema_version_id": pipeline.get("schema_version_id"),
            }
            edges = [
                await self._get_or_create_edge(
                    session, org_id=org_id, from_asset_id=source_asset.id, to_asset_id=connector_asset.id,
                    edge_type="CONNECTS_TO", pipeline_id=pipeline["id"], metadata=edge_metadata,
                ),
                await self._get_or_create_edge(
                    session, org_id=org_id, from_asset_id=connector_asset.id, to_asset_id=raw_topic_asset.id,
                    edge_type="INGESTS_TO", pipeline_id=pipeline["id"], metadata=edge_metadata,
                ),
            ]
            if quality_asset is not None:
                edges.append(await self._get_or_create_edge(
                    session, org_id=org_id, from_asset_id=raw_topic_asset.id, to_asset_id=quality_asset.id,
                    edge_type="VALIDATED_BY", pipeline_id=pipeline["id"], metadata=edge_metadata,
                ))
                if quarantine_topic_asset is not None:
                    edges.append(await self._get_or_create_edge(
                        session, org_id=org_id, from_asset_id=quality_asset.id, to_asset_id=quarantine_topic_asset.id,
                        edge_type="QUARANTINES_TO", pipeline_id=pipeline["id"], metadata=edge_metadata,
                    ))
                if prepared_topic_asset is not None:
                    edges.append(await self._get_or_create_edge(
                        session, org_id=org_id, from_asset_id=quality_asset.id, to_asset_id=prepared_topic_asset.id,
                        edge_type="EMITS_TO", pipeline_id=pipeline["id"], metadata=edge_metadata,
                    ))
            await self._prune_stale_edges(session, org_id=org_id, pipeline_id=pipeline["id"], keep={e.id for e in edges})
            await session.commit()
            return {
                "nodes": [
                    _asset_to_dict(a)
                    for a in (
                        source_asset, connector_asset, raw_topic_asset, quality_asset, quarantine_topic_asset, prepared_topic_asset,
                    )
                    if a is not None
                ],
                "edges": [_edge_to_dict(e) for e in edges],
            }

    async def _quality_policy_of(self, pipeline: dict) -> dict | None:
        """The pipeline's quality policy, or None when it has none (or sits in the quarantine zone, which the quality worker never checks)."""
        if not pipeline.get("quality_policy_id") or pipeline.get("zone") == "quarantine":
            return None
        from app.services.quality_service import (
            quality_service,  # deferred: quality_service reaches back into pipelines
        )

        return await quality_service.get_policy(pipeline["org_id"], pipeline["quality_policy_id"])

    async def _prune_stale_edges(self, session: AsyncSession, *, org_id: str, pipeline_id: str, keep: set[str]) -> None:
        """Drop this pipeline's edges that the current configuration no longer produces (a quality policy that was removed, ENFORCE switched back
        to OBSERVE...), and any asset left with no edge at all that only a quality flow creates (processor, prepared and quarantine topics).
        Without this, get-or-create only ever ADDS, so an old deploy's processor and prepared topic stayed in the graph forever."""
        conditions = [LineageEdge.org_id == org_id, LineageEdge.pipeline_id == pipeline_id]
        if keep:
            conditions.append(LineageEdge.id.not_in(keep))
        stale = (await session.execute(select(LineageEdge).where(*conditions))).scalars().all()
        if not stale:
            return
        touched = {e.from_asset_id for e in stale} | {e.to_asset_id for e in stale}
        await session.execute(delete(LineageEdge).where(LineageEdge.id.in_([e.id for e in stale])))
        for asset in (await session.execute(select(Asset).where(Asset.org_id == org_id, Asset.id.in_(touched)))).scalars().all():
            zone = (asset.metadata_json or {}).get("zone")
            quality_only = asset.asset_type == "processor" or (asset.asset_type == "kafka_topic" and zone in ("prepared", "quarantine"))
            if not quality_only:
                continue
            remaining = (await session.execute(
                select(func.count()).select_from(LineageEdge).where(
                    LineageEdge.org_id == org_id,
                    (LineageEdge.from_asset_id == asset.id) | (LineageEdge.to_asset_id == asset.id),
                )
            )).scalar_one()
            if remaining == 0:
                await session.delete(asset)

    async def consume_runtime_event(self, event: dict) -> dict:
        org_id = event["org_id"]
        async with AsyncSessionLocal() as session:
            from_asset = await self._get_or_create_asset(
                session,
                org_id=org_id,
                asset_type=event["from_asset"]["asset_type"],
                name=event["from_asset"]["name"],
                qualified_name=event["from_asset"]["qualified_name"],
                metadata=event["from_asset"].get("metadata", {}),
            )
            to_asset = await self._get_or_create_asset(
                session,
                org_id=org_id,
                asset_type=event["to_asset"]["asset_type"],
                name=event["to_asset"]["name"],
                qualified_name=event["to_asset"]["qualified_name"],
                metadata=event["to_asset"].get("metadata", {}),
            )
            edge = await self._get_or_create_edge(
                session,
                org_id=org_id,
                from_asset_id=from_asset.id,
                to_asset_id=to_asset.id,
                edge_type=event["edge_type"],
                pipeline_id=event.get("pipeline_id"),
                metadata={
                    **event.get("metadata", {}),
                    "event_type": event.get("event_type"),
                    "connector_id": event.get("connector_id"),
                    "transform_policy_id": event.get("transform_policy_id"),
                    "schema_version_id": event.get("schema_version_id"),
                },
            )
            await session.commit()
            await emit_runtime_edge_lineage(event)
            return {
                "nodes": [_asset_to_dict(from_asset), _asset_to_dict(to_asset)],
                "edges": [_edge_to_dict(edge)],
            }

    async def _get_or_create_asset(
        self,
        session: AsyncSession,
        *,
        org_id: str,
        asset_type: str,
        name: str,
        qualified_name: str,
        metadata: dict,
    ) -> Asset:
        repo = AssetRepository(session)
        existing = await repo.get_by_qualified_name(org_id, qualified_name)
        if existing is not None:
            return await repo.update(
                existing, {"asset_type": asset_type, "name": name, "metadata_json": metadata}
            )
        values = {
            "org_id": org_id,
            "asset_type": asset_type,
            "name": name,
            "qualified_name": qualified_name,
            "metadata_json": metadata,
        }
        try:
            async with session.begin_nested():  # SAVEPOINT: a conflict here rolls back only this insert
                return await repo.create(values)
        except IntegrityError:
            # Lost a race to a concurrent materialize call for the same
            # (org_id, qualified_name) — the unique constraint caught it
            # instead of letting a duplicate row through. The winning row
            # exists now; fetch and update it instead.
            existing = await repo.get_by_qualified_name(org_id, qualified_name)
            if existing is None:
                raise
            return await repo.update(
                existing, {"asset_type": asset_type, "name": name, "metadata_json": metadata}
            )

    async def _get_or_create_edge(
        self,
        session: AsyncSession,
        *,
        org_id: str,
        from_asset_id: str,
        to_asset_id: str,
        edge_type: str,
        pipeline_id: str | None,
        metadata: dict,
    ) -> LineageEdge:
        repo = LineageEdgeRepository(session)
        existing = await repo.get_by_identity(org_id, from_asset_id, to_asset_id, edge_type, pipeline_id)
        if existing is not None:
            return existing  # an edge is an immutable fact once recorded — nothing to update
        values = {
            "org_id": org_id,
            "from_asset_id": from_asset_id,
            "to_asset_id": to_asset_id,
            "edge_type": edge_type,
            "pipeline_id": pipeline_id,
            "metadata_json": metadata,
        }
        try:
            async with session.begin_nested():
                return await repo.create(values)
        except IntegrityError:
            existing = await repo.get_by_identity(org_id, from_asset_id, to_asset_id, edge_type, pipeline_id)
            if existing is None:
                raise
            return existing


lineage_service = LineageService()
