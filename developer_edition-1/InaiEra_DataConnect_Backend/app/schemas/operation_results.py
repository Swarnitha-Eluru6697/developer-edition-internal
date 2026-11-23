"""
Typed result contracts for each Operation.operation_type (GAP-11).

Enforced at the persistence boundary, not left to each callable: operation_runner.register_work refuses an operation
type that has no contract, and operation_runner.check_result checks every result against it (and against
settings.operation_result_max_bytes) before it is stored. The paragraphs below describe the contracts themselves.

Operation.result (app/models/operations.py) is a JSONB column that, before
this, accepted literally any JSON — nothing stopped a work callable from
persisting an unbounded object graph. The concrete example this fixes:
pipeline_deploy's result used to embed `runtime_response`, the RAW Kafka
Connect PUT response — itself an echo of the full connector config, in
principle unbounded for a connector with a long SMT chain — even though
nothing internal or in the frontend ever read that field (a caller that
wants the connector's real, current state calls GET /pipelines/{id}/status,
which asks Kafka Connect directly, rather than trusting a stale snapshot
from whenever this operation happened to run). Every work callable
registered with operation_runner.register_work now constructs one of the
models below before returning, so an oversized or malformed result fails
loudly (a Pydantic ValidationError) at the point it's produced, never
silently persisted.

pipeline_deploy/pause/resume/restart share one shape today
(PipelineLifecycleResult): PipelineDeployResult/PipelinePauseResult/
PipelineResumeResult/PipelineRestartResult are thin, field-for-field-
identical subclasses of it — pipeline_service.py constructs the specific
one for each action (self-documenting at the call site, and free to
diverge later if e.g. deploy grows a field the others never will), while
the OperationResult union below (what GET /operations/{id} documents in
its OpenAPI schema) uses the shared base: a consumer has no way to tell
PipelineDeployResult and PipelinePauseResult's *data* apart anyway (they're
structurally identical), so a Union listing all four would just be four
names for one shape.

connection_discover's result (ConnectionDiscoveryResult, GAP-10) is the
canonical example of "large results should be referenced, not embedded":
the discovered objects themselves live in DiscoveryObject rows, this is
only ever the id pointing at them.
"""

from typing import Any

from pydantic import BaseModel, ValidationError


class OperationResultInvalid(ValueError):
    """A work callable's side effects ALREADY HAPPENED but the result it then built did not satisfy its contract (GAP-04).

    Raised only by `build_result`, which callables call as their very last step. The runner treats it differently from any other
    exception: the operation's execution outcome is `succeeded` (a connector really was deployed), and its result outcome is
    `invalid`. A subclass of ValueError, like pydantic's own ValidationError, so a direct caller still sees what it always saw."""

    def __init__(self, model: type[BaseModel], errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{model.__name__} result is invalid")
        self.model_name = model.__name__
        self.errors = errors


def build_result(model: type[BaseModel], **fields: Any) -> dict[str, Any]:
    """Build and serialize a result in its contract's shape; OperationResultInvalid (not a bare pydantic error) if it does not fit,
    so the runner can tell "the action failed" from "the action worked but its result is malformed"."""
    try:
        return model(**fields).model_dump()
    except ValidationError as exc:
        raise OperationResultInvalid(model, exc.errors(include_url=False, include_input=False)) from exc


class PipelineRuntimeSummary(BaseModel):
    """Bounded, normalized runtime snapshot — deliberately NOT a passthrough
    of whatever Kafka Connect's own API happened to return. Also fixes a
    real pre-existing inconsistency: deploy's own inline runtime dict used
    a "state" key, while pause/resume/restart's (via
    RuntimeOrchestratorService) used "status" — a client reading `.state`
    off a pipeline_pause result's runtime got nothing. Every lifecycle
    operation normalizes into this one shape now."""

    state: str
    connector: str | None = None
    topic: str | None = None


class PipelineLifecycleResult(BaseModel):
    """The shared shape behind PipelineDeployResult/PipelinePauseResult/
    PipelineResumeResult/PipelineRestartResult below."""

    pipeline_id: str
    status: str
    connector_name: str
    topic_name: str | None = None  # null for a sink (GAP-12)
    runtime: PipelineRuntimeSummary


class PipelineDeployResult(PipelineLifecycleResult):
    """operation_type="pipeline_deploy"."""


class PipelinePauseResult(PipelineLifecycleResult):
    """operation_type="pipeline_pause"."""


class PipelineResumeResult(PipelineLifecycleResult):
    """operation_type="pipeline_resume"."""


class PipelineRestartResult(PipelineLifecycleResult):
    """operation_type="pipeline_restart"."""


class ConnectionDiscoveryResult(BaseModel):
    """operation_type="connection_discover" (GAP-10)."""

    discovery_run_id: str


# operation_type -> the model its result must satisfy — used at the point
# each work callable builds its result (the real enforcement point: nothing
# downstream currently reads Operation.result, see this module's own
# docstring, so validating here is what actually stops an unbounded/
# malformed result from ever being persisted, not a check applied after
# the fact when reading it back).
RESULT_MODEL_BY_OPERATION_TYPE: dict[str, type[BaseModel]] = {
    "pipeline_deploy": PipelineDeployResult,
    "pipeline_pause": PipelinePauseResult,
    "pipeline_resume": PipelineResumeResult,
    "pipeline_restart": PipelineRestartResult,
    "connection_discover": ConnectionDiscoveryResult,
}

# What GET /operations/{id} documents `result` as in the OpenAPI schema: each operation type's own contract, then a bare
# dict for a row persisted before its contract existed (read back as-is — see Operation's validator in
# app/schemas/operations.py, which picks the model from `operation_type`). Extra fields are ignored, never rejected, so
# reading an old operation is never a 500.
OperationResult = (
    PipelineDeployResult
    | PipelinePauseResult
    | PipelineResumeResult
    | PipelineRestartResult
    | ConnectionDiscoveryResult
    | dict[str, Any]
)
