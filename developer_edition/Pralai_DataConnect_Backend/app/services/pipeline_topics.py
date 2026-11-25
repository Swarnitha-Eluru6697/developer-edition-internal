"""
Which topic a pipeline's data is on (GAP-12).

A SOURCE pipeline produces to a topic DataConnect generates for it: `topic_name`. A SINK produces nothing to Kafka — it
reads topics that already exist, named in `connector_config["topics"]` — so it has NO generated topic: `topic_name` is
null for a sink. Code that needs "the topic this pipeline's data is on" (quality results, lineage, the stream worker)
asks here rather than reading `topic_name`, so a sink resolves to what it actually reads instead of crashing on, or
worse inventing, an output topic.
"""

from typing import Any


def input_topics(pipeline: dict[str, Any]) -> list[str]:
    """The topics a sink reads, in order; empty for a source or a sink with none configured."""
    raw = (pipeline.get("connector_config") or {}).get("topics")
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list | tuple):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def is_sink_pipeline(pipeline: dict[str, Any]) -> bool:
    return (pipeline.get("direction") or "").upper() == "SINK"


def consumed_topics(pipeline: dict[str, Any]) -> list[str]:
    """EVERY topic a quality worker must read for this pipeline, in order, without repeats: a source's generated topic, or all of a sink's
    input topics (NEW-02). A sink may read several, and a check that covers only the first lets the rest through unchecked — so anything that
    decides what to CONSUME uses this, never `data_topic`."""
    topics = input_topics(pipeline) or [pipeline.get("topic_name")]     # a source has no `topics` setting, so it falls through to its own topic
    return list(dict.fromkeys(t for t in topics if t))


def data_topic(pipeline: dict[str, Any]) -> str | None:
    """A single topic that names this pipeline's data, for LABELS only (a result row, a message to the user): a source's generated topic, a
    sink's FIRST input topic. A sink with several inputs has more than one; never use this to decide what to read — see `consumed_topics`."""
    if is_sink_pipeline(pipeline):
        topics = input_topics(pipeline)
        return topics[0] if topics else None
    return pipeline.get("topic_name")
