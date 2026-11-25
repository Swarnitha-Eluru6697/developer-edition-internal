"""GAP-05: the runtime reconciler preflights a connector config BEFORE applying it — the explicit Deploy action
already did, but the reconciler is what applies a pipeline EDIT and re-creates a connector Connect lost, and used to
send those straight to Connect with no compatibility check."""
import pytest

import app.services.runtime_reconciler as reconciler_module
from app.services import pipeline_service as pipeline_service_module
from app.services.catalog_service import catalog_service
from tests.test_runtime_reconciler_convergence import (
    PIPELINE,
    _FakeRepo,
    _FakeSession,
    _install_fakes,
    _row,
)

CONNECTOR = "io.confluent.connect.jdbc.JdbcSourceConnector"
SMT = "org.apache.kafka.connect.transforms.MaskField$Value"
TEMPLATE = {"connector_class": CONNECTOR, "connector_type": "source", "connector_version": "10.7.6"}


class _Worker:
    """A Kafka Connect client that reports a plugin catalog (and counts how often it is asked)."""

    def __init__(self, plugins):
        self._plugins = plugins
        self.plugin_calls = 0

    async def list_connector_plugins(self):
        self.plugin_calls += 1
        return self._plugins

    async def get_worker_info(self):
        return {"version": "7.9.0-ccs"}


def _plugins(*, jdbc_version="10.9.7", with_smt=True):
    plugins = [{"class": CONNECTOR, "type": "source", "version": jdbc_version}]
    # A worker that lists transformations at all lists SOME; with_smt=False means it has others but not ours.
    plugins.append({"class": SMT if with_smt else "x.OtherSmt", "type": "transformation", "version": "3.9.0"})
    return plugins


@pytest.fixture(autouse=True)
def _template(monkeypatch):
    async def _get_template(template_id):
        return dict(TEMPLATE)

    monkeypatch.setattr(catalog_service, "get_template", _get_template)
    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", True)


def _use_pipeline_with_a_template(monkeypatch) -> None:
    async def _get_pipeline(org_id, pipeline_id):
        return {**PIPELINE, "template_id": "tpl-jdbc", "mode": "batch"}

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _get_pipeline)


async def _tick(monkeypatch, worker, *, config=None, exists=False):
    calls, deployed = _install_fakes(monkeypatch, status_sequence=["RUNNING"])
    _use_pipeline_with_a_template(monkeypatch)
    if config is not None:
        async def _resolve(pipeline):
            return config, "hash-new"

        monkeypatch.setattr(reconciler_module, "_resolve_config", _resolve)
    row = _row(desired_state="running", desired_generation=1, observed_generation=0, config_hash="hash-old")
    reconciler = reconciler_module.RuntimeReconciler()
    result = await reconciler._reconcile_row(
        _session := _FakeSession(live_desired_generation=1), _FakeRepo(_session), client=worker,
        existing={"conn-1"} if exists else set(), row=row,
    )
    return calls, deployed, row, result


@pytest.mark.asyncio
async def test_a_compatible_config_is_applied(monkeypatch) -> None:
    calls, deployed, row, _ = await _tick(monkeypatch, _Worker(_plugins()), config={"connector.class": CONNECTOR})

    assert calls["deploy"] == ["conn-1"]
    assert row.last_runtime_error is None
    assert row.config_hash == "hash-new"


@pytest.mark.asyncio
async def test_an_edit_that_adds_an_smt_the_worker_lacks_is_not_applied(monkeypatch) -> None:
    config = {
        "connector.class": CONNECTOR, "transforms": "mask", "transforms.mask.type": SMT,
    }

    calls, deployed, row, _ = await _tick(monkeypatch, _Worker(_plugins(with_smt=False)), config=config, exists=True)

    # The worker lists other transformations but not this one -> blocked before anything reaches Connect.
    assert calls["deploy"] == []
    assert deployed == []


@pytest.mark.asyncio
async def test_a_blocked_config_surfaces_as_the_pipelines_runtime_error_and_is_not_marked_applied(monkeypatch) -> None:
    worker = _Worker(_plugins(with_smt=False))
    config = {"connector.class": CONNECTOR, "transforms": "mask", "transforms.mask.type": SMT}

    calls, _, row, _ = await _tick(monkeypatch, worker, config=config, exists=True)

    assert calls["deploy"] == []
    assert "preflight failed" in row.last_runtime_error
    assert SMT in row.last_runtime_error
    assert row.config_hash == "hash-old"  # not stamped as applied, so the next tick tries again


@pytest.mark.asyncio
async def test_an_installed_plugin_older_than_the_template_qualification_blocks_the_apply(monkeypatch) -> None:
    calls, _, row, _ = await _tick(
        monkeypatch, _Worker(_plugins(jdbc_version="10.6.0")), config={"connector.class": CONNECTOR},
    )

    assert calls["deploy"] == []
    assert "older than the 10.7.6" in row.last_runtime_error


@pytest.mark.asyncio
async def test_the_preflight_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", False)

    calls, _, row, _ = await _tick(
        monkeypatch, _Worker(_plugins(jdbc_version="1.0.0")), config={"connector.class": CONNECTOR},
    )

    assert calls["deploy"] == ["conn-1"]


@pytest.mark.asyncio
async def test_many_pipelines_in_one_pass_share_a_single_plugin_list_request(monkeypatch) -> None:
    worker = _Worker(_plugins())
    reconciler = reconciler_module.RuntimeReconciler()
    for _ in range(5):
        calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])
        _use_pipeline_with_a_template(monkeypatch)

        async def _resolve(pipeline):
            return {"connector.class": CONNECTOR}, "hash-new"

        monkeypatch.setattr(reconciler_module, "_resolve_config", _resolve)
        await reconciler._reconcile_row(
            _session := _FakeSession(live_desired_generation=1), _FakeRepo(_session), client=worker, existing=set(),
            row=_row(desired_state="running", desired_generation=1),
        )

    assert worker.plugin_calls == 1
