"""
How a quality policy is applied to a pipeline's stream: OBSERVE or ENFORCE (NEW-01).

    OBSERVE   (the default, and what every pipeline did before this existed)
              The quality worker reads the pipeline's own topic, evaluates each record, keeps the statistics and copies a failing record
              to the validation quarantine topic. It writes nothing else: the data topic is untouched, so EVERY record — failing ones
              included — still reaches every consumer of that topic. Quarantine is a record of what failed, not a barrier.

    ENFORCE   The worker is a gate. A record that passes is written to the pipeline's PREPARED topic; a record that fails is written
              to quarantine and nowhere else. Consumers that must only ever see valid data read the prepared topic, never the data
              topic: a SINK pipeline in ENFORCE mode reads it by construction (connect_config._sink_topic), and a sink pipeline may
              not read the data topic of an ENFORCE pipeline at all (pipeline_spec_validator).

The prepared topic of a pipeline is its own seven-part name with the zone replaced by "prepared"
(`org_1001-prepared-erp-sap-customers-cdc-v1`). It comes from the pipeline's OWN fields, so it is unique to the pipeline: two pipelines
cannot share the same domain, source system, entity, mode and version, because their connector names would then collide.

BLOCK_ON_FAILURE (stop the stream when a record fails) is deliberately not a mode here. It is a different policy with different
consequences, and would be its own decision.
"""

from typing import Any

from app.services.pipeline_topics import input_topics, is_sink_pipeline
from app.services.topic_naming import TopicParts

OBSERVE = "OBSERVE"
ENFORCE = "ENFORCE"
QUALITY_MODES = frozenset({OBSERVE, ENFORCE})
DEFAULT_QUALITY_MODE = OBSERVE


def is_enforcing(pipeline: dict[str, Any]) -> bool:
    return pipeline.get("quality_mode") == ENFORCE


# An ENFORCE SINK's passing records are handed from its quality worker to its own connector through this topic. It is not a data topic:
# nobody else reads it, the "_" system-topic prefix keeps it out of the catalog (UCS Explore hides "_" topics; Gravitino lists no name
# with a dot), lineage draws the sink as input -> table, and the worker trims what the connector has written (trim_consumed_records).
SINK_HANDOFF_PREFIX = "_dataconnect.gate."


def is_sink_handoff(topic: str | None) -> bool:
    return bool(topic) and topic.startswith(SINK_HANDOFF_PREFIX)


def build_prepared_topic(pipeline: dict[str, Any]) -> str:
    """The prepared topic for this pipeline's fields, whether or not it is enforcing. Needs domain, source_system, entity,
    mode and version (zone is replaced, so it is not read). A SOURCE's is a real data topic other pipelines read; a SINK's (direction
    "SINK") is its internal hand-off, see SINK_HANDOFF_PREFIX."""
    if is_sink_pipeline(pipeline):
        return (f"{SINK_HANDOFF_PREFIX}{pipeline['domain']}-{pipeline['source_system']}-{pipeline['entity']}"
                f"-{pipeline['mode']}-{pipeline['version']}")
    parts = TopicParts(
        zone="prepared", domain=pipeline["domain"], source_system=pipeline["source_system"],
        entity=pipeline["entity"], mode=pipeline["mode"], version=pipeline["version"],
    )
    return parts.topic_name()


def prepared_topic_for(pipeline: dict[str, Any]) -> str | None:
    """The topic an ENFORCE pipeline's passing records are written to; None for an OBSERVE pipeline (it has none)."""
    return build_prepared_topic(pipeline) if is_enforcing(pipeline) else None


def quality_consumer_group(pipeline: dict[str, Any]) -> str:
    """The Kafka consumer group the pipeline's quality worker reads its input with (quality_stream_worker.py). Its committed offsets are how
    far the worker has got: for an ENFORCE sink, what it has not reached yet is backlog the sink cannot write (runtime_metrics.sink_backlogs)."""
    return f"inai-quality-{pipeline['id']}"


def enforcement_problem(pipeline: dict[str, Any], policy: dict[str, Any] | None, own_topic: str | None) -> str | None:
    """Why this pipeline cannot run in ENFORCE mode, or None if it can. `own_topic` is the topic a SOURCE pipeline writes (its
    data topic); a sink has none. Checked when a pipeline is created or edited, so a pipeline that could not be enforced is refused
    up front instead of quietly not enforcing."""
    if not is_enforcing(pipeline):
        return None
    if not pipeline.get("quality_policy_id") or policy is None:
        return "ENFORCE needs a quality policy: it is the policy's rules that decide which records pass."
    if policy.get("on_failure", "route_to_quarantine") != "route_to_quarantine":
        return (
            f"ENFORCE needs the policy to route failures to quarantine (on_failure=route_to_quarantine), but this policy's "
            f"on_failure is {policy.get('on_failure')!r}. A failing record has to go somewhere other than the prepared topic, and "
            "stopping the stream on a failure (block) is a separate policy that is not available."
        )
    if policy.get("quarantine_sink", "kafka") != "kafka":
        return f"ENFORCE needs failures quarantined to Kafka; this policy uses {policy.get('quarantine_sink')!r}."
    if pipeline.get("zone") == "quarantine":
        return "A pipeline in the quarantine zone is not checked by the quality worker, so it cannot be ENFORCE."
    prepared = build_prepared_topic(pipeline)
    reads = set(input_topics(pipeline)) if is_sink_pipeline(pipeline) else set()
    if prepared == own_topic or prepared in reads:
        return (
            f"The prepared topic {prepared!r} is this pipeline's own "
            f"{'input' if prepared in reads else 'data'} topic, so enforcing would write the validated records back onto it. "
            "Use a zone other than 'prepared', or a different input topic."
        )
    return None
