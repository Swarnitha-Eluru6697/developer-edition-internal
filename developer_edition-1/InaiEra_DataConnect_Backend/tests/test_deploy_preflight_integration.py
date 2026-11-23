"""
GAP-05: proof that PipelineService.deploy() actually calls the preflight
check (app/services/deploy_preflight.py) BEFORE provisioning a topic,
recording desired state, or calling Kafka Connect's own deploy endpoint —
not just that run_deploy_preflight() works in isolation (see
tests/test_deploy_preflight.py for that).
"""

import pytest

from app.core.config import settings
from app.core.errors import ConnectorCompatibilityError
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient


def _seed_org(fake_postgres, org_id: str, **template_overrides) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}, **template_overrides},
    )
    connection = fake_postgres.create(
        "connection_profiles", {"org_id": org_id, "name": "Test Connection", "connection_config": {}}
    )
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict, **overrides) -> dict:
    payload = PipelineCreate(
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders",
        mode=overrides.pop("mode", "cdc"), version="v1", **overrides,
    )
    return await pipeline_service.create_pipeline(org_id, payload)


async def _noop_record_desired(*args, **kwargs) -> None:
    return None


async def _noop_materialize_lineage(*args, **kwargs) -> dict:
    return {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_deploy_is_blocked_before_any_side_effect_when_connector_class_not_installed(
    monkeypatch, fake_postgres
) -> None:
    template, connection = _seed_org(fake_postgres, "org_preflight_1")
    created = await _create_pipeline("org_preflight_1", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired)
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop_materialize_lineage
    )

    calls: list[str] = []

    async def _fake_list_connector_plugins(self):
        return []  # worker has nothing installed — the template's class is missing

    async def _tracking_deploy_connector(self, connector_name, config):
        calls.append("deploy_connector")
        return {"config": config}

    async def _tracking_record_desired(*args, **kwargs):
        calls.append("record_desired")

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _tracking_deploy_connector)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _tracking_record_desired)

    with pytest.raises(ConnectorCompatibilityError):
        await pipeline_service.deploy("org_preflight_1", created["id"])

    # Neither the durable desired-state record nor the live deploy call ever
    # happened — the whole point of running preflight first.
    assert calls == []
    assert (await pipeline_service.get_or_404("org_preflight_1", created["id"]))["status"] == "draft"


@pytest.mark.asyncio
async def test_deploy_succeeds_when_preflight_passes(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_preflight_2")
    created = await _create_pipeline("org_preflight_2", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired)
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop_materialize_lineage
    )

    async def _fake_list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    async def _fake_deploy_connector(self, connector_name, config):
        return {"config": config}

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _fake_deploy_connector)

    result = await pipeline_service.deploy("org_preflight_2", created["id"])

    assert result["status"] == "deploying"


@pytest.mark.asyncio
async def test_deploy_is_blocked_when_pipelines_mode_is_not_in_templates_supported_modes(
    monkeypatch, fake_postgres
) -> None:
    # FE-BUG-23: a mode/template mismatch is now rejected at CREATE time too
    # (pipeline_spec_validator.py's _validate_capabilities) — so this
    # pipeline is created against an unrestricted template (mode="cdc"
    # passes) and its pinned template_snapshot is mutated afterward, to
    # simulate a pipeline that predates supported_modes being curated for
    # its template (or one created before this field existed at all) — the
    # exact case this deploy-time check still exists as a safety net for,
    # per deploy()'s own comment on why it reads template_snapshot.
    template, connection = _seed_org(fake_postgres, "org_preflight_3")
    created = await _create_pipeline("org_preflight_3", template, connection, mode="cdc")
    fake_postgres.pipelines[created["id"]].template_snapshot["supported_modes"] = ["batch"]

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired)

    async def _fake_list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    with pytest.raises(ConnectorCompatibilityError) as exc_info:
        await pipeline_service.deploy("org_preflight_3", created["id"])

    assert exc_info.value.details["failures"][0]["check"] == "source_mode_supported"
