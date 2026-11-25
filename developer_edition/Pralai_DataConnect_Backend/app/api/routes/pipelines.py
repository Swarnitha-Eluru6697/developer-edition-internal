"""Pipeline routes — all operations are org-scoped."""

from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from gateway_auth import Principal

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.config import settings
from app.core.permissions import require
from app.core.response_filters import pipeline_for, pipelines_for
from app.core.tenant import get_org_id
from app.schemas.operations import Operation
from app.schemas.pipeline import (
    DeployReadiness,
    Pipeline,
    PipelineCreate,
    PipelineStatus,
    PipelineUpdate,
    TopicNamePreview,
)
from app.services import operation_runner
from app.services.pipeline_service import pipeline_service
from app.services.secrets import redact_connector_config
from app.services.topic_naming import (
    TopicParts,
    build_topic_name,
    validate_topic_parts,
)

router = APIRouter()
logger = structlog.get_logger(__name__)

# Registers how the operation worker (app/services/operation_runner.py)
# rebuilds each lifecycle operation's work from just (org_id, pipeline_id) —
# needed so a deploy/pause/resume/restart submitted by one process can still
# be claimed and run by another (or the same process after a restart).
_LIFECYCLE_METHODS = {
    "pipeline_deploy": "deploy",
    "pipeline_pause": "pause",
    "pipeline_resume": "resume",
    "pipeline_restart": "restart",
}


def _lifecycle_work(method_name: str, org_id: str, pipeline_id: str):
    async def _run() -> dict:
        try:
            return await getattr(pipeline_service, method_name)(org_id, pipeline_id)
        except Exception:
            # FE-BUG-25 follow-up: _submit_lifecycle_operation atomically
            # claims the pipeline (status -> a transitional value) BEFORE
            # this operation ever runs, specifically so a duplicate
            # submission has nothing left to claim. But that means if THIS
            # operation then fails — e.g. deploy_preflight rejecting an
            # incompatible Kafka Connect worker — nothing else was ever
            # going to move the pipeline off that transitional status: it
            # stays "deploying" forever, and the SAME claim guard that
            # blocks a duplicate submission now also blocks every future
            # legitimate retry (only "deleting" is reachable from
            # "deploying" — see VALID_TRANSITIONS). Releasing the claim
            # here, on failure, is what turns "permanently stuck" back into
            # "failed, and retryable" — the operation itself still fails
            # and is still recorded as failed below; this only fixes what
            # happens to the pipeline record, not the operation record.
            await pipeline_service.release_claim_on_failure(org_id, pipeline_id)
            raise

    return _run


for _operation_type, _method_name in _LIFECYCLE_METHODS.items():
    operation_runner.register_work(
        _operation_type,
        lambda org_id, pipeline_id, _method_name=_method_name: _lifecycle_work(_method_name, org_id, pipeline_id),
    )


@router.get("", response_model=list[Pipeline], summary="List pipelines")
async def list_pipelines(
    org_id: str = Depends(get_org_id),
    principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> list[dict]:
    # generated_config is stripped for a caller without dataconnect:pipelines:deploy (BUG-07).
    return pipelines_for(principal, await pipeline_service.list_pipelines(org_id))


@router.post(
    "",
    response_model=Pipeline,
    status_code=status.HTTP_201_CREATED,
    summary="Create pipeline",
)
async def create_pipeline(
    payload: PipelineCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    principal: Principal = Depends(require("dataconnect:pipelines:edit")),
) -> dict:
    pipeline = await pipeline_service.create_pipeline(org_id, payload)
    # Best-effort: if the source is reachable, auto-introspect and register its schema.
    # autocreate_schema_from_source itself persists schema_introspection_status/_message
    # (BUG-10) for every outcome, including an unexpected failure (e.g. the source
    # connection timing out) — this catch is only a last-resort safety net for a failure
    # in that persistence step itself, which we still log instead of losing silently.
    try:
        await pipeline_service.autocreate_schema_from_source(org_id, pipeline)
    except Exception:
        logger.exception("pipeline.schema_autocreate_failed", org_id=org_id, pipeline_id=pipeline["id"])
    # Best-effort: if a quality policy is attached, auto-run it on a live sample.
    # autorun_quality_from_source itself persists a "skipped" QualityResult with
    # a reason on any expected skip condition (no source, no rows, etc.) — this
    # catch is only for genuinely unexpected failures, which we still log instead
    # of losing silently.
    try:
        await pipeline_service.autorun_quality_from_source(org_id, pipeline)
    except Exception:
        logger.exception("pipeline.quality_autorun_failed", org_id=org_id, pipeline_id=pipeline["id"])
    result = await pipeline_service.get_or_404(org_id, pipeline["id"])
    await record_audit(
        org_id=org_id, actor=actor, action="created",
        resource_type="pipeline", resource_id=pipeline["id"], after_state=result,
    )
    return pipeline_for(principal, result)  # the audit row above keeps the full state; the response is permission-filtered


@router.get(
    "/topic-name-preview",
    response_model=TopicNamePreview,
    summary="Preview the real topic name for a set of identity fields",
)
async def topic_name_preview(
    zone: str = Query(...),
    domain: str = Query(...),
    source_system: str = Query(...),
    entity: str = Query(...),
    mode: str = Query(...),
    version: str = Query(...),
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> dict:
    # FE-BUG-08: registered before "/{pipeline_id}" so this static path isn't
    # swallowed by that dynamic one. Computed with the exact same
    # TopicParts/build_topic_name formula generate_pipeline_config uses at
    # create time — a wizard preview built any other way could drift from
    # what actually gets deployed, which is the whole bug this closes.
    parts = TopicParts(
        zone=zone, domain=domain, source_system=source_system,
        entity=entity, mode=mode, version=version,
    )
    try:
        validate_topic_parts(parts, soft_limit=settings.topic_soft_limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"topic_name": build_topic_name(parts, soft_limit=settings.topic_soft_limit)}


@router.get("/{pipeline_id}", response_model=Pipeline, summary="Get pipeline")
async def get_pipeline(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> dict:
    # generated_config is stripped for a caller without dataconnect:pipelines:deploy (BUG-07).
    return pipeline_for(principal, await pipeline_service.get_or_404(org_id, pipeline_id))


@router.patch("/{pipeline_id}", response_model=Pipeline, summary="Update pipeline specification")
async def update_pipeline(
    pipeline_id: str,
    payload: PipelineUpdate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    principal: Principal = Depends(require("dataconnect:pipelines:edit")),
) -> dict:
    before = await pipeline_service.get_or_404(org_id, pipeline_id)
    updated = await pipeline_service.update_pipeline(org_id, pipeline_id, payload)
    await record_audit(
        org_id=org_id, actor=actor, action="updated",
        resource_type="pipeline", resource_id=pipeline_id,
        before_state=before, after_state=updated,
    )
    return pipeline_for(principal, updated)


@router.delete(
    "/{pipeline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete pipeline (async once deployed — see status='deleting')",
)
async def delete_pipeline(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:pipelines:edit")),
) -> None:
    # For a deployed pipeline this only *requests* deletion — the pipeline
    # briefly stays visible with status "deleting" until the runtime
    # reconciler confirms the connector is gone from Kafka Connect. A
    # never-deployed (draft) pipeline is removed immediately, as before.
    before = await pipeline_service.get_or_404(org_id, pipeline_id)
    await pipeline_service.delete_pipeline(org_id, pipeline_id)  # org guard added
    await record_audit(
        org_id=org_id, actor=actor, action="delete_requested",
        resource_type="pipeline", resource_id=pipeline_id, before_state=before,
    )


async def _submit_lifecycle_operation(
    *, org_id: str, actor: str, pipeline_id: str, operation_type: str, action: str,
) -> dict:
    """Shared by deploy/pause/resume/restart below: all four durably record
    intent (pipeline_service's own record_desired, inside the service call
    itself) and then wait on a live Kafka Connect round-trip — exactly the
    kind of infrastructure operation that shouldn't tie up the HTTP request.
    Submits the actual pipeline_service call as an Operation (queued
    immediately, claimed and run by the operation worker — see
    app/services/operation_runner.py and this module's _LIFECYCLE_METHODS
    registration above) and returns it for the caller to poll via GET
    /operations/{id}.

    Audited as "<action>_requested" at submission time, the same "this is
    async now, audit the request not a confirmation we don't synchronously
    have" convention delete_pipeline below already uses for
    "delete_requested" — the runtime reconciler (app/services/
    runtime_reconciler.py) is the actual source of truth for whether the
    pipeline's status ever reaches its target regardless of whether this
    specific Operation or its audit trail does.

    FE-BUG-25: before any of that, the pipeline is atomically claimed for
    this action (PipelineService.claim_lifecycle_transition) — a 409 there
    means it already has an operation in flight, and this function returns
    before ever creating a new one. The operation id is pre-generated so the
    SAME id backs both the claim and the Operation row it's paired with.
    """
    before = await pipeline_service.get_or_404(org_id, pipeline_id)
    if action == "deploy":
        # FE-BUG-24: the "Ready to deploy" screen's own checks, enforced here too — before the
        # pipeline is claimed or an Operation created — so calling the API directly can't skip
        # what the UI refuses. Pause/resume/restart act on an already-deployed connector and
        # aren't gated.
        await pipeline_service.assert_ready_to_deploy(org_id, pipeline_id)
    operation_id = str(uuid4())
    claim_status = pipeline_service.status_to_claim_for(action)
    await pipeline_service.claim_lifecycle_transition(org_id, pipeline_id, claim_status, operation_id)
    operation = await operation_runner.create_and_run(
        org_id=org_id, operation_type=operation_type, resource_type="pipeline", resource_id=pipeline_id,
        operation_id=operation_id,
    )
    await record_audit(
        org_id=org_id, actor=actor, action=f"{action}_requested",
        resource_type="pipeline", resource_id=pipeline_id,
        before_state={"status": before["status"]},
    )
    return operation


@router.get(
    "/{pipeline_id}/deploy-readiness", response_model=DeployReadiness,
    summary="Preflight checks for the 'Ready to deploy' review screen (read-only — never deploys)",
)
async def deploy_readiness(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    # Same gate as the deploy action itself — whoever can't deploy shouldn't
    # see a screen whose only purpose is deciding whether to.
    _principal: Principal = Depends(require("dataconnect:pipelines:deploy")),
) -> dict:
    return await pipeline_service.check_deploy_readiness(org_id, pipeline_id)


@router.post(
    "/{pipeline_id}/deploy", response_model=Operation, status_code=status.HTTP_202_ACCEPTED,
    summary="Deploy pipeline (async — poll the returned operation)",
)
async def deploy_pipeline(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:pipelines:deploy")),
) -> dict:
    return await _submit_lifecycle_operation(
        org_id=org_id, actor=actor, pipeline_id=pipeline_id,
        operation_type="pipeline_deploy", action="deploy",
    )


@router.post(
    "/{pipeline_id}/pause", response_model=Operation, status_code=status.HTTP_202_ACCEPTED,
    summary="Pause pipeline (async — poll the returned operation)",
)
async def pause_pipeline(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:pipelines:operate")),
) -> dict:
    return await _submit_lifecycle_operation(
        org_id=org_id, actor=actor, pipeline_id=pipeline_id,
        operation_type="pipeline_pause", action="pause",
    )


@router.post(
    "/{pipeline_id}/resume", response_model=Operation, status_code=status.HTTP_202_ACCEPTED,
    summary="Resume pipeline (async — poll the returned operation)",
)
async def resume_pipeline(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:pipelines:operate")),
) -> dict:
    return await _submit_lifecycle_operation(
        org_id=org_id, actor=actor, pipeline_id=pipeline_id,
        operation_type="pipeline_resume", action="resume",
    )


@router.post(
    "/{pipeline_id}/restart", response_model=Operation, status_code=status.HTTP_202_ACCEPTED,
    summary="Restart pipeline (async — poll the returned operation)",
)
async def restart_pipeline(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:pipelines:operate")),
) -> dict:
    return await _submit_lifecycle_operation(
        org_id=org_id, actor=actor, pipeline_id=pipeline_id,
        operation_type="pipeline_restart", action="restart",
    )


@router.get("/{pipeline_id}/status", response_model=PipelineStatus, summary="Pipeline status")
async def pipeline_status(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:pipelines:view")),
) -> dict:
    pipeline = await pipeline_service.get_or_404(org_id, pipeline_id)
    reconciliation = await pipeline_service.get_reconciliation_state(pipeline_id)
    return pipeline_service.build_status(pipeline, reconciliation)


@router.get("/{pipeline_id}/generated-config", summary="View generated Kafka Connect config")
async def generated_config(
    pipeline_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    # Stricter than the other pipeline GETs above (pipelines:view): even
    # redacted of secrets, this exposes source topology, connector class,
    # topic/transform wiring — so it needs pipelines:deploy.
    _principal: Principal = Depends(require("dataconnect:pipelines:deploy")),
) -> dict:
    pipeline = await pipeline_service.get_or_404(org_id, pipeline_id)
    # Redacted again here defensively: this endpoint is the one place a raw
    # secret must never reach a browser, regardless of how it got persisted.
    config = redact_connector_config(pipeline["generated_config"])
    # A read, not a mutation — but this is exactly the kind of access ("who
    # looked at this pipeline's full resolved connector wiring") that's worth
    # a durable record even though nothing changed. No before/after state:
    # the config itself is already redacted of secrets and re-recording it
    # on every view would just be noise, not signal.
    await record_audit(
        org_id=org_id, actor=actor, action="accessed",
        resource_type="pipeline_generated_config", resource_id=pipeline_id,
    )
    return config
