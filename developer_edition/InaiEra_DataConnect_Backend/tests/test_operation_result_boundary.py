"""
GAP-11: Operation.result is enforced at the persistence boundary.

Each work callable used to be trusted to build a typed result; nothing stopped a new one (or a refactor) from returning
an arbitrary dict, or an enormous one, and having it stored. Now `register_work` refuses an operation type with no
contract, and `operation_runner.check_result` checks every result against that contract and
settings.operation_result_max_bytes before it is persisted.
"""

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

import app.services.operation_runner as operation_runner
from app.core.config import settings
from app.schemas.operation_results import RESULT_MODEL_BY_OPERATION_TYPE, ConnectionDiscoveryResult
from app.schemas.operations import Operation


class _Result(BaseModel):
    ok: bool
    note: str = ""


def _op(fake_postgres, operation_type: str) -> dict:
    return fake_postgres.create(
        "operations",
        {
            "org_id": "org-1", "operation_type": operation_type, "resource_type": "connection",
            "resource_id": "conn-1", "status": "running", "attempt_count": 1,
        },
    )


async def _run(fake_postgres, monkeypatch, returned, *, operation_type: str = "test_boundary_op"):
    monkeypatch.setitem(operation_runner._result_models, operation_type, _Result)

    async def work():
        return returned

    monkeypatch.setitem(operation_runner._work_factories, operation_type, lambda org_id, resource_id: work)
    op = _op(fake_postgres, operation_type)
    with capture_logs() as logs:
        await operation_runner._execute(op)
    return await operation_runner.get_operation("org-1", op["id"]), logs


# ── registration ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_an_operation_type_without_a_result_contract_cannot_be_registered(monkeypatch) -> None:
    monkeypatch.setattr(operation_runner, "_work_factories", {})
    monkeypatch.setattr(operation_runner, "_result_models", {})

    with pytest.raises(ValueError, match="no result contract"):
        operation_runner.register_work("brand_new_operation", lambda org_id, resource_id: None)


def test_a_type_can_be_registered_with_its_own_contract(monkeypatch) -> None:
    monkeypatch.setattr(operation_runner, "_work_factories", {})
    monkeypatch.setattr(operation_runner, "_result_models", {})

    operation_runner.register_work("brand_new_operation", lambda org_id, resource_id: None, result_model=_Result)

    assert operation_runner._result_models["brand_new_operation"] is _Result


def test_every_operation_type_the_app_registers_has_its_contract() -> None:
    import app.main  # noqa: F401 - importing the app registers every route module's work

    registered = set(operation_runner._work_factories)
    assert {"pipeline_deploy", "pipeline_pause", "pipeline_resume", "pipeline_restart", "connection_discover"} <= registered
    for operation_type in registered:
        if operation_type.startswith("test_"):
            continue  # registered by other tests, with their own contracts
        assert operation_runner._result_models[operation_type] is RESULT_MODEL_BY_OPERATION_TYPE[operation_type]


# ── what gets persisted ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_result_is_stored(fake_postgres, monkeypatch) -> None:
    finished, logs = await _run(fake_postgres, monkeypatch, {"ok": True, "note": "fine"})

    assert finished["status"] == "succeeded"
    assert finished["result_status"] == "valid"
    assert finished["error_code"] is None
    assert finished["result"] == {"ok": True, "note": "fine"}
    assert not any(entry["event"] == "operation.result_rejected" for entry in logs)


@pytest.mark.asyncio
async def test_a_stray_field_is_dropped_so_only_the_contract_is_stored(fake_postgres, monkeypatch) -> None:
    """The original bug: a callable embedded the raw Kafka Connect response next to its real result."""
    finished, _ = await _run(fake_postgres, monkeypatch, {"ok": True, "runtime_response": {"config": {"x": "y" * 500}}})

    assert finished["result"] == {"ok": True, "note": ""}


@pytest.mark.asyncio
async def test_a_result_that_breaks_its_contract_is_not_stored_and_is_logged(fake_postgres, monkeypatch) -> None:
    finished, logs = await _run(fake_postgres, monkeypatch, {"note": "no ok field"})

    assert finished["status"] == "succeeded"  # the work itself happened; its result was the defect
    assert finished["result"] is None
    assert finished["result_status"] == "invalid"  # ... and that is recorded, not hidden behind the success (GAP-04)
    assert finished["error_code"] == "OPERATION_RESULT_INVALID"
    rejected = [entry for entry in logs if entry["event"] == "operation.result_rejected"]
    assert rejected
    assert rejected[0]["operation_type"] == "test_boundary_op"
    assert rejected[0]["reason"] == "does not match its contract"


@pytest.mark.asyncio
async def test_a_result_over_the_size_limit_is_not_stored_and_is_logged(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr(settings, "operation_result_max_bytes", 200)

    finished, logs = await _run(fake_postgres, monkeypatch, {"ok": True, "note": "x" * 5000})

    assert finished["status"] == "succeeded"
    assert finished["result"] is None
    assert finished["result_status"] == "invalid"
    assert finished["error_code"] == "OPERATION_RESULT_INVALID"
    assert "too large" in finished["error_message"]
    rejected = [entry for entry in logs if entry["event"] == "operation.result_rejected"]
    assert rejected[0]["reason"] == "too large"
    assert rejected[0]["limit_bytes"] == 200
    assert rejected[0]["size_bytes"] > 200


@pytest.mark.asyncio
async def test_a_result_just_under_the_limit_is_stored(fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr(settings, "operation_result_max_bytes", 200)

    finished, _ = await _run(fake_postgres, monkeypatch, {"ok": True, "note": "x" * 100})

    assert finished["result"]["note"] == "x" * 100


@pytest.mark.asyncio
async def test_a_failed_operation_has_no_result_to_validate(fake_postgres, monkeypatch) -> None:
    monkeypatch.setitem(operation_runner._result_models, "test_boundary_fail", _Result)

    async def work():
        raise RuntimeError("boom")

    monkeypatch.setitem(operation_runner._work_factories, "test_boundary_fail", lambda o, r: work)
    op = _op(fake_postgres, "test_boundary_fail")

    with capture_logs() as logs:
        await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    assert finished["status"] == "failed"
    assert finished["result"] is None
    assert finished["result_status"] is None  # nothing ran to completion, so there is no result outcome
    assert not any(entry["event"] == "operation.result_rejected" for entry in logs)


# ── what the API says about it ─────────────────────────────────────────────────────────────────────────────────────

_LIFECYCLE = {
    "pipeline_id": "p1", "status": "running", "connector_name": "c", "topic_name": "t",
    "runtime": {"state": "running", "connector": "c", "topic": "t"},
}


def _operation(operation_type: str, result: dict | None) -> dict:
    return {
        "id": "op-1", "operation_type": operation_type, "resource_type": "pipeline", "resource_id": "p1",
        "status": "succeeded", "result": result,
    }


@pytest.mark.parametrize(
    ("operation_type", "result", "expected"),
    [
        ("pipeline_deploy", _LIFECYCLE, "PipelineDeployResult"),
        ("pipeline_pause", _LIFECYCLE, "PipelinePauseResult"),
        ("pipeline_resume", _LIFECYCLE, "PipelineResumeResult"),
        ("pipeline_restart", _LIFECYCLE, "PipelineRestartResult"),
        ("connection_discover", {"discovery_run_id": "run-1"}, "ConnectionDiscoveryResult"),
    ],
)
def test_a_result_is_read_back_as_its_operation_types_contract(operation_type, result, expected) -> None:
    operation = Operation.model_validate(_operation(operation_type, result))

    assert type(operation.result).__name__ == expected


def test_an_old_row_that_no_longer_fits_its_contract_is_still_readable() -> None:
    legacy = {"pipeline_id": "p1", "status": "running", "runtime_response": {"name": "c", "config": {"a": "b"}}}

    operation = Operation.model_validate(_operation("pipeline_deploy", legacy))

    assert operation.result == legacy  # kept as stored — reading an old operation is never a 500


def test_an_operation_type_with_no_contract_reads_back_as_a_plain_dict() -> None:
    operation = Operation.model_validate(_operation("something_old", {"anything": 1}))

    assert operation.result == {"anything": 1}


def test_the_api_documents_each_contract_by_name() -> None:
    from app.main import app

    schemas = app.openapi()["components"]["schemas"]
    options = {ref["$ref"].rsplit("/", 1)[-1] for ref in _result_options(schemas["Operation"])}

    assert {
        "PipelineDeployResult", "PipelinePauseResult", "PipelineResumeResult", "PipelineRestartResult",
        "ConnectionDiscoveryResult",
    } <= options


def _result_options(operation_schema: dict) -> list[dict]:
    result = operation_schema["properties"]["result"]
    return [opt for opt in result.get("anyOf", []) if "$ref" in opt]


def test_the_discovery_contract_is_only_a_reference() -> None:
    """Large results are referenced, not embedded: the only field is the id of the run holding them."""
    assert set(ConnectionDiscoveryResult.model_fields) == {"discovery_run_id"}


# ── GAP-04: execution outcome and result-contract outcome are separate, visible, and counted ─────────────────────────


def _invalid_count(operation_type: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "dataconnect_operation_result_invalid_total", {"service": "dataconnect", "operation": operation_type},
    ) or 0.0


@pytest.mark.asyncio
async def test_an_invalid_result_is_counted_so_it_can_be_alerted_on(fake_postgres, monkeypatch) -> None:
    before = _invalid_count("test_boundary_op")

    await _run(fake_postgres, monkeypatch, {"note": "no ok field"})
    await _run(fake_postgres, monkeypatch, {"ok": True})                 # a valid one is not counted

    assert _invalid_count("test_boundary_op") - before == 1


@pytest.mark.asyncio
async def test_the_diagnostic_carries_a_correlation_id_and_does_not_invite_a_retry(fake_postgres, monkeypatch) -> None:
    finished, _ = await _run(fake_postgres, monkeypatch, {"note": "no ok field"})

    assert finished["error_code"] == "OPERATION_RESULT_INVALID"
    assert finished["retryable"] is False                                # repeating a deploy would not fix a malformed result
    assert finished["correlation_id"]
    assert "took effect" in finished["error_message"]


@pytest.mark.asyncio
async def test_a_result_that_fails_to_build_after_the_side_effect_is_not_recorded_as_a_failure(fake_postgres, monkeypatch) -> None:
    """The action's own callable builds its typed result as its LAST step. If that raises, the connector was already deployed:
    the operation must not be reported `failed`, and must not be hidden behind a plain `succeeded` either."""
    from app.schemas.operation_results import build_result

    effects: list[str] = []
    monkeypatch.setitem(operation_runner._result_models, "test_build_op", _Result)

    async def work():
        effects.append("connector deployed")
        return build_result(_Result, note="missing the required ok field")

    monkeypatch.setitem(operation_runner._work_factories, "test_build_op", lambda o, r: work)
    op = _op(fake_postgres, "test_build_op")

    with capture_logs() as logs:
        await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    assert effects == ["connector deployed"]
    assert finished["status"] == "succeeded"
    assert finished["result_status"] == "invalid"
    assert finished["error_code"] == "OPERATION_RESULT_INVALID"
    assert finished["result"] is None
    assert any(entry["event"] == "operation.result_rejected" for entry in logs)


@pytest.mark.asyncio
async def test_an_exception_from_the_action_itself_is_still_a_failure(fake_postgres, monkeypatch) -> None:
    """Only a malformed RESULT gets the new treatment: any other exception raised by the work stays `failed`."""
    monkeypatch.setitem(operation_runner._result_models, "test_fail_op", _Result)

    async def work():
        raise ValueError("the action itself broke")

    monkeypatch.setitem(operation_runner._work_factories, "test_fail_op", lambda o, r: work)
    op = _op(fake_postgres, "test_fail_op")

    await operation_runner._execute(op)

    finished = await operation_runner.get_operation("org-1", op["id"])
    assert finished["status"] == "failed"
    assert finished["result_status"] is None
    assert finished["error_code"] != "OPERATION_RESULT_INVALID"


def test_build_result_returns_the_contract_shape_or_raises_a_typed_error() -> None:
    from app.schemas.operation_results import OperationResultInvalid, build_result

    assert build_result(ConnectionDiscoveryResult, discovery_run_id="run-1") == {"discovery_run_id": "run-1"}
    with pytest.raises(OperationResultInvalid) as exc_info:
        build_result(ConnectionDiscoveryResult)
    assert isinstance(exc_info.value, ValueError)                         # what a direct caller always saw (pydantic's is one too)
    assert exc_info.value.model_name == "ConnectionDiscoveryResult"
    assert exc_info.value.errors


def test_the_real_lifecycle_and_discovery_callables_build_their_results_through_build_result() -> None:
    """A guard: if one went back to constructing the model inline, a bad result would be reported as a failed operation."""
    import inspect

    from app.api.routes import connections
    from app.services.pipeline_service import PipelineService

    for source in (inspect.getsource(PipelineService.deploy), inspect.getsource(PipelineService._lifecycle), inspect.getsource(connections)):
        assert "build_result(" in source
        assert ").model_dump()" not in source.replace("build_result(", "")


def test_the_operation_api_exposes_the_result_outcome() -> None:
    operation = Operation.model_validate({**_operation("something_old", None), "result_status": "invalid", "error_code": "OPERATION_RESULT_INVALID"})

    assert operation.status == "succeeded"
    assert operation.result_status == "invalid"
    assert operation.error_code == "OPERATION_RESULT_INVALID"
    assert "result_status" in Operation.model_fields


def test_the_database_only_accepts_valid_or_invalid_as_a_result_status() -> None:
    from app.models.operations import Operation as OperationRow

    names = {c.name for c in OperationRow.__table__.constraints if c.name}
    assert "ck_dc_operations_result_status" in names
