"""
BUG-11: PipelineService/connection-discover work callables run through the
Operation claim/lease worker (app/services/operation_runner.py) — this
confirms _execute's failure path persists a SafeError's fields instead of
`str(exc)[:2000]`, and that the two non-exception terminal failures
(attempts exhausted, no work registered) get the same treatment.
"""

import pytest
from pydantic import BaseModel

import app.services.operation_runner as operation_runner


class _OkResult(BaseModel):
    ok: bool


@pytest.mark.asyncio
async def test_a_raised_exception_persists_safe_error_fields_not_raw_text(fake_postgres) -> None:
    op = fake_postgres.create(
        "operations",
        {
            "org_id": "org-1", "operation_type": "test_leaky_op", "resource_type": "connection",
            "resource_id": "conn-1", "status": "running", "attempt_count": 1,
        },
    )

    def _leaky_factory(org_id, resource_id):
        async def work():
            raise ConnectionError("could not connect to postgres://dataconnect:hunter2@db.internal:5432/erp")
        return work

    operation_runner.register_work("test_leaky_op", _leaky_factory, result_model=_OkResult)

    await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    assert finished["status"] == "failed"
    assert finished["error_code"] == "SOURCE_CONNECTION_REFUSED"
    assert finished["retryable"] is True
    assert finished["correlation_id"] is not None
    assert "hunter2" not in finished["error_message"]
    assert "db.internal" not in finished["error_message"]
    assert finished["error_message"] == "Could not connect to the source system."


@pytest.mark.asyncio
async def test_attempts_exhausted_gets_a_safe_error_too(fake_postgres) -> None:
    op = fake_postgres.create(
        "operations",
        {
            "org_id": "org-1", "operation_type": "test_any_op", "resource_type": "connection",
            "resource_id": "conn-1", "status": "running",
            "attempt_count": 999,  # comfortably past settings.operation_max_attempts
        },
    )

    await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    # FE-BUG-05: "stale", not "failed" — the work itself was never observed
    # to error out, every claim of it just never reached a terminal status.
    assert finished["status"] == "stale"
    assert finished["error_code"] == "ATTEMPTS_EXHAUSTED"
    assert finished["retryable"] is False
    assert finished["correlation_id"] is not None
    assert "999" in finished["error_message"]  # the generic message may reference the count itself, just not internals


@pytest.mark.asyncio
async def test_no_work_registered_gets_a_safe_error_too(fake_postgres) -> None:
    op = fake_postgres.create(
        "operations",
        {
            "org_id": "org-1", "operation_type": "test_totally_unregistered_op", "resource_type": "connection",
            "resource_id": "conn-1", "status": "running", "attempt_count": 1,
        },
    )

    await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    assert finished["error_code"] == "NO_WORK_REGISTERED"
    assert finished["retryable"] is False
    # the operation_type value itself never leaks into the persisted message —
    # only into the (separately logged) diagnostic context.
    assert "test_totally_unregistered_op" not in finished["error_message"]


@pytest.mark.asyncio
async def test_a_successful_operation_leaves_error_fields_null(fake_postgres) -> None:
    op = fake_postgres.create(
        "operations",
        {
            "org_id": "org-1", "operation_type": "test_ok_op", "resource_type": "connection",
            "resource_id": "conn-1", "status": "running", "attempt_count": 1,
        },
    )

    def _ok_factory(org_id, resource_id):
        async def work():
            return {"ok": True}
        return work

    operation_runner.register_work("test_ok_op", _ok_factory, result_model=_OkResult)

    await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    assert finished["status"] == "succeeded"
    assert finished["error_code"] is None
    assert finished["correlation_id"] is None
