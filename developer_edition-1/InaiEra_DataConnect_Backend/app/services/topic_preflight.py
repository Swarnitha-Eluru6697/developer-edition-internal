"""
Can this pipeline's topic be created on THIS Kafka cluster? (GAP-06)

Asked before anything issues a create request — readiness, deploy, the reconciler — whether or not explicit topic provisioning is
switched on (with it off, Kafka Connect creates the topic itself from the same settings and fails after the fact). The answer is
only needed when the topic is about to be CREATED: a topic that already exists is left alone (its replication cannot be changed
here anyway), so it is never blocked by what a policy says now.

Gated by settings.topic_feasibility_check_enabled.
"""

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.errors import TopicReplicationFactorError
from app.services.kafka_cluster import (
    PolicyProblem,
    broker_count,
    topic_exists,
    topic_policy_problems,
)
from app.services.pipeline_topics import is_sink_pipeline
from app.services.topic_policy import resolve_topic_config


@dataclass
class TopicPreflight:
    applicable: bool  # False: a sink (creates no topic), no topic yet, or the check is switched off
    topic: str | None = None
    resolved: dict[str, Any] = field(default_factory=dict)
    brokers: int | None = None  # None = could not be determined
    topic_exists: bool | None = None
    problems: list[PolicyProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


async def evaluate_topic_policy(org_id: str, pipeline: dict[str, Any], topic_policy: dict[str, Any] | None) -> TopicPreflight:
    topic = pipeline.get("topic_name")
    if not settings.topic_feasibility_check_enabled or is_sink_pipeline(pipeline) or not topic:
        return TopicPreflight(applicable=False, topic=topic)
    resolved = await resolve_topic_config(org_id, pipeline["domain"], topic_policy)
    exists = await topic_exists(topic)
    if exists:
        return TopicPreflight(applicable=True, topic=topic, resolved=resolved, topic_exists=True)
    brokers = await broker_count()
    return TopicPreflight(
        applicable=True, topic=topic, resolved=resolved, brokers=brokers, topic_exists=exists,
        problems=topic_policy_problems(resolved, brokers),
    )


async def assert_topic_policy_feasible(org_id: str, pipeline: dict[str, Any], topic_policy: dict[str, Any] | None) -> None:
    """Raise TopicReplicationFactorError (422, not retryable — the policy or the cluster has to change) if the topic cannot be
    created on this cluster."""
    result = await evaluate_topic_policy(org_id, pipeline, topic_policy)
    if result.ok:
        return
    raise TopicReplicationFactorError(
        f"Cannot create topic '{result.topic}': " + " ".join(p.message for p in result.problems),
        details={
            "topic": result.topic,
            "requested_replication_factor": result.resolved.get("replication_factor"),
            "min_insync_replicas": result.resolved.get("min_insync_replicas"),
            "broker_count": result.brokers,
            "problems": [p.code for p in result.problems],
        },
    )
