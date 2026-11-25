"""
Centralized referential validation for a pipeline specification.

A pipeline references several other org-scoped resources by id: connection
profile, transform policy, schema version, quality policy, and topic policy
(connector templates are the one exception — they're a global, platform-managed
catalog with no org_id, see app/models/catalog.py). Every one of those ids comes
straight from client input, so a stale id, an id copied from another org's
export, or an id belonging to a different org must never be silently
accepted — it must be rejected the same way a missing id would be.

This is the one place that resolves and validates all of them, so adding a
new org-scoped reference to the pipeline model means adding it here too,
rather than each caller re-implementing (and inevitably forgetting) its own
ownership check.

Connection profiles, transform policies, schema versions, quality policies,
and topic policies are all Postgres-authoritative (see their respective
services); this function goes through those services rather than any
in-memory store. Connector templates are the one exception to org-scoping
(not org ownership — they're just a global, platform-managed catalog with
no org_id), and are also read straight from Postgres via catalog_service.py.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.connect_pools import SINK_POOL_ID, canonical_pool_id
from app.core.error_policies import (
    DLQ_POLICIES,
    allowed_policies,
    default_policy,
    label,
    rejection_reason,
)
from app.core.errors import InvalidTopicNameError, ValidationError
from app.schemas.errors import FieldError
from app.schemas.pipeline import PipelineCreate
from app.services.connect_runtime import pool_for_template
from app.services.pipeline_topics import input_topics
from app.services.quality_mode import ENFORCE, build_prepared_topic, enforcement_problem
from app.services.sink_topics import (
    SinkTopicError,
    misplaced_config_keys,
    normalize_sink_topics,
)
from app.services.topic_naming import TopicParts

VALID_DIRECTIONS = {"SOURCE", "SINK"}


@dataclass
class PipelineReferences:
    template: dict[str, Any]
    connection: dict[str, Any]
    transform: dict[str, Any] | None
    schema: dict[str, Any] | None
    quality_policy: dict[str, Any] | None
    topic_policy: dict[str, Any] | None
    direction: str


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


def _validate_direction(template: dict[str, Any], payload: PipelineCreate, org_id: str) -> str:
    """GAP-12: a pipeline's direction (SOURCE/SINK) is derived from its
    resolved template's connector_type, never user-submitted — see
    Pipeline.direction. Kafka Connect already treats sources and sinks
    structurally differently (connect_config.py's _build_connector has a
    distinct branch per class, never shared), but nothing checked that a
    pipeline's own connector_config actually matched its direction's
    requirements until deploy time (_sink_topic() raising mid-build). This
    makes that check explicit and moves it up to creation/update time.

        SOURCE: source object required (entity, or connector_config.tables
                for multiple) — generated output (topic_name) required.
                Sink-only settings (`topics`, `topics.regex`) prohibited.
        SINK:   input topic required (connector_config.topics): literal topic
                names, each belonging to this organization (app/services/
                sink_topics.py — a sink must not be able to read another
                tenant's topic). A pipeline has no generated output topic of
                its own, and its generated name is never a valid substitute
                (connect_config.py's _sink_topic() only ever reads
                connector_config.topics, by design). Source-only settings
                (`topic.prefix`, `tables`, ...) prohibited.
    """
    direction = (template.get("connector_type") or "").upper()
    if direction not in VALID_DIRECTIONS:
        raise ValidationError(
            f"Connector template has an unrecognized connector_type: {template.get('connector_type')!r}."
        )
    connector_config = payload.connector_config or {}
    misplaced = misplaced_config_keys(direction, connector_config)
    if misplaced:
        other = "sink" if direction == "SOURCE" else "source"
        raise ValidationError(
            f"{direction.capitalize()} pipelines cannot set {', '.join(repr(k) for k in misplaced)} — "
            f"{'that setting belongs' if len(misplaced) == 1 else 'those settings belong'} to {other} connectors.",
            field_errors=[
                FieldError(field=f"connector_config.{key}", message=f"Not applicable to a {direction.lower()}.")
                for key in misplaced
            ],
        )
    if direction == "SOURCE":
        # Always satisfied today by `entity` being a required PipelineBase
        # field (connector_config.tables, when set, lists additional/
        # alternative source objects — see connect_config.py's
        # resolve_tables) — checked explicitly by name anyway, rather than
        # left as an incidental side effect of an unrelated field.
        if not payload.entity and not connector_config.get("tables"):
            raise ValidationError(
                "Source pipelines require a source object — set 'entity' or connector_config.tables."
            )
    else:  # SINK
        try:
            # Stored in its normalized form, so what is deployed is exactly what was validated.
            payload.connector_config = {
                **connector_config, "topics": normalize_sink_topics(connector_config.get("topics")),
            }
        except SinkTopicError as exc:
            # BUG-05: a typed code (INVALID_TOPIC_NAME), not just the generic validation_error.
            raise InvalidTopicNameError(
                str(exc),
                field_errors=[FieldError(field="connector_config.topics", message=str(exc))],
                details={"topic": exc.topic} if exc.topic else None,
            ) from exc
    return direction


def _resolve_pool(template: dict[str, Any], payload: PipelineCreate) -> str:
    """A pipeline's worker pool is DERIVED from its connector template (runtime design: one pool per workload — see
    app/core/connect_pools.py), never chosen by the client. A client that supplies one anyway must agree with it; the
    resolved pool is written onto the payload so it is stored with the pipeline."""
    try:
        pool_id = pool_for_template(template)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    # Design invariant: the sink pool holds every sink, and only sinks — a sink's write load and credentials must not share a
    # cluster with the sources that feed it.
    is_sink_template = (template.get("connector_type") or "").lower() == "sink"
    if is_sink_template != (pool_id == SINK_POOL_ID):
        raise ValidationError(
            f"Connector template is misconfigured: a {'sink' if is_sink_template else 'source'} connector cannot run in the "
            f"'{pool_id}' worker pool ({'sinks belong in' if is_sink_template else 'only sinks run in'} '{SINK_POOL_ID}')."
        )
    supplied = payload.runtime_pool_id
    if supplied and canonical_pool_id(supplied) != pool_id:
        raise ValidationError(
            f"This connector runs in the '{pool_id}' worker pool; a pipeline's pool is set by its connector and cannot be "
            f"'{supplied}'.",
            field_errors=[FieldError(field="runtime_pool_id", message=f"Fixed by the connector: {pool_id}.")],
        )
    payload.runtime_pool_id = pool_id
    return pool_id


def _validate_capabilities(template: dict[str, Any], payload: PipelineCreate) -> None:
    """FE-BUG-23: PipelineBase.validate_mode/validate_error_handling_policy
    (app/schemas/pipeline.py) only check `mode`/`error_handling_policy`
    against the global set of every value that exists anywhere — they have
    no template to check against, since Pydantic field validators run before
    any DB lookup. This is the first point in the create path where the
    resolved template is available, so it's where a value valid in general
    but unsupported by THIS template gets rejected, rather than only being
    caught later at deploy time (deploy_preflight.py's mode check) or never
    (no deploy-time check exists for DLQ at all).

    Deliberately not moved into deploy_preflight.py: a user should learn
    their choice is unsupported when they make it, not after everything
    else about the pipeline was already accepted.
    """
    supported_modes = template.get("supported_modes") or []
    if supported_modes and payload.mode not in supported_modes:
        raise ValidationError(
            f"This connector template does not support mode '{payload.mode}' "
            f"(supports {sorted(supported_modes)}).",
            field_errors=[FieldError(field="mode", message=f"Not supported by this connector — choose one of {sorted(supported_modes)}.")],
        )
    allowed = allowed_policies(template)
    if payload.error_handling_policy not in allowed:
        raise ValidationError(
            rejection_reason(payload.error_handling_policy, template),
            field_errors=[FieldError(
                field="error_handling_policy",
                message=f"Not available for this connector — choose one of {', '.join(label(p) for p in allowed)}.",
            )],
        )


def shared_dead_letter_error(other_name: str | None) -> ValidationError:
    """The one error for "another sink already owns this dead-letter queue" — raised by the up-front check AND by the database index when two
    concurrent creates both got past that check (NEW-08), so the loser of the race sees exactly what a sequential loser sees."""
    who = f"Pipeline {other_name!r}" if other_name else "Another sink pipeline"
    return ValidationError(
        f"{who} already uses this dead-letter queue (same domain, source system, entity and version), so errors from the two would "
        "be counted together. Change the entity or version, or choose a policy without a dead-letter queue.",
        field_errors=[FieldError(
            field="entity", message="Another sink pipeline already uses the dead-letter queue for this name.",
        )],
    )


async def _reject_shared_dead_letter_topic(org_id: str, payload: PipelineCreate, direction: str) -> None:
    """GAP-04: a pipeline's dead-letter topic is named from domain/source_system/entity/version only (the topic
    naming contract has no room for the zone or mode), so two SINK pipelines that differ only in zone or mode
    would write to ONE dead-letter topic — and the THRESHOLD policy, which counts that topic to decide whether to
    pause a pipeline, would count the other pipeline's errors as its own. Refuse the second one up front.
    (Sources never write a dead-letter topic — see app/core/error_policies.py — so only sinks can collide.)"""
    if direction != "SINK" or payload.error_handling_policy not in DLQ_POLICIES:
        return
    from app.services.pipeline_service import pipeline_service

    own_identity = (payload.zone, payload.mode, payload.domain, payload.source_system, payload.entity, payload.version)
    for other in await pipeline_service.list_pipelines(org_id):
        other_identity = (
            other.get("zone"), other.get("mode"), other.get("domain"), other.get("source_system"),
            other.get("entity"), other.get("version"),
        )
        if (
            other_identity != own_identity  # not this same pipeline (an edit re-validates itself)
            and (other.get("direction") or "").upper() == "SINK"
            and other.get("error_handling_policy") in DLQ_POLICIES
            and other_identity[2:] == own_identity[2:]
        ):
            raise shared_dead_letter_error(other.get("name"))


def _quality_mode_error(message: str) -> ValidationError:
    return ValidationError(message, field_errors=[FieldError(field="quality_mode", message=message)])


async def _validate_quality_mode(
    org_id: str, payload: PipelineCreate, direction: str, quality_policy: dict[str, Any] | None
) -> None:
    """NEW-01: ENFORCE only means something if the whole chain honours it, so it is checked in three places.

    1. The pipeline itself can be enforced (a policy that routes failures to quarantine, a zone the worker covers, a prepared topic that
       is not its own data or input topic) — quality_mode.enforcement_problem.
    2. A SINK may not read the data topic of an ENFORCE pipeline: that topic still carries the failing records, so reading it would
       quietly undo the enforcement. The error names the prepared topic to read instead.
    3. A SOURCE may not be switched to ENFORCE while a sink still reads its data topic, for the same reason from the other side.
    """
    pipeline = payload.model_dump() | {"org_id": org_id, "direction": direction}
    own_topic = (
        TopicParts(
            zone=payload.zone, domain=payload.domain, source_system=payload.source_system,
            entity=payload.entity, mode=payload.mode, version=payload.version,
        ).topic_name(soft_limit=settings.topic_soft_limit)
        if direction == "SOURCE" else None
    )
    if problem := enforcement_problem(pipeline, quality_policy, own_topic):
        raise _quality_mode_error(problem)

    from app.services.pipeline_service import pipeline_service

    others = [p for p in await pipeline_service.list_pipelines(org_id) if p.get("status") != "deleting"]
    if direction == "SINK":
        reads = set(input_topics(pipeline))
        for other in others:
            if other.get("quality_mode") == ENFORCE and other.get("topic_name") in reads:
                raise _quality_mode_error(
                    f"This sink reads {other['topic_name']!r}, the data topic of pipeline {other.get('name')!r}, which runs with quality "
                    f"ENFORCE — that topic still carries the records that failed. Read its prepared topic "
                    f"{build_prepared_topic(other)!r} instead."
                )
    elif payload.quality_mode == ENFORCE and own_topic:
        readers = sorted(
            str(p.get("name")) for p in others
            if (p.get("direction") or "").upper() == "SINK" and own_topic in input_topics(p)
        )
        if readers:
            raise _quality_mode_error(
                f"Cannot enforce quality here yet: sink pipeline(s) {', '.join(repr(r) for r in readers)} still read this pipeline's data "
                f"topic {own_topic!r}, which carries the failing records. Point them at the prepared topic "
                f"{build_prepared_topic(pipeline)!r} first."
            )


def _reject_inline_credentials(payload: PipelineCreate) -> None:
    """GAP-01: a credential typed into a pipeline's free-form connector_config would be stored in Postgres in
    clear, returned in every Pipeline response and copied into the audit log. Credentials live on the
    connection (encrypted secret store). The error names the offending keys, never their values."""
    from app.services.secrets import find_secret_shaped_entries

    offending = find_secret_shaped_entries(payload.connector_config or {})
    if offending:
        raise ValidationError(
            "A pipeline's connector_config must not contain credentials — store them on the connection "
            f"instead (offending: {', '.join(offending)}).",
            field_errors=[
                FieldError(
                    field=f"connector_config.{path}",
                    message="Credentials belong on the connection (encrypted secret store), not on the pipeline.",
                )
                for path in offending
            ],
        )


async def validate_pipeline_references(org_id: str, payload: PipelineCreate) -> PipelineReferences:
    """Resolve every reference on `payload` and confirm it exists and belongs
    to `org_id` (connector templates excepted — they're global). Raises a 404
    naming the specific reference that failed."""
    from app.services.catalog_service import catalog_service
    from app.services.connection_service import connection_service
    from app.services.quality_service import quality_service
    from app.services.schema_service import schema_service
    from app.services.topic_policy import topic_policy_service
    from app.services.transform_policy import source_transform_problems
    from app.services.transform_service import transform_service

    template = await catalog_service.get_template(payload.template_id)
    if not template:
        raise _not_found("Connector template not found")
    direction = _validate_direction(template, payload, org_id)
    _resolve_pool(template, payload)
    if payload.error_handling_policy is None:
        payload.error_handling_policy = default_policy(template)  # not chosen: the connector's own default
    _validate_capabilities(template, payload)
    await _reject_shared_dead_letter_topic(org_id, payload, direction)
    _reject_inline_credentials(payload)

    connection = await connection_service.get_connection(org_id, payload.connection_profile_id)
    if not connection:
        raise _not_found("Connection profile not found")

    transform = None
    if payload.transform_policy_id:
        transform = await transform_service.get_policy(org_id, payload.transform_policy_id)
        if not transform:
            raise _not_found("Transform policy not found")
        if direction == "SOURCE":
            refused = source_transform_problems(transform.get("transforms") or [])
            if refused:
                raise ValidationError(" ".join(refused), field_errors=[FieldError(field="transform_policy_id", message=" ".join(refused))])

    schema = None
    if payload.schema_version_id:
        schema = await schema_service.get_schema(org_id, payload.schema_version_id)
        if not schema:
            raise _not_found("Schema version not found")

    quality_policy = None
    if payload.quality_policy_id:
        quality_policy = await quality_service.get_policy(org_id, payload.quality_policy_id)
        if not quality_policy:
            raise _not_found("Quality policy not found")

    await _validate_quality_mode(org_id, payload, direction, quality_policy)

    topic_policy = None
    if payload.topic_policy_id:
        topic_policy = await topic_policy_service.get_policy(org_id, payload.topic_policy_id)
        if not topic_policy:
            raise _not_found("Topic policy not found")

    return PipelineReferences(
        template=template,
        connection=connection,
        transform=transform,
        schema=schema,
        quality_policy=quality_policy,
        topic_policy=topic_policy,
        direction=direction,
    )
