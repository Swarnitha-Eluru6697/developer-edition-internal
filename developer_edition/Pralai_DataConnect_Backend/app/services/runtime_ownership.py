"""
Maps raw Kafka Connect / Kafka runtime objects (connector names, topic
names) back to the DataConnect pipeline that owns them, scoped to one org.

The Kafka Connect cluster and Kafka cluster monitoring reads from are shared
infrastructure — settings.kafka_connect_url/kafka_bootstrap point at one
cluster regardless of which org is asking, and that cluster may host
connectors/topics belonging to a completely different application, or to a
different DataConnect org. Every monitoring route (app/api/routes/monitoring.py)
must filter through these helpers before returning anything to a caller, so a
raw Connect/Kafka listing never becomes a cross-tenant (or cross-application)
data leak — this is also what lets a response map a bare connector/topic name
back to a `pipeline_id` for the UI's "consistent contract" (see
app/schemas/monitoring.py).

Pipelines are Postgres-authoritative (see PipelineService) — these helpers
go through it rather than the in-memory store.

Despite the name, this module has nothing to do with WHICH RECONCILER
PROCESS owns reconciling a given pipeline when more than one is running
(GAP-08) — that's app/services/runtime_reconciler.py's own claim/lease
mechanism (`_claim_rows`, `PipelineRuntimeStateRepository.claim_batch`).
This module is tenant-attribution only: which pipeline a raw Connect/Kafka
object belongs to, never which process is allowed to act on it.
"""

from typing import Any

from app.services.pipeline_service import pipeline_service
from app.services.topic_naming import (
    build_connector_quarantine_topic_name,
    to_prepared_topic,
    validation_quarantine_topic_for,
)


async def org_pipelines(org_id: str) -> list[dict[str, Any]]:
    return await pipeline_service.list_pipelines(org_id)


async def pipeline_by_connector_name(org_id: str) -> dict[str, dict[str, Any]]:
    """connector_name -> pipeline, for this org's own pipelines only. A
    connector name that isn't a key here either belongs to another org's
    DataConnect pipeline or to an entirely different application sharing
    the same Connect cluster — either way, not this caller's to see."""
    return {
        pipeline["connector_name"]: pipeline
        for pipeline in await org_pipelines(org_id)
        if pipeline.get("connector_name")
    }


async def pipeline_by_topic_name(org_id: str) -> dict[str, dict[str, Any]]:
    """Every topic name this org's pipelines own — the pipeline's own raw
    topic plus its derived prepared/quarantine topics — mapped back to the
    owning pipeline, so a bare topic name from Kafka can be attributed.

    Two distinct quarantine topics per pipeline now (GAP-03), not one: the
    Kafka Connect connector-level DLQ and the DataConnect quality-validation
    quarantine are different failure classes on different topics (see
    app/services/topic_naming.py) — both still belong to this pipeline, so
    both need an entry here, or one of the two would show up to monitoring
    as an unattributed/cross-tenant topic instead of this pipeline's own."""
    mapping: dict[str, dict[str, Any]] = {}
    for pipeline in await org_pipelines(org_id):
        # A SINK has no generated topic (GAP-12): it only reads topics other pipelines own, so it owns none of those —
        # attributing its input topic to it would claim another pipeline's topic. It still owns its own dead-letter and
        # quarantine topics below.
        topic_name = pipeline.get("topic_name")
        if topic_name:
            mapping[topic_name] = pipeline
            prepared_topic = to_prepared_topic(topic_name)
            if prepared_topic != topic_name:  # a raw-zone topic: its prepared counterpart belongs to this pipeline too
                mapping[prepared_topic] = pipeline
        try:
            quarantine_kwargs = {
                "domain": pipeline["domain"],
                "source_system": pipeline["source_system"],
                "entity": pipeline["entity"],
                "version": pipeline["version"],
            }
            mapping[validation_quarantine_topic_for(pipeline)] = pipeline
            mapping[build_connector_quarantine_topic_name(**quarantine_kwargs)] = pipeline
        except (KeyError, ValueError):
            # A pipeline missing one of these fields (shouldn't happen for a
            # real pipeline row) just doesn't get a quarantine-topic entry —
            # never a reason to fail the whole lookup for every pipeline.
            pass
    return mapping
