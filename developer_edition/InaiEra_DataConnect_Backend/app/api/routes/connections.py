"""
Connection Profile routes.

Improvements:
- Delete uses org-scoped guard (was missing org_id check in original).
- test and discover use get_or_404 so error messages are consistent.
- OpenAPI response models included for all endpoints.
"""

from fastapi import APIRouter, Depends, status
from gateway_auth import Principal

from app.core.actor import get_actor
from app.core.audit import record_audit
from app.core.permissions import require
from app.core.tenant import get_org_id
from app.schemas.connection import (
    ConnectionProfile,
    ConnectionProfileCreate,
    ConnectionTestResult,
)
from app.schemas.discovery import DiscoveryRun
from app.schemas.operation_results import ConnectionDiscoveryResult, build_result
from app.services import operation_runner
from app.services.connection_service import connection_service
from app.services.discovery_service import discovery_service
from app.services.secrets import mask_connection_profile

router = APIRouter()


def _discover_factory(org_id: str, resource_id: str):
    """Registered with the operation worker (app/services/operation_runner.py)
    so a discover Operation can be claimed and (re)run from just
    (org_id, resource_id) alone — including by a later process, after this
    one restarts mid-run. `resource_id` is a DiscoveryRun id (GAP-10), not a
    connection id directly: the run row is what carries which connection
    this discovery is for, the same durable-intent role a pipeline's own
    row plays for deploy/pause/resume/restart. discovery_service.
    run_discovery persists its result as DiscoveryObject rows rather than
    returning it for Operation.result to hold — a large source's discovered
    object count doesn't belong in one generic JSONB column."""
    async def work() -> dict:
        run = await discovery_service.get_run_or_404(org_id, resource_id)
        connection = await connection_service.get_or_404(org_id, run["connection_id"])
        await discovery_service.run_discovery(org_id, run["id"], connection)
        return build_result(ConnectionDiscoveryResult, discovery_run_id=run["id"])

    return work


operation_runner.register_work("connection_discover", _discover_factory)


@router.get("", response_model=list[ConnectionProfile], summary="List connections")
async def list_connections(
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:connections:view")),
) -> list[dict]:
    return [mask_connection_profile(c) for c in await connection_service.list_connections(org_id)]


@router.post(
    "",
    response_model=ConnectionProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Create a connection profile",
)
async def create_connection(
    payload: ConnectionProfileCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:connections:edit")),
) -> dict:
    created = await connection_service.create_connection(org_id, payload.model_dump(exclude_none=True))
    # Verify we can actually reach/authenticate the source; reflect it in
    # status/verification_status/last_test_message regardless of whether the
    # probe itself raises (a timeout, DNS failure, ...) — see
    # ConnectionProfileService.probe_and_record (BUG-08). Never propagates:
    # a probe failure means the connection is saved but unverified/erroring,
    # not that creation itself failed.
    await connection_service.probe_and_record(org_id, created["id"])
    created = await connection_service.get_or_404(org_id, created["id"])
    masked = mask_connection_profile(created)
    await record_audit(
        org_id=org_id, actor=actor, action="created",
        resource_type="connection", resource_id=created["id"],
        after_state=masked,
    )
    return masked


@router.get("/{connection_id}", response_model=ConnectionProfile, summary="Get a connection")
async def get_connection(
    connection_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:connections:view")),
) -> dict:
    return mask_connection_profile(await connection_service.get_or_404(org_id, connection_id))


@router.put("/{connection_id}", response_model=ConnectionProfile, summary="Update a connection")
async def update_connection(
    connection_id: str,
    payload: ConnectionProfileCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:connections:edit")),
) -> dict:
    existing = await connection_service.get_or_404(org_id, connection_id)
    before = mask_connection_profile(existing)
    data = payload.model_dump(exclude_none=True)
    # The client only ever saw a masked connection_config (see get_connection/
    # list_connections below) — connection_service.update_connection handles
    # a secret field it echoed back unchanged (or omitted) by leaving
    # whatever's already stored for that key alone (see
    # app.services.secrets.split_secret_fields), so there's nothing to
    # restore here first.
    updated = await connection_service.update_connection(org_id, connection_id, data)
    after = mask_connection_profile(updated)
    await record_audit(
        org_id=org_id, actor=actor, action="updated",
        resource_type="connection", resource_id=connection_id,
        before_state=before, after_state=after,
    )
    return after


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a connection",
)
async def delete_connection(
    connection_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:connections:edit")),
) -> None:
    # Original was missing org_id guard — fixed here.
    existing = await connection_service.get_or_404(org_id, connection_id)
    before = mask_connection_profile(existing)
    await connection_service.delete_connection(org_id, connection_id)
    await record_audit(
        org_id=org_id, actor=actor, action="deleted",
        resource_type="connection", resource_id=connection_id,
        before_state=before,
    )


@router.post(
    "/{connection_id}/test",
    response_model=ConnectionTestResult,
    summary="Test a connection profile",
)
async def test_connection(
    connection_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    connection = await connection_service.get_or_404(org_id, connection_id)
    result = await connection_service.test_connection(connection)
    await record_audit(
        org_id=org_id, actor=actor, action="tested",
        resource_type="connection", resource_id=connection_id, after_state=result,
    )
    return result


@router.post(
    "/test-connection",
    response_model=ConnectionTestResult,
    summary="Live connectivity test for a payload (before saving)",
)
async def test_connection_payload(
    payload: ConnectionProfileCreate,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    # Set when this probes the in-progress edit of an already-saved
    # connection (the edit form's "Test connection" button always tests
    # whatever's currently typed, not the saved row, so this is audit
    # correlation only — it doesn't change what gets probed). Absent for a
    # genuinely new, not-yet-created connection, where there is no id yet.
    connection_id: str | None = None,
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    result = await connection_service.live_test_connection(payload.model_dump(exclude_none=True), org_id)
    await record_audit(
        org_id=org_id, actor=actor, action="tested",
        resource_type="connection", resource_id=connection_id, after_state=result,
    )
    return result


@router.post(
    "/{connection_id}/test-connection",
    response_model=ConnectionTestResult,
    summary="Live connectivity test for a saved connection",
)
async def test_connection_live(
    connection_id: str,
    org_id: str = Depends(get_org_id),
    actor: str = Depends(get_actor),
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    # Durably records the outcome (verification_status/last_tested_at/
    # last_test_message, and the coarser status) the same way creation's own
    # auto-probe does — see ConnectionProfileService.probe_and_record
    # (BUG-08) — rather than just returning a result nothing else remembers.
    result = await connection_service.probe_and_record(org_id, connection_id)
    await record_audit(
        org_id=org_id, actor=actor, action="tested",
        resource_type="connection", resource_id=connection_id, after_state=result,
    )
    return result


@router.post(
    "/{connection_id}/discover",
    response_model=DiscoveryRun,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Discover objects exposed by a connection (async — poll the returned discovery run)",
)
async def discover_connection(
    connection_id: str,
    org_id: str = Depends(get_org_id),
    _principal: Principal = Depends(require("dataconnect:connections:test")),
) -> dict:
    """Discovery is live network I/O against the source system itself (a DB
    introspection query, an API call, a file listing) with no bound on how
    long the source takes to answer, and — for a large source — can turn up
    far more objects (GAP-10: tens of thousands of tables/columns) than
    belong in one generic JSON blob. Submitted as an Operation (durable
    claim/lease execution, same as every other long-running action) driving
    a DiscoveryRun (the pollable summary this returns): poll
    GET /discovery/{id} for status/object_count, then page through the
    results with GET /discovery/{id}/objects — never the operation itself.
    """
    await connection_service.get_or_404(org_id, connection_id)  # fail fast with a 404 if it's already gone
    run = await discovery_service.create_run(org_id, connection_id)
    operation = await operation_runner.create_and_run(
        org_id=org_id, operation_type="connection_discover", resource_type="discovery_run", resource_id=run["id"],
    )
    await discovery_service.set_operation_id(org_id, run["id"], operation["id"])
    run["operation_id"] = operation["id"]
    return run
