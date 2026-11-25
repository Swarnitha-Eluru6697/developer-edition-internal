"""BUG-07: who may see a pipeline's generated connector config, enforced on EVERY response that carries it.

GET /pipelines/{id}/generated-config requires GENERATED_CONFIG_PERMISSION, but the same data — source topology,
connector class, Kafka topic and transform wiring, even after secret redaction — also rides along inside other
responses: the Pipeline object (list, get, create, update) and the audit log's before/after snapshots of pipeline
changes. Gating one door while the others stay open protects nothing, so those responses go through the helpers
below, which blank the field for a caller who does not hold the permission.
"""

from typing import Any

from gateway_auth import Principal

# The generated config is what a pipeline deploys, so it is visible to whoever may deploy pipelines.
GENERATED_CONFIG_PERMISSION = "dataconnect:pipelines:deploy"
_FIELD = "generated_config"


def may_view_generated_config(principal: Principal) -> bool:
    return principal.has(GENERATED_CONFIG_PERMISSION)


def pipeline_for(principal: Principal, pipeline: dict[str, Any]) -> dict[str, Any]:
    """The pipeline as `principal` may see it: generated_config emptied unless they may view it."""
    if may_view_generated_config(principal) or _FIELD not in pipeline:
        return pipeline
    return {**pipeline, _FIELD: {}}


def pipelines_for(principal: Principal, pipelines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if may_view_generated_config(principal):
        return pipelines
    return [pipeline_for(principal, p) for p in pipelines]


def audit_state_for(principal: Principal, state: dict[str, Any] | None) -> dict[str, Any] | None:
    """An audit before/after snapshot as `principal` may see it. Any snapshot that embeds a
    generated_config (a pipeline's) has it emptied; everything else is returned untouched."""
    if state is None or not isinstance(state, dict):
        return state
    return pipeline_for(principal, state)
