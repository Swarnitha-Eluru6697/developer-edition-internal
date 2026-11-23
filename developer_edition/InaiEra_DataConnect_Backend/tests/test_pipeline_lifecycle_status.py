import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import VALID_TRANSITIONS, pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient, RuntimeOrchestratorService


def _seed_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}}
    )
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict) -> dict:
    payload = PipelineCreate(
        name="Orders CDC",
        template_id=template["id"],
        connection_profile_id=connection["id"],
        zone="raw",
        domain="sales",
        source_system="orders_app",
        entity="orders",
        mode="cdc",
        version="v1",
    )
    return await pipeline_service.create_pipeline(org_id, payload)


async def _noop_record_desired(*args, **kwargs) -> None:
    return None


async def _noop_materialize_lineage(*args, **kwargs) -> dict:
    return {"nodes": [], "edges": []}


def test_valid_transitions_include_all_previously_missing_statuses() -> None:
    """deploying, pausing, resuming, deleting, degraded, unknown were the
    statuses explicitly called out as missing from the state machine."""
    for missing_status in ("deploying", "pausing", "resuming", "deleting", "degraded", "unknown"):
        assert missing_status in VALID_TRANSITIONS


def test_deleting_is_a_terminal_in_flight_state() -> None:
    """Nothing else may be requested while a delete is in flight."""
    assert VALID_TRANSITIONS["deleting"] == set()


def test_a_pipeline_in_a_transitional_state_rejects_a_second_request() -> None:
    """While "pausing" is in flight, requesting another pause (or anything
    but delete) must be rejected — operations are not instantaneous, so two
    concurrent requests for the same pipeline must not both proceed."""
    assert "pausing" not in VALID_TRANSITIONS["pausing"]
    assert VALID_TRANSITIONS["pausing"] == {"deleting"}


@pytest.mark.asyncio
async def test_deploy_without_runtime_enabled_goes_straight_to_final_state(monkeypatch, fake_postgres) -> None:
    """No reconciler is running in this mode, so there's nothing async to
    model — old, synchronous-completion behavior is preserved."""
    assert settings.runtime_deploy_enabled is False
    template, connection = _seed_org(fake_postgres, "org_life_1")
    created = await _create_pipeline("org_life_1", template, connection)

    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage",
        _noop_materialize_lineage,
    )

    result = await pipeline_service.deploy("org_life_1", created["id"])

    assert result["status"] == "deployed"


@pytest.mark.asyncio
async def test_deploy_with_runtime_enabled_is_transitional_not_final(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_life_2")
    created = await _create_pipeline("org_life_2", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage",
        _noop_materialize_lineage,
    )

    async def _fake_deploy_connector(self, connector_name, config):
        return {"config": config}

    async def _fake_list_connector_plugins(self):
        # GAP-05: deploy() preflight-checks against the live worker's
        # installed plugins before deploying — matches _seed_org's
        # JdbcSourceConnector template.
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _fake_deploy_connector)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    result = await pipeline_service.deploy("org_life_2", created["id"])

    assert result["status"] == "deploying"
    assert (await pipeline_service.get_or_404("org_life_2", created["id"]))["status"] == "deploying"


@pytest.mark.asyncio
async def test_pause_resume_restart_are_transitional_when_runtime_enabled(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_life_3")
    created = await _create_pipeline("org_life_3", template, connection)
    fake_postgres.pipelines[created["id"]].status = "deployed"

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )

    async def _fake_pause(self, pipeline):
        return {"status": "pausing"}

    async def _fake_resume(self, pipeline):
        return {"status": "resuming"}

    async def _fake_restart(self, pipeline):
        return {"status": "restarting"}

    monkeypatch.setattr(RuntimeOrchestratorService, "pause_pipeline", _fake_pause)
    monkeypatch.setattr(RuntimeOrchestratorService, "resume_pipeline", _fake_resume)
    monkeypatch.setattr(RuntimeOrchestratorService, "restart_pipeline", _fake_restart)

    paused = await pipeline_service.pause("org_life_3", created["id"])
    assert paused["status"] == "pausing"

    fake_postgres.pipelines[created["id"]].status = "paused"
    resumed = await pipeline_service.resume("org_life_3", created["id"])
    assert resumed["status"] == "resuming"

    fake_postgres.pipelines[created["id"]].status = "running"
    restarted = await pipeline_service.restart("org_life_3", created["id"])
    assert restarted["status"] == "restarting"


@pytest.mark.asyncio
async def test_delete_deployed_pipeline_is_transitional_when_runtime_enabled(monkeypatch, fake_postgres) -> None:
    """The row must survive the request — only the reconciler, once it
    confirms the connector is really gone from Kafka Connect, removes it."""
    template, connection = _seed_org(fake_postgres, "org_life_4")
    created = await _create_pipeline("org_life_4", template, connection)
    fake_postgres.pipelines[created["id"]].status = "running"

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )

    await pipeline_service.delete_pipeline("org_life_4", created["id"])

    still_there = await pipeline_service.get_pipeline("org_life_4", created["id"])
    assert still_there is not None
    assert still_there["status"] == "deleting"


@pytest.mark.asyncio
async def test_delete_draft_pipeline_is_immediate_even_with_runtime_enabled(monkeypatch, fake_postgres) -> None:
    """A pipeline that was never deployed has no Kafka Connect state to wait
    on, so it's removed immediately regardless of the runtime flag."""
    template, connection = _seed_org(fake_postgres, "org_life_5")
    created = await _create_pipeline("org_life_5", template, connection)  # still "draft"

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )

    await pipeline_service.delete_pipeline("org_life_5", created["id"])

    assert await pipeline_service.get_pipeline("org_life_5", created["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_pipeline_twice_is_rejected(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_life_6")
    created = await _create_pipeline("org_life_6", template, connection)
    fake_postgres.pipelines[created["id"]].status = "deleting"

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )

    with pytest.raises(HTTPException) as exc_info:
        await pipeline_service.delete_pipeline("org_life_6", created["id"])

    assert exc_info.value.status_code == 409
