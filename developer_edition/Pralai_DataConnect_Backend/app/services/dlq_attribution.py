"""Which pipeline a record on a connector dead-letter topic belongs to (NEW-04).

A DLQ topic is named from domain / source system / entity / version only (app/services/topic_naming.py), so two sink pipelines that differ in
their zone or mode would write to ONE topic. Create-time validation refuses that for new pipelines (GAP-04), but a topic can still be shared
(pipelines created before that rule, rows edited outside the API), and the topic name alone then says nothing about whose failure a record is.
Treating the first pipeline as the owner of a shared topic filed every pipeline's failures under that one.

Kafka Connect says exactly whose a DLQ record is: with `errors.deadletterqueue.context.headers.enable=true` (set by pipeline_config) it stamps
`__connect.errors.connector.name` on every record. A pipeline's `connector_name` is that name, and it is unique within an organization, so a record
is attributed to a pipeline by that header — never by guessing from the topic.

The rules, in one place so the counter and the monitoring screen cannot disagree:

  * the header names one of this topic's pipelines          -> that pipeline
  * the header names a connector that is not one of them    -> nobody (a deleted pipeline's leftovers, or another connector entirely)
  * there is no header, and the topic has exactly ONE pipeline -> that pipeline (it can only be theirs; e.g. headers were switched off)
  * there is no header, and the topic is shared             -> nobody (it could be any of them; guessing is the bug this fixes)
"""

from collections.abc import Iterable
from typing import Any

from app.core.error_policies import DLQ_POLICIES
from app.services.topic_naming import build_connector_quarantine_topic_name

CONNECTOR_NAME_HEADER = "__connect.errors.connector.name"


def is_dlq_sink(pipeline: dict[str, Any]) -> bool:
    """Only a SINK pipeline whose policy keeps bad records on a dead-letter queue writes one — Kafka Connect gives a source connector none
    (app/core/error_policies.py) — and only once it has been deployed (has a connector name)."""
    return (
        bool(pipeline.get("connector_name"))
        and str(pipeline.get("direction") or "").upper() == "SINK"
        and pipeline.get("error_handling_policy") in DLQ_POLICIES
    )


def dlq_topic_for(pipeline: dict[str, Any]) -> str | None:
    """The connector-DLQ topic this pipeline writes, or None if its fields do not form a topic name."""
    try:
        return build_connector_quarantine_topic_name(
            domain=pipeline["domain"], source_system=pipeline["source_system"],
            entity=pipeline["entity"], version=pipeline["version"],
        )
    except (KeyError, ValueError):
        return None


def pipelines_by_dlq_topic(pipelines: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """{DLQ topic: EVERY dead-letter sink pipeline that writes it}, in the order given. More than one means the topic is shared."""
    topics: dict[str, list[dict[str, Any]]] = {}
    for pipeline in pipelines:
        if is_dlq_sink(pipeline) and (topic := dlq_topic_for(pipeline)):
            topics.setdefault(topic, []).append(pipeline)
    return topics


class DlqAttribution:
    """Maps the records of ONE DLQ topic to the pipelines that write it. `pipelines` are dicts with `id` and `connector_name`."""

    def __init__(self, pipelines: Iterable[dict[str, Any]]):
        self.by_connector: dict[str, str] = {}
        for pipeline in pipelines:
            self.by_connector.setdefault(str(pipeline["connector_name"]), str(pipeline["id"]))
        ids = set(self.by_connector.values())
        self.sole_pipeline_id: str | None = next(iter(ids)) if len(ids) == 1 else None

    def pipeline_id_for(self, headers: dict[str, str]) -> str | None:
        """The pipeline a record with these (decoded) headers belongs to, or None when it cannot be told."""
        connector = (headers.get(CONNECTOR_NAME_HEADER) or "").strip()
        if connector:
            return self.by_connector.get(connector)
        return self.sole_pipeline_id
