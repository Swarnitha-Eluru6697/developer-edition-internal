"""
GAP-11: every Operation.operation_type now has a typed result contract
(app/schemas/operation_results.py) instead of accepting arbitrary JSON.
Two concrete bugs this fixes, both confirmed by reading the actual code
before writing these tests (not assumed):

1. pipeline_deploy's result used to embed `runtime_response`, the raw
   Kafka Connect PUT response (an echo of the full connector config,
   unbounded for a wide SMT chain) — nothing internal or in the frontend
   ever read it.
2. pipeline_pause/resume/restart's runtime sub-dict came from
   RuntimeOrchestratorService, which keys the connector's status under
   "status" — but the fallback path (runtime_deploy_enabled=False, or no
   runtime dict) used "state". A client reading `.runtime.state` off a
   pause/resume/restart result got nothing when runtime_deploy_enabled was
   on. Both now normalize into PipelineRuntimeSummary.state.
"""

import pytest

from app.core.config import settings
from app.schemas.operation_results import (
    PipelineDeployResult,
    PipelinePauseResult,
    PipelineRestartResult,
    PipelineResumeResult,
)
from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service
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
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    )
    return await pipeline_service.create_pipeline(org_id, payload)


async def _noop_record_desired(*args, **kwargs) -> None:
    return None


async def _noop_materialize_lineage(*args, **kwargs) -> dict:
    return {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_deploy_result_validates_against_its_typed_contract_and_has_no_raw_connect_response(
    monkeypatch, fake_postgres,
) -> None:
    template, connection = _seed_org(fake_postgres, "org_result_1")
    created = await _create_pipeline("org_result_1", template, connection)

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired)
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop_materialize_lineage,
    )

    async def _fake_deploy_connector(self, connector_name, config):
        # A realistic Kafka Connect response — echoes the full config back,
        # the exact shape that used to get embedded whole as runtime_response.
        return {"name": connector_name, "config": {**config, "connection.password": "supersecret"}, "tasks": []}

    async def _fake_list_connector_plugins(self):
        return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.7.4"}]

    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _fake_deploy_connector)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _fake_list_connector_plugins)

    result = await pipeline_service.deploy("org_result_1", created["id"])

    # Validates against the typed contract — a stray extra top-level field
    # (like the old runtime_response) would be silently dropped by
    # model_validate (extra="ignore" is the Pydantic v2 default), so this
    # alone doesn't prove absence; the explicit key check below does.
    validated = PipelineDeployResult.model_validate(result)
    assert validated.runtime.state == "deployed"
    assert validated.runtime.connector == created["connector_name"]

    assert "runtime_response" not in result["runtime"]
    assert "supersecret" not in str(result)  # the credential-bearing echo never makes it in at all


@pytest.mark.asyncio
async def test_pause_resume_restart_results_normalize_state_from_the_orchestrators_status_key(
    monkeypatch, fake_postgres,
) -> None:
    """Regression for the real pre-existing bug: RuntimeOrchestratorService.
    pause_pipeline/resume_pipeline/restart_pipeline key the connector's
    status under "status", not "state" — before this fix, a pause/resume/
    restart result's `runtime` dict had NO "state" key at all when
    runtime_deploy_enabled was on (it silently kept whatever "status" said
    instead, under a totally different key than deploy's own runtime used)."""
    template, connection = _seed_org(fake_postgres, "org_result_2")
    created = await _create_pipeline("org_result_2", template, connection)
    fake_postgres.pipelines[created["id"]].status = "deployed"

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop_record_desired)

    async def _fake_pause(self, pipeline):
        return {"pipeline_id": pipeline["id"], "connector_name": pipeline["connector_name"], "status": "pausing"}

    async def _fake_resume(self, pipeline):
        return {"pipeline_id": pipeline["id"], "connector_name": pipeline["connector_name"], "status": "resuming"}

    async def _fake_restart(self, pipeline):
        return {"pipeline_id": pipeline["id"], "connector_name": pipeline["connector_name"], "status": "restarting"}

    monkeypatch.setattr(RuntimeOrchestratorService, "pause_pipeline", _fake_pause)
    monkeypatch.setattr(RuntimeOrchestratorService, "resume_pipeline", _fake_resume)
    monkeypatch.setattr(RuntimeOrchestratorService, "restart_pipeline", _fake_restart)

    paused = await pipeline_service.pause("org_result_2", created["id"])
    validated = PipelinePauseResult.model_validate(paused)
    assert validated.runtime.state == "pausing"  # not missing, not under "status"
    assert validated.runtime.connector == created["connector_name"]

    fake_postgres.pipelines[created["id"]].status = "paused"
    resumed = await pipeline_service.resume("org_result_2", created["id"])
    assert PipelineResumeResult.model_validate(resumed).runtime.state == "resuming"

    fake_postgres.pipelines[created["id"]].status = "running"
    restarted = await pipeline_service.restart("org_result_2", created["id"])
    assert PipelineRestartResult.model_validate(restarted).runtime.state == "restarting"


@pytest.mark.asyncio
async def test_lifecycle_results_fall_back_to_target_status_when_runtime_deploy_disabled(
    monkeypatch, fake_postgres,
) -> None:
    """settings.runtime_deploy_enabled defaults False in tests — no
    orchestrator call happens at all, so `runtime` must fall back to the
    final target status rather than being left empty/malformed."""
    template, connection = _seed_org(fake_postgres, "org_result_3")
    created = await _create_pipeline("org_result_3", template, connection)
    monkeypatch.setattr(
        "app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop_materialize_lineage,
    )

    deployed = await pipeline_service.deploy("org_result_3", created["id"])
    validated = PipelineDeployResult.model_validate(deployed)
    assert validated.status == "deployed"
    assert validated.runtime.state == "deployed"
