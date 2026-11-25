"""
TimestampConverter (and the other SMTs) with a missing setting: the New pipeline form let you create one, the deploy reported
success, and the task then failed at start-up ("TimestampConverter requires format option to be specified when using string
timestamps") with no reason saved on the pipeline. Now:
  - saving a transform policy with a missing required setting is a 422 naming it,
  - the deploy preflight blocks the same thing in an already-saved policy,
  - a task Kafka Connect reports FAILED has its reason saved as the pipeline's runtime error,
  - a CDC pipeline using TimestampConverter gets time.precision.mode=connect (Debezium's default microseconds were read as
    milliseconds: 2026-10-11 became year 58746).
"""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient
from test_runtime_reconciler_generation_race import (
    _FakeRepo,
    _FakeSession,
    _install_common_fakes,
    _row,
)

import app.services.runtime_reconciler as reconciler_module
from app.main import app
from app.services.connect_config import _build_connector
from app.services.deploy_preflight import transform_settings_problems
from app.services.runtime_reconciler import connect_failure_reason
from app.services.transform_policy import (
    generate_smt_config,
    transform_config_problems,
    validate_transform_settings,
)

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
TS_CLASS = "org.apache.kafka.connect.transforms.TimestampConverter$Value"
FORMAT_ERROR = ("TimestampConverter: 'format' is required when the target type is string")


def _ts(**config) -> dict:
    return {"order": 1, "name": "TimestampConverter", "type": "TimestampConverter", "scope": "value", "config": config}


# ── the rule itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize(("config", "expected"), [
    ({"field": "created_at", "target.type": "string", "format": "yyyy-MM-dd HH:mm:ss"}, []),
    ({"field": "created_at", "target.type": "unix"}, []),
    ({"field": "created_at", "target.type": "Timestamp"}, []),
    ({"field": "created_at", "target.type": "string"}, [FORMAT_ERROR]),
    ({"field": "created_at", "target.type": "string", "format": "   "}, [FORMAT_ERROR]),
    ({"target.type": "string", "format": "yyyy"}, ["TimestampConverter: 'field' is required."]),
    ({"field": "created_at"}, ["TimestampConverter: 'target.type' is required."]),
    ({"field": "created_at", "target.type": "epoch"}, ["TimestampConverter: target type 'epoch' must be one of"]),
])
def test_timestamp_converter_settings(config, expected) -> None:
    problems = transform_config_problems("TimestampConverter", config)
    assert len(problems) == len(expected)
    for problem, want in zip(problems, expected, strict=True):
        assert problem.startswith(want)


@pytest.mark.parametrize(("transform_type", "config", "missing"), [
    ("ValueToKey", {}, ["fields"]),
    ("ExtractField", {}, ["field"]),
    ("HoistField", {}, ["field"]),
    ("MaskField", {"replacement": "****"}, ["fields"]),
    ("RegexRouter", {"regex": "(.*)"}, ["replacement"]),
    ("HeaderFrom", {"fields": "tenant_id"}, ["headers", "operation"]),
])
def test_other_transforms_name_each_missing_setting(transform_type, config, missing) -> None:
    assert transform_config_problems(transform_type, config) == [f"{transform_type}: '{k}' is required." for k in missing]


@pytest.mark.parametrize("transform_type", ["ReplaceField", "Flatten", "ExtractNewRecordState", "InsertField"])
def test_transforms_with_nothing_required_pass_empty(transform_type) -> None:
    assert transform_config_problems(transform_type, {}) == []


def test_validate_transform_settings_lists_every_problem() -> None:
    with pytest.raises(ValueError, match="TimestampConverter: 'format' is required") as exc:
        validate_transform_settings([_ts(field="created_at", **{"target.type": "string"}), {"type": "MaskField", "config": {}}])
    assert "MaskField: 'fields' is required." in str(exc.value)


def test_generating_config_stays_tolerant_so_old_policies_can_still_be_paused_or_deleted() -> None:
    # Every reconciler tick builds the config: an old policy missing a setting must not make that raise.
    assert generate_smt_config([_ts(field="created_at", **{"target.type": "string"})])["transforms"] == "TimestampConverter"


# ── saving a policy ───────────────────────────────────────────────────────

def test_saving_a_policy_without_format_is_a_422_naming_it(fake_postgres) -> None:
    response = client.post("/api/v1/dataconnect/transform-policies", headers={"X-Tenant-Id": "org-1"}, json={
        "name": "no-format", "transforms": [_ts(field="created_at", **{"target.type": "string"})]})
    assert response.status_code == 422
    assert FORMAT_ERROR in response.text


def test_saving_a_complete_policy_still_works(monkeypatch) -> None:
    from app.services.transform_service import transform_service

    async def _create(org_id, data):
        return {**data, "id": "tp-1", "version": 1, "created_at": "2026-10-16T00:00:00Z", "updated_at": "2026-10-16T00:00:00Z"}

    async def _no_audit(**kwargs):
        return None

    monkeypatch.setattr(transform_service, "create_policy", _create)
    monkeypatch.setattr("app.api.routes.transforms.record_audit", _no_audit)
    response = client.post("/api/v1/dataconnect/transform-policies", headers={"X-Tenant-Id": "org-1"}, json={
        "name": "with-format", "transforms": [_ts(field="created_at", format="yyyy-MM-dd HH:mm:ss", **{"target.type": "string"})]})
    assert response.status_code == 201, response.text


# ── deploy preflight, from the generated config ───────────────────────────

def test_preflight_finds_a_string_target_without_format_in_the_generated_config() -> None:
    config = {"transforms": "TimestampConverter,reroute",
              "transforms.TimestampConverter.type": TS_CLASS, "transforms.TimestampConverter.field": "created_at",
              "transforms.TimestampConverter.target.type": "string",
              "transforms.reroute.type": "org.apache.kafka.connect.transforms.RegexRouter",
              "transforms.reroute.regex": ".*", "transforms.reroute.replacement": "t"}
    assert transform_settings_problems(config) == [
        "TimestampConverter: 'format' is required when the target type is string (for example yyyy-MM-dd HH:mm:ss)."]
    config["transforms.TimestampConverter.format"] = "yyyy-MM-dd"
    assert transform_settings_problems(config) == []


def test_preflight_ignores_transform_classes_it_does_not_offer() -> None:
    config = {"transforms": "x", "transforms.x.type": "com.example.CustomSmt"}
    assert transform_settings_problems(config) == []


# ── the reason Kafka Connect gives is saved ───────────────────────────────

TRACE = ("org.apache.kafka.connect.errors.ConnectException: org.apache.kafka.common.config.ConfigException: TimestampConverter "
         "requires format option to be specified when using string timestamps\n\tat org.apache.kafka.connect.runtime...")


def test_connect_failure_reason_takes_the_failed_tasks_first_trace_line() -> None:
    status = {"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "FAILED", "trace": TRACE}]}
    reason = connect_failure_reason(status)
    assert reason.startswith("Kafka Connect reports the task 0 FAILED: org.apache.kafka.connect.errors.ConnectException")
    assert "requires format option" in reason
    assert "\tat " not in reason


def test_connect_failure_reason_for_a_failed_connector_and_for_none() -> None:
    assert "the connector FAILED: boom" in connect_failure_reason({"connector": {"state": "FAILED", "trace": "boom"}, "tasks": []})
    assert connect_failure_reason({"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "RUNNING"}]}) is None
    assert connect_failure_reason(None) is None


@pytest.mark.asyncio
async def test_a_reconcile_tick_saves_the_failed_tasks_reason_on_the_pipeline(monkeypatch) -> None:
    _install_common_fakes(monkeypatch, connect_status_sequence=["RUNNING"])

    async def _degraded(client, connector_name):
        return {"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "FAILED", "trace": TRACE}]}

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _degraded)
    row = _row(desired_state="running", desired_generation=5)
    session = _FakeSession(live_desired_generation=5)

    result = await reconciler_module.RuntimeReconciler()._reconcile_row(session, _FakeRepo(session), client=None, existing={"conn-1"}, row=row)

    assert result["observed_state"] == "degraded"
    assert "requires format option" in (row.last_runtime_error or "")


async def _tick_with_status(monkeypatch, status: dict, previous_error: str | None):
    _install_common_fakes(monkeypatch, connect_status_sequence=["RUNNING"])

    async def _status(client, connector_name):
        return status

    monkeypatch.setattr(reconciler_module, "get_status_retryable", _status)
    row = _row(desired_state="running", desired_generation=5)
    row.last_runtime_error = previous_error
    session = _FakeSession(live_desired_generation=5)
    await reconciler_module.RuntimeReconciler()._reconcile_row(session, _FakeRepo(session), client=None, existing={"conn-1"}, row=row)
    return row.last_runtime_error


SAVED = "Kafka Connect reports the task 0 FAILED: ...ConfigException: TimestampConverter requires format option"


@pytest.mark.asyncio
async def test_the_reason_is_kept_on_the_tick_after_the_self_heal_restart(monkeypatch) -> None:
    # Right after the restart Connect shows the task without a trace (or no task at all yet): the reason must not flicker away.
    restarting = {"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "UNASSIGNED"}]}
    assert await _tick_with_status(monkeypatch, restarting, SAVED) == SAVED


@pytest.mark.asyncio
async def test_the_reason_is_cleared_once_the_connector_is_healthy(monkeypatch) -> None:
    healthy = {"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "RUNNING"}]}
    assert await _tick_with_status(monkeypatch, healthy, SAVED) is None


@pytest.mark.asyncio
async def test_an_unrelated_older_error_is_not_kept_by_that_rule(monkeypatch) -> None:
    restarting = {"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "UNASSIGNED"}]}
    assert await _tick_with_status(monkeypatch, restarting, "Connect unreachable: timeout") is None


# ── CDC timestamp precision ───────────────────────────────────────────────

def _cdc_pipeline() -> dict:
    return {"id": "p1", "org_id": "org_1", "domain": "d", "source_system": "pg", "entity": "people", "version": "v1", "mode": "cdc",
            "connector_name": "d--pg--people--cdc--v1", "topic_name": "raw-d-pg-people-cdc-v1",
            "connector_config": {"tables": ["people"]}}


def _cdc_config(transforms: list[dict] | None, connector_class="io.debezium.connector.postgresql.PostgresConnector") -> dict:
    connection = {"source_type": "postgresql", "connection_config": {"hostname": "h", "port": 5432, "database": "db", "username": "u"}}
    config, _ = _build_connector(_cdc_pipeline(), connection, {"connector_class": connector_class},
                                 None, {"transforms": transforms} if transforms else None)
    return config


def test_cdc_with_timestamp_converter_uses_connect_time_precision() -> None:
    config = _cdc_config([_ts(field="created_at", format="yyyy-MM-dd HH:mm:ss", **{"target.type": "string"})])
    assert config["time.precision.mode"] == "connect"


def test_cdc_without_timestamp_converter_keeps_debeziums_default_precision() -> None:
    assert "time.precision.mode" not in _cdc_config(None)
    assert "time.precision.mode" not in _cdc_config([{"order": 1, "name": "MaskField", "type": "MaskField", "scope": "value",
                                                      "config": {"fields": "email"}}])


def test_mysql_cdc_with_timestamp_converter_also_uses_connect_time_precision() -> None:
    config = _cdc_config([_ts(field="created_at", **{"target.type": "unix"})], connector_class="io.debezium.connector.mysql.MySqlConnector")
    assert config["time.precision.mode"] == "connect"


class _Plugins:
    async def list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source"}, {"class": TS_CLASS, "type": "transformation"}]

    async def get_worker_info(self):
        return {"version": "7.6.0"}


@pytest.mark.asyncio
async def test_the_deploy_preflight_runs_the_settings_check() -> None:
    from app.services.deploy_preflight import collect_preflight_failures

    config = {"connector.class": "io.confluent.connect.jdbc.JdbcSourceConnector", "transforms": "TimestampConverter",
              "transforms.TimestampConverter.type": TS_CLASS, "transforms.TimestampConverter.field": "created_at",
              "transforms.TimestampConverter.target.type": "string"}
    template = {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source"}
    failures = await collect_preflight_failures(template=template, pipeline={"id": "p1", "mode": "batch"}, config=config, client=_Plugins())
    assert failures == [{"check": "smt_settings_valid", "reason":
                         "TimestampConverter: 'format' is required when the target type is string (for example yyyy-MM-dd HH:mm:ss)."}]
