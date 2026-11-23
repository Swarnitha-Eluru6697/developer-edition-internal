from typing import Any

from app.core.config import settings
from app.core.error_policies import DLQ_POLICIES, TOLERANCE, default_policy, is_sink
from app.schemas.pipeline import PipelineCreate
from app.services.secrets import mask_connection_config
from app.services.topic_naming import (
    TopicParts,
    build_connector_name,
    build_connector_quarantine_topic_name,
    build_topic_name,
    connector_name_for_class,
    validate_topic_parts,
)
from app.services.topic_policy import (
    resolve_topic_config,
    schema_strategy_converter_config,
    topic_creation_connect_config,
)
from app.services.transform_policy import generate_smt_config


# GAP-04: errors.tolerance used to be hard-coded to "all" here — and ONLY
# here, in this create-time PREVIEW builder. connect_config.build_connector,
# which builds the config actually POSTed to Kafka Connect at deploy time,
# never set errors.tolerance at all, so every real deployed connector
# silently ran under Kafka Connect's own default (errors.tolerance=none,
# i.e. fail-fast) regardless of what this preview claimed. error_handling_connect_config
# is now the ONE place either builder gets this from, so the two can't
# drift again — see connect_config.py's build_connector for the other caller.
def error_handling_connect_config(
    *, error_handling_policy: str, direction: str, domain: str, source_system: str, entity: str,
    version: str,
) -> dict[str, Any]:
    """The errors.* Kafka Connect config for a pipeline's error_handling_policy
    (see app/core/error_policies.py for the four policies and why two of them are sink-only).

    FAIL_FAST (errors.tolerance=none): the task fails on the first error —
    no DLQ config, since Connect never uses it once tolerance is "none".
    SKIP_AND_LOG (errors.tolerance=all, no DLQ): the bad record is skipped
    and logged — the only tolerant option for a SOURCE connector, which
    Kafka Connect gives no dead-letter queue.
    DLQ_AND_CONTINUE (errors.tolerance=all): a bad record is logged and
    routed to this pipeline's own connector-DLQ topic; the task keeps
    running. THRESHOLD: identical Connect-level config to DLQ_AND_CONTINUE
    — Connect has no native "N errors per window" concept — but
    app/services/error_threshold.py + runtime_reconciler.py separately
    count this pipeline's connector-DLQ topic volume every tick and pause
    it once its configured max_errors is exceeded within its window.

    An unrecognized policy FAILS CLOSED (errors.tolerance=none) — it must
    never silently become "tolerate everything". The DLQ keys are emitted
    only for a sink: a source connector doesn't define them.
    """
    tolerance = TOLERANCE.get(error_handling_policy, "none")
    config: dict[str, Any] = {
        "errors.tolerance": tolerance,
        "errors.log.enable": "true",
        "errors.log.include.messages": "true",
    }
    if error_handling_policy in DLQ_POLICIES and is_sink(direction):
        # Kafka Connect's OWN dead-letter queue — a connector-level failure
        # (bad serialization, a converter error, the connector erroring on
        # a malformed source message), never a DataConnect quality-rule
        # failure. GAP-03: kept on its own topic, distinct from
        # build_validation_quarantine_topic_name's, so monitoring can tell
        # the two failure classes apart.
        config["errors.deadletterqueue.topic.name"] = build_connector_quarantine_topic_name(
            domain=domain, source_system=source_system, entity=entity, version=version,
        )
        # Without this, Kafka Connect's DeadLetterQueueReporter falls back to ITS OWN
        # hardcoded default (3) for a topic it auto-creates on first use — completely
        # ignoring this platform's own configured topic_default_replication_factor,
        # the exact value every other topic this platform creates already respects (see
        # topic_policy.py's resolve_topic_config). On a single-broker cluster (any local
        # dev Kafka) that default fails outright with InvalidReplicationFactorException,
        # and the sink task never starts.
        config["errors.deadletterqueue.topic.replication.factor"] = str(settings.topic_default_replication_factor)
        config["errors.deadletterqueue.context.headers.enable"] = "true"
    return config


def make_topic_parts(pipeline: PipelineCreate) -> TopicParts:
    return TopicParts(
        zone=pipeline.zone,
        domain=pipeline.domain,
        source_system=pipeline.source_system,
        entity=pipeline.entity,
        mode=pipeline.mode,
        version=pipeline.version,
    )


async def generate_pipeline_config(
    *,
    org_id: str,
    pipeline: PipelineCreate,
    connector_template: dict[str, Any],
    connection_profile: dict[str, Any],
    transform_policy: dict[str, Any] | None = None,
    topic_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parts = make_topic_parts(pipeline)
    validate_topic_parts(parts, soft_limit=settings.topic_soft_limit)
    topic_name = build_topic_name(parts, soft_limit=settings.topic_soft_limit)
    connector_name = connector_name_for_class(build_connector_name(parts), connector_template.get("connector_class"))
    transforms = transform_policy.get("transforms", []) if transform_policy else []

    config: dict[str, Any] = {}
    config.update(connector_template.get("default_config", {}))
    # This preview is persisted on the pipeline row and returned by the API
    # (Pipeline.generated_config, GET .../generated-config) — never merge the
    # connection's raw connection_config here, only a redacted copy. The real
    # values are resolved transiently, at deploy time, by connect_config.py.
    config.update(mask_connection_config(connection_profile.get("connection_config", {})))
    config.update(pipeline.connector_config)
    config.update(generate_smt_config(transforms))
    config.update(error_handling_connect_config(
        error_handling_policy=pipeline.error_handling_policy or default_policy(connector_template),
        direction=connector_template.get("connector_type") or "",
        domain=pipeline.domain, source_system=pipeline.source_system,
        entity=pipeline.entity, version=pipeline.version,
    ))
    config.update(
        {
            "name": connector_name,
            "connector.class": connector_template["connector_class"],
        }
    )
    if connection_profile.get("secret_ref"):
        config["inai.secret.ref"] = connection_profile["secret_ref"]

    # `topics`/`topic.prefix` name what a SOURCE connector produces to, so they are this
    # pipeline's own derived topic_name. A SINK reads from a topic that must already exist —
    # the one the caller named in connector_config["topics"] — so it must never be overwritten
    # here: this used to unconditionally set `topics: topic_name` for every connector type,
    # which silently clobbered a sink's real upstream topic with its own (empty) name. Every
    # sink ever deployed through this path was consuming from the wrong topic as a result.
    #
    # Explicit topic provisioning — partitions/replication/retention/cleanup/min-ISR/
    # compression/max-message-size/schema-strategy — is also SOURCE-only for the same reason
    # (a sink never creates the topic it reads from). See app/services/topic_policy.py for the
    # pipeline-policy > domain-policy > platform-default resolution order.
    if connector_template.get("connector_type") == "source":
        config["topics"] = topic_name
        config["topic.prefix"] = topic_name
        resolved = await resolve_topic_config(org_id, pipeline.domain, topic_policy)
        config.update(topic_creation_connect_config(resolved))
        config.update(schema_strategy_converter_config(resolved))

    return {
        # GAP-12: a sink reads existing topics and generates none of its own — only a source gets a topic_name.
        "topic_name": None if connector_template.get("connector_type") == "sink" else topic_name,
        "connector_name": connector_name,
        "generated_config": config,
    }

