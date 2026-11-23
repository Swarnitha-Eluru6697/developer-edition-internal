"""
Sync DataConnect's pipeline lineage to the Unified Catalog Service (UCS) as
OpenLineage RunEvents, POSTed to UCS's `POST /api/v1/lineage/events`.

Only edges between dataset-shaped assets are forwarded — a "connector" or
"processor" node (see app/models/lineage.py's Asset docstring) is DataConnect
plumbing, not something Gravitino/UCS can resolve to a catalog object, so
sending one would just show up in UCS as an unresolvable dataset. "source",
"kafka_topic" and "sink" are the only asset_types this module ever emits.

UCS resolves an OpenLineage dataset's (namespace, name) to a Gravitino FQN
via its own admin-managed namespace-mapping table (the Lineage mapping page,
UCS's app/services/lineage_service.py::resolve_fqn). This module never tries
to guess UCS's internal catalog names; it emits a stable, deterministic
(namespace, name) per DataConnect asset and leaves translating that to a
real FQN to whoever administers the UCS catalog — exactly like any other
OpenLineage producer (dbt, Spark, Flink) would.

A source or sink dataset's namespace also names the system it lives in — the
connection's source_type: `dataconnect:sink:postgresql`,
`dataconnect:sink:aerospike`, ... (ol_dataset). With one namespace for every sink,
mapped to the Postgres catalog, every sink — an Aerospike or Redis one included —
resolved to a Postgres table of the same name, and showed up in UCS as writing into it.
Each system is now mapped (or deliberately left unmapped) on its own.

A pipeline is one OpenLineage job, named after the pipeline, plus `<name>:quality`
for its quality hop. Deleting or renaming a pipeline retires those jobs in UCS
(retire_pipeline_lineage → UCS `POST /api/v1/lineage/jobs/retire`); OpenLineage
itself has no "job deleted" event, so a deleted pipeline otherwise kept its edges.

Best-effort and non-blocking by design, gated by settings.ucs_lineage_sync_enabled
(same opt-in convention as runtime_deploy_enabled): a pipeline deploy must
never fail, and a runtime lineage event must never be dropped, just because
UCS happens to be unreachable. Every call here catches its own exceptions
and logs a warning rather than propagating.

Column-level lineage: a source pipeline's raw topic output additionally
carries a `columnLineage` facet (see `_source_output_facets`) built from the
pipeline's own registered SchemaVersion (dc_schema_versions — the same
fields the Schemas page shows, whether auto-introspected from the source
table or hand-registered) and, if attached, its TransformPolicy's MaskField
steps. UCS's Lineage tab column view and its masked-column sample redaction
both read directly off this facet — see
docs/OPENLINEAGE_INTEGRATION_GUIDE.md §1 and §9 in the UCS repo. Every field
is IDENTITY from the same-named upstream field unless a MaskField step names
it, matching what the connector's own Kafka Connect SMT chain actually does
to the data (MaskField runs inline at the connector, before either the raw
or prepared topic ever sees a value) — this module doesn't attempt to model
every other SMT type's effect on column shape (ExtractField, HoistField,
Flatten, RegexRouter, HeaderFrom, ...); MaskField's masking flag is the one
piece of column-level semantics UCS's own guide calls out as consuming.

Deliberately no `schema` facet: UCS's own quality pipeline
(app/integrations/openlineage.py::_extract_quality in the UCS repo) treats
any output dataset's `schema` facet as a quality-relevant schema-drift
signal and snapshots it for comparison against its own independently
Kafka-message-sampled schema — an unrelated feature this integration has no
business feeding, and doing so produces a bogus "columns removed" drift
warning on the topic's Overview page.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from uuid import uuid4

import structlog

from app.core.config import settings
from app.core.http_clients import get_client
from app.services.schema_service import schema_service
from app.services.transform_service import transform_service

logger = structlog.get_logger(__name__)

# The only DataConnect asset_types (see app/models/lineage.py) that represent
# addressable data rather than pipeline plumbing — see module docstring.
_DATASET_ASSET_TYPES = {"source", "kafka_topic", "sink"}


def ol_dataset(asset_type: str, name: str, system: str | None = None) -> dict[str, str]:
    """A source or sink dataset carries its system (see the module docstring); a topic's namespace has none. The namespace names no
    organization: UCS keeps every event by the tenant DataConnect sends with it (X-Tenant-Id)."""
    namespace = f"dataconnect:{asset_type}"
    if system and asset_type in ("source", "sink"):
        namespace = f"{namespace}:{system}"
    return {"namespace": namespace, "name": name}


async def _connection_system(org_id: str, connection_profile_id: str | None) -> str:
    """The system a pipeline's source or sink lives in: its connection's source_type (postgresql, mysql, s3, ...), "unknown" without one."""
    from app.services.connection_service import (
        connection_service,  # imported here: connection_service's own imports reach this module
    )

    connection = await connection_service.get_connection(org_id, connection_profile_id) if connection_profile_id else None
    system = re.sub(r"[^a-z0-9_]+", "_", str((connection or {}).get("source_type") or "").strip().lower()).strip("_")
    return system or "unknown"


def lineage_job_names(pipeline: dict[str, Any]) -> list[str]:
    """Every job name a pipeline has had in UCS: its own, its quality hop's, and its id (the runtime edges' job name before they used
    the pipeline's name)."""
    return [pipeline["name"], f"{pipeline['name']}:quality", str(pipeline["id"])]


async def retire_pipeline_lineage(org_id: str, job_names: list[str]) -> None:
    """Tell UCS these jobs are gone (pipeline deleted, or renamed: its old name): their edges are retired and their column lineage dropped.
    Best effort like every call here."""
    if not settings.ucs_lineage_sync_enabled or not job_names:
        return
    try:
        client = await get_client(settings.ucs_base_url)
        resp = await client.post(
            "/api/v1/lineage/jobs/retire",
            json={"namespace": "dataconnect", "names": job_names},
            headers=ucs_service_headers(org_id, LINEAGE_EVENTS_PERMISSION),
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a UCS outage must never fail a delete
        logger.warning("ucs_lineage_sync.retire_failed", jobs=job_names, error=str(exc))


async def _masked_field_names(org_id: str, transform_policy_id: str | None) -> set[str]:
    """Field names named by a MaskField step's `config.fields` (comma-
    separated, same convention the frontend's transform builder writes) —
    see module docstring on why MaskField is the one SMT this module gives
    column-lineage semantics to."""
    if not transform_policy_id:
        return set()
    policy = await transform_service.get_policy(org_id, transform_policy_id)
    if not policy:
        return set()
    masked: set[str] = set()
    for step in policy.get("transforms") or []:
        if step.get("type") != "MaskField":
            continue
        raw = (step.get("config") or {}).get("fields") or ""
        masked.update(name.strip() for name in raw.split(",") if name.strip())
    return masked


async def _schema_fields(org_id: str, schema_version_id: str | None) -> list[dict[str, Any]]:
    if not schema_version_id:
        return []
    schema = await schema_service.get_schema(org_id, schema_version_id)
    if not schema:
        return []
    return ((schema.get("schema_definition") or {}).get("fields")) or []


def _source_output_facets(fields: list[dict[str, Any]], input_dataset: dict[str, str],
                          masked: set[str]) -> dict[str, Any] | None:
    """`columnLineage` facet for a source pipeline's first-hop output, built
    from its registered SchemaVersion's field list — None if that schema has
    no fields (autocreate_schema_from_source didn't run, the source wasn't
    reachable, or the pipeline has no schema_version_id at all)."""
    if not fields:
        return None

    column_fields: dict[str, Any] = {}
    for field in fields:
        name = field.get("name")
        if not name:
            continue
        is_masked = name in masked
        column_fields[name] = {
            "inputFields": [
                {
                    **input_dataset,
                    "field": name,
                    "transformations": [
                        {"type": "MASK" if is_masked else "IDENTITY", "masking": is_masked}
                    ],
                }
            ],
        }
    # Only the columnLineage facet is sent — a `schema` facet here would also
    # be picked up by UCS's unrelated quality pipeline (see
    # app/integrations/openlineage.py::_extract_quality, which feeds any
    # output dataset's `schema` facet into quality_service.check_schema_drift)
    # and get compared against UCS's own independently-sampled Kafka message
    # schema, producing a bogus "columns removed" drift warning that has
    # nothing to do with this integration.
    return {"columnLineage": {"fields": column_fields}}


# What this service-to-service caller may do in UCS (UCS authorises on permissions, contract
# docs/auth/01-CONTRACT.md): sent as x-user-permissions, never a role header.
LINEAGE_EVENTS_PERMISSION = "ucs:lineage-events:create"
QUALITY_RESULTS_PERMISSION = "ucs:quality-results:create"


def ucs_service_headers(org_id: str, permission: str) -> dict[str, str]:
    """Headers for a call to UCS without Envoy in front: UCS's gateway secret, a service identity, the tenant and
    exactly the one permission the call needs (base64 JSON list, the way Envoy renders a list claim)."""
    return {
        "X-Tenant-Id": org_id,
        "X-User-Name": settings.ucs_lineage_sync_user,
        "X-User-Groups": "dataconnect",
        "X-User-Permissions": base64.b64encode(json.dumps([permission]).encode()).decode(),
        "X-Inaiera-Gateway-Secret": settings.ucs_gateway_secret,
    }


async def send_ol_event(org_id: str, event: dict[str, Any], permission: str = LINEAGE_EVENTS_PERMISSION) -> None:
    try:
        client = await get_client(settings.ucs_base_url)
        resp = await client.post(
            "/api/v1/lineage/events",
            json=event,
            headers=ucs_service_headers(org_id, permission),
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a UCS outage must never break a pipeline
        logger.warning(
            "ucs_lineage_sync.failed",
            job=event.get("job", {}).get("name"),
            run_id=event.get("run", {}).get("runId"),
            error=str(exc),
        )


async def emit_deploy_lineage(pipeline: dict[str, Any]) -> None:
    """Fired once per pipeline deploy — the static, config-time lineage.

    Single hop, exactly the physical Kafka Connect data flow, quality_policy_id
    attached or not — a quality policy never changes what topic a connector
    actually reads or writes (see app/services/quality_stream_worker.py's own
    docstring: it's a read-only observer, not a router):
      - Source: the source table feeds this pipeline's own topic directly.
      - Sink: the topic it reads from (an existing topic it never creates)
        feeds the destination it writes to.

    A pipeline is a sink iff its own connector_config carries "topics" —
    the same signal app/services/connect_config.py's _sink_topic() already
    relies on for every sink connector type (JDBC, Mongo, S3, GCS, ADLS,
    file). A source's connector_config carries "tables" instead; it never
    has a real input topic to point at. This is checked here, not passed in
    by the caller, because pipeline_service.deploy() only computes its own
    source/sink distinction (the `topic` variable from build_connector())
    inside a runtime_deploy_enabled-gated block that this function must work
    without.
    """
    if not settings.ucs_lineage_sync_enabled:
        return
    org_id = pipeline["org_id"]
    # The pipeline's own entity — its source/destination table, never
    # client-editable, always present, see app/schemas/pipeline.py — not the
    # connection's display name. Two problems this fixes together:
    #
    # 1. Two pipelines sharing one connection profile (one Postgres
    #    connection, several destination tables is a common setup) used to
    #    both resolve to the exact same UCS dataset when named by connection
    #    alone — e.g. every sink pipeline through that connection collapsed
    #    onto "dataconnect:sink"/"<connection name>". UCS's lineage
    #    graph walks by dataset id, not by job, so only ONE of several real
    #    sinks ever showed up as a topic's downstream consumer.
    #
    # 2. The bare entity name is also the pipeline's REAL destination/source
    #    table name (connect_config.py's JDBC sink sets
    #    "table.name.format": entity; a JDBC source's own entity is its
    #    table). UCS's admin-configured lineage namespace mapping for
    #    "dataconnect:source"/"dataconnect:sink" resolves this
    #    name under the real Postgres catalog + schema those tables actually
    #    live in (e.g. dataconnect_postgres.public.<entity>) — the SAME FQN
    #    Gravitino itself uses for that table. Prefixing with the connection
    #    name (as this used to, to fix problem 1) broke that: the resulting
    #    FQN pointed at a synthetic node nothing else in UCS ever
    #    recognized, so a table's own Explore-browsed profile page could
    #    never show the lineage/quality UCS had actually recorded for it.
    #    The entity alone already fixes problem 1 too, as long as one
    #    org's traffic maps to one catalog (this platform's actual setup;
    #    see docs/OPENLINEAGE_INTEGRATION_GUIDE.md) — two DIFFERENT
    #    connections both happening to have a same-named table would still
    #    collide, but that's a pre-existing limit of mapping a whole org's
    #    source/sink traffic to one shared catalog, not something this
    #    introduces.
    asset_name = pipeline["entity"]
    input_topic = (pipeline.get("connector_config") or {}).get("topics")
    # A fresh runId per deploy call (not a fixed deploy-{pipeline_id}) so UCS's
    # own column-lineage dedup — which supersedes a dataset's old column edges
    # only when the new event's runId differs from the ones already stored —
    # actually clears stale edges on redeploy instead of piling up duplicates
    # forever under the same runId.
    run_id = f"deploy-{pipeline['id']}-{uuid4()}"
    system = await _connection_system(org_id, pipeline.get("connection_profile_id"))

    if input_topic:
        # The configured input, also for an ENFORCE sink: its quality gate hands passing records to its own connector through an internal
        # topic (quality_mode.SINK_HANDOFF_PREFIX) that is not part of the data's lineage — the sink reads its input and writes the table;
        # its failures are the quality hop's (_emit_quality_lineage).
        event = {
            "eventType": "COMPLETE",
            "job": {"namespace": "dataconnect", "name": pipeline["name"]},
            "run": {"runId": run_id},
            "inputs": [ol_dataset("kafka_topic", input_topic)],
            "outputs": [ol_dataset("sink", asset_name, system), *_dead_letter_outputs(pipeline)],
        }
    else:
        input_dataset = ol_dataset("source", asset_name, system)
        output = ol_dataset("kafka_topic", pipeline["topic_name"])
        fields = await _schema_fields(org_id, pipeline.get("schema_version_id"))
        masked = await _masked_field_names(org_id, pipeline.get("transform_policy_id"))
        facets = _source_output_facets(fields, input_dataset, masked)
        if facets:
            output = {**output, "facets": facets}
        event = {
            "eventType": "COMPLETE",
            "job": {"namespace": "dataconnect", "name": pipeline["name"]},
            "run": {"runId": run_id},
            "inputs": [input_dataset],
            "outputs": [output],
        }
    await send_ol_event(org_id, event)
    try:
        await _emit_quality_lineage(pipeline)
    except Exception:  # noqa: BLE001 — like the UCS call itself, this must never fail a deploy
        logger.warning("ucs_lineage_sync.quality_hop_failed", pipeline_id=pipeline.get("id"), exc_info=True)


def _dead_letter_outputs(pipeline: dict[str, Any]) -> list[dict[str, str]]:
    """A sink connector with a dead-letter queue writes a record it cannot process there (Kafka Connect creates the topic when the task
    starts — DeadLetterQueueReporter), so the topic is one of the sink's outputs: the one its failures go to. Read from the config the
    connector actually runs with."""
    topic = (pipeline.get("generated_config") or {}).get("errors.deadletterqueue.topic.name")
    return [ol_dataset("kafka_topic", topic)] if topic else []


async def _emit_quality_lineage(pipeline: dict[str, Any]) -> None:
    """The quality hop of a pipeline with a quality policy, as its own job `<pipeline>:quality`: the topics the quality stream worker reads,
    feeding the topics it writes — the validation quarantine topic (failing records) and, in ENFORCE, the prepared topic (passing records).
    The same decision the worker makes (quality_stream_worker._quarantine / _forward_to_prepared), so the graph shows where a failing record
    actually goes. Without it a topic's quarantine topic was in DataConnect's own lineage (QUARANTINES_TO) but never in UCS."""
    policy_id = pipeline.get("quality_policy_id")
    if not policy_id or pipeline.get("zone") == "quarantine":
        return
    # Imported here: quality_service imports ucs_quality_sync, which imports this module.
    from app.services.pipeline_topics import consumed_topics
    from app.services.quality_mode import is_enforcing, is_sink_handoff, prepared_topic_for
    from app.services.quality_service import quality_service
    from app.services.topic_naming import validation_quarantine_topic_for

    org_id = pipeline["org_id"]
    policy = await quality_service.get_policy(org_id, policy_id)
    inputs = consumed_topics(pipeline)
    if not policy or not inputs:
        return
    outputs: list[str] = []
    routes_to_quarantine = (policy.get("on_failure", "route_to_quarantine") == "route_to_quarantine"
                            and policy.get("quarantine_sink", "kafka") == "kafka")
    if is_enforcing(pipeline) or routes_to_quarantine:
        outputs.append(validation_quarantine_topic_for(pipeline))
    prepared = prepared_topic_for(pipeline)
    if prepared and not is_sink_handoff(prepared):   # a sink's hand-off is internal, not an output of the data
        outputs.append(prepared)
    if not outputs:
        return
    await send_ol_event(org_id, {
        "eventType": "COMPLETE",
        "job": {"namespace": "dataconnect", "name": f"{pipeline['name']}:quality"},
        # "dq-" keeps it within UCS's 80-character run id (ol_run.run_id); the plain deploy run id already uses all 80.
        "run": {"runId": f"dq-{pipeline['id']}-{uuid4()}"},
        "inputs": [ol_dataset("kafka_topic", t) for t in inputs],
        "outputs": [ol_dataset("kafka_topic", t) for t in outputs],
    })


async def emit_runtime_edge_lineage(event: dict[str, Any]) -> None:
    """Fired for every runtime edge DataConnect's Kafka-consumed lineage
    stream records (app/services/lineage_event_consumer.py) whose endpoints
    are both dataset-shaped — most commonly a sink write (WRITTEN_TO). Edges
    touching a connector or processor node are skipped; UCS has no catalog
    object those could ever resolve to."""
    if not settings.ucs_lineage_sync_enabled:
        return
    from_asset, to_asset = event["from_asset"], event["to_asset"]
    if (
        from_asset["asset_type"] not in _DATASET_ASSET_TYPES
        or to_asset["asset_type"] not in _DATASET_ASSET_TYPES
    ):
        return
    org_id = event["org_id"]
    run_id = event.get("run_id") or f"runtime-{uuid4()}"
    # The same job and the same datasets as the pipeline's deploy lineage: a source or sink is the pipeline's own table (entity) in its
    # connection's system — not the asset's display name (for a source, the CONNECTION's name) — and the job is the pipeline's name, so
    # a delete retires it too. Without a known pipeline, the edge is forwarded as it comes.
    pipeline = await _pipeline_for(org_id, event.get("pipeline_id"))
    system = await _connection_system(org_id, pipeline.get("connection_profile_id")) if pipeline else None

    def _dataset(asset: dict[str, Any]) -> dict[str, str]:
        if pipeline and asset["asset_type"] in ("source", "sink"):
            return ol_dataset(asset["asset_type"], pipeline["entity"], system)
        return ol_dataset(asset["asset_type"], asset["name"])

    ol_event = {
        "eventType": "COMPLETE",
        "job": {
            "namespace": "dataconnect",
            "name": (pipeline or {}).get("name") or event.get("pipeline_id") or event.get("connector_id") or "dataconnect-runtime",
        },
        "run": {"runId": str(run_id)},
        "inputs": [_dataset(from_asset)],
        "outputs": [_dataset(to_asset)],
    }
    await send_ol_event(org_id, ol_event)


async def _pipeline_for(org_id: str, pipeline_id: str | None) -> dict[str, Any] | None:
    if not pipeline_id:
        return None
    from app.services.pipeline_service import (
        pipeline_service,  # imported here: pipeline_service imports this module
    )

    try:
        return await pipeline_service.get_or_404(org_id, pipeline_id)
    except Exception:  # noqa: BLE001 — a deleted or unreadable pipeline: forward the edge as it comes
        return None
