"""
GAP-04 against a REAL PostgreSQL: an operation whose action succeeded but whose result is invalid is stored with
status=succeeded, result_status=invalid and error_code=OPERATION_RESULT_INVALID — and the column's CHECK constraint holds.

Skipped unless a database is provided; run with the dev stack up:

    DATACONNECT_DATABASE_URL=postgresql+asyncpg://dataconnect:dataconnect@localhost:5435/dataconnect \\
    DATACONNECT_REAL_DB_TESTS=1 .venv/bin/python -m pytest tests/test_operation_result_real_db.py --no-cov

Rows are created under `org_1001` and deleted afterwards.
"""

import os
import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.skipif(not os.environ.get("DATACONNECT_REAL_DB_TESTS"), reason="needs a real PostgreSQL (DATACONNECT_REAL_DB_TESTS=1)")

ORG = "org_1001"


class _Result(BaseModel):
    ok: bool


@pytest.fixture
async def db():
    from app.db.session import AsyncSessionLocal, engine

    await engine.dispose()
    ids: list[str] = []
    yield AsyncSessionLocal, ids
    async with AsyncSessionLocal() as session:
        for op_id in ids:
            await session.execute(text("delete from dc_operations where id = :i"), {"i": op_id})
        await session.commit()
    await engine.dispose()


async def _submit_and_run(db, monkeypatch, returned):
    import app.services.operation_runner as runner

    session_factory, ids = db
    operation_type = f"test_real_{uuid.uuid4().hex[:8]}"
    monkeypatch.setitem(runner._result_models, operation_type, _Result)

    async def work():
        return returned

    monkeypatch.setitem(runner._work_factories, operation_type, lambda o, r: work)
    op = await runner.create_and_run(org_id=ORG, operation_type=operation_type, resource_type="pipeline", resource_id="p-real")
    ids.append(op["id"])
    async with session_factory() as session:
        await session.execute(text("update dc_operations set status='running', attempt_count=1 where id=:i"), {"i": op["id"]})
        await session.commit()
    await runner._execute({**op, "status": "running", "attempt_count": 1})
    async with session_factory() as session:
        row = (await session.execute(text(
            "select status, result_status, error_code, retryable, correlation_id, result is null from dc_operations where id=:i"),
            {"i": op["id"]})).one()
    return row


async def test_an_invalid_result_is_persisted_as_succeeded_with_result_status_invalid(db, monkeypatch) -> None:
    status, result_status, error_code, retryable, correlation_id, no_result = await _submit_and_run(db, monkeypatch, {"nope": 1})

    assert status == "succeeded"
    assert result_status == "invalid"
    assert error_code == "OPERATION_RESULT_INVALID"
    assert retryable is False
    assert correlation_id
    assert no_result


async def test_a_valid_result_is_persisted_as_valid_with_no_error(db, monkeypatch) -> None:
    status, result_status, error_code, _, _, no_result = await _submit_and_run(db, monkeypatch, {"ok": True})

    assert (status, result_status, error_code, no_result) == ("succeeded", "valid", None, False)


async def test_the_database_refuses_any_other_result_status(db) -> None:
    session_factory, ids = db
    from app.models.operations import Operation

    async with session_factory() as session:
        op = Operation(org_id=ORG, operation_type="pipeline_deploy", resource_type="pipeline", resource_id="p", status="succeeded", result_status="bogus")
        session.add(op)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
