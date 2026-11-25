"""
The ticket this closes: production DataConnect behavior must explicitly
create/validate a pipeline's topic (and record the resolved TopicPolicy)
BEFORE the connector is deployed, never relying on the broker's implicit
KAFKA_AUTO_CREATE_TOPICS_ENABLE — see app/services/kafka_admin.py.

These tests exercise PipelineService.deploy() itself (not kafka_admin.py's
own logic, covered by test_kafka_admin.py), monkeypatching
provision_topic_and_record_audit so no real Kafka Admin API or database
connection is needed. Connection profiles/pipelines go through the
`fake_postgres` fixture (see conftest.py) since both are now
Postgres-authoritative.
"""

import pytest

from app.core.config import settings
from app.schemas.pipeline import PipelineCreate
from app.services.kafka_admin import TopicProvisioningError
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient


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
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    )
    return await pipeline_service.create_pipeline(org_id, payload)


async def _noop_record_desired(*args, **kwargs) -> None:
    return None


async def _noop_materialize_lineage(*args, **kwargs) -> dict:
    return {"nodes": [], "edges": []}


async def _fake_deploy_connector(self, connector_name, config):
    return {"config": config}


async def _fake_list_connector_plugins(self):
    # GAP-05: deploy() now preflight-checks the connector class against the
    # live worker's installed plugins before ever provisioning a topic or
    # calling deploy_connector — matches _seed_org's JdbcSourceConnector
    # template. No "transformation"/"converter" entries here is deliberate:
    # it exercises deploy_preflight's graceful skip for those two checks on
    # a worker that doesn't report them, same as any pre-3.5 Connect cluster.
    return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]


@pytest.mark.asyncio
async def test_deploy_does_not_provision_topics_by_default(monkeypatch, fake_postgres) -> None:
    """topic_admin_provisioning_enabled defaults to False — development
    keeps relying on the Compose broker's auto-create, unchanged."""
    assert settings.topic_admin_provisioning_enabled is False
    template, connection = _seed_org(fake_postgres, "org_topic_deploy_1")
    created = await _create_pipeline("org_topic_deploy_1", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage",
        _noop_materialize_lineage,
    )
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _fake_deploy_connector)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    calls = []

    async def _tracking_provision(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("app.services.kafka_admin.provision_topic_and_record_audit", _tracking_provision)

    await pipeline_service.deploy("org_topic_deploy_1", created["id"])

    assert calls == []


@pytest.mark.asyncio
async def test_deploy_provisions_the_topic_before_deploying_when_enabled(monkeypatch, fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_topic_deploy_2")
    created = await _create_pipeline("org_topic_deploy_2", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(settings, "topic_admin_provisioning_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage",
        _noop_materialize_lineage,
    )

    call_order = []

    async def _tracking_provision(org_id, pipeline, topic, topic_policy):
        call_order.append("provision")
        assert org_id == "org_topic_deploy_2"
        assert topic == pipeline["topic_name"]

    async def _tracking_deploy_connector(self, connector_name, config):
        call_order.append("deploy_connector")
        return {"config": config}

    monkeypatch.setattr("app.services.kafka_admin.provision_topic_and_record_audit", _tracking_provision)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _tracking_deploy_connector)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    await pipeline_service.deploy("org_topic_deploy_2", created["id"])

    assert call_order == ["provision", "deploy_connector"]


@pytest.mark.asyncio
async def test_deploy_aborts_when_topic_provisioning_fails(monkeypatch, fake_postgres) -> None:
    """A genuine provisioning failure must abort the deploy — never fall
    through to deploying the connector against an unprovisioned topic."""
    template, connection = _seed_org(fake_postgres, "org_topic_deploy_3")
    created = await _create_pipeline("org_topic_deploy_3", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(settings, "topic_admin_provisioning_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired
    )

    async def _failing_provision(*args, **kwargs):
        raise TopicProvisioningError("broker unreachable")

    deploy_calls = []

    async def _tracking_deploy_connector(self, connector_name, config):
        deploy_calls.append(connector_name)
        return {"config": config}

    monkeypatch.setattr("app.services.kafka_admin.provision_topic_and_record_audit", _failing_provision)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _tracking_deploy_connector)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    with pytest.raises(TopicProvisioningError):
        await pipeline_service.deploy("org_topic_deploy_3", created["id"])

    assert deploy_calls == []
