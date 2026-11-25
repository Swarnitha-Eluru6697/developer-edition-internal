"""
FE-BUG-07: PipelineService.list_pipelines/get_pipeline enrich their response
with the reconciler's own desired-vs-observed state (app/models/
pipeline_runtime_state.py) — desired_state/observed_state/last_observed_at/
runtime_error — instead of leaving a caller to infer reconciliation status
from dc_pipelines' own `status` column alone, which only ever reflects the
last successfully-mapped outcome and says nothing about whether that's
fresh or how it was derived.
"""

from datetime import UTC, datetime

import pytest

from app.schemas.pipeline import PipelineCreate
from app.services.pipeline_service import pipeline_service


def _seed_org(fake_postgres, org_id: str) -> tuple[dict, dict]:
    template = fake_postgres.create(
        "connector_templates",
        {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": org_id, "connection_config": {}})
    return template, connection


async def _create_pipeline(org_id: str, template: dict, connection: dict) -> dict:
    payload = PipelineCreate(
        name="Orders CDC", template_id=template["id"], connection_profile_id=connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
    )
    return await pipeline_service.create_pipeline(org_id, payload)


@pytest.mark.asyncio
async def test_a_never_deployed_pipeline_has_no_runtime_state_fields(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_rt_1")
    created = await _create_pipeline("org_rt_1", template, connection)

    fetched = await pipeline_service.get_pipeline("org_rt_1", created["id"])

    assert fetched["desired_state"] is None
    assert fetched["observed_state"] is None
    assert fetched["last_observed_at"] is None
    assert fetched["runtime_error"] is None


@pytest.mark.asyncio
async def test_get_pipeline_surfaces_the_reconcilers_desired_and_observed_state(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_rt_2")
    created = await _create_pipeline("org_rt_2", template, connection)
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    fake_postgres.create(
        "pipeline_runtime_states",
        {
            "org_id": "org_rt_2", "pipeline_id": created["id"], "connector_name": created["connector_name"],
            "desired_state": "running", "desired_generation": 3, "observed_state": "paused",
            "observed_generation": 2, "last_observed_at": observed_at, "last_runtime_error": None,
        },
    )

    fetched = await pipeline_service.get_pipeline("org_rt_2", created["id"])

    # Desired and observed can genuinely disagree mid-reconciliation — the
    # whole point is exposing that distinction, not collapsing it.
    assert fetched["desired_state"] == "running"
    assert fetched["observed_state"] == "paused"
    assert fetched["last_observed_at"] == observed_at
    assert fetched["runtime_error"] is None


@pytest.mark.asyncio
async def test_get_pipeline_surfaces_a_runtime_error_when_connect_is_unreachable(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_rt_3")
    created = await _create_pipeline("org_rt_3", template, connection)
    stale_observation = datetime(2026, 1, 1, tzinfo=UTC)
    fake_postgres.create(
        "pipeline_runtime_states",
        {
            "org_id": "org_rt_3", "pipeline_id": created["id"], "connector_name": created["connector_name"],
            "desired_state": "running", "desired_generation": 1, "observed_state": "unknown",
            "observed_generation": 0, "last_observed_at": stale_observation,
            "last_runtime_error": "Could not reach Kafka Connect.",
        },
    )

    fetched = await pipeline_service.get_pipeline("org_rt_3", created["id"])

    assert fetched["desired_state"] == "running"
    assert fetched["observed_state"] == "unknown"
    # A stale, honest timestamp — not silently refreshed to "now" just
    # because a request happened to come in.
    assert fetched["last_observed_at"] == stale_observation
    assert fetched["runtime_error"] == "Could not reach Kafka Connect."


@pytest.mark.asyncio
async def test_list_pipelines_batches_runtime_state_across_multiple_rows(fake_postgres) -> None:
    template, connection = _seed_org(fake_postgres, "org_rt_4")
    deployed = await _create_pipeline("org_rt_4", template, connection)
    fake_postgres.create(
        "pipeline_runtime_states",
        {
            "org_id": "org_rt_4", "pipeline_id": deployed["id"], "connector_name": deployed["connector_name"],
            "desired_state": "running", "desired_generation": 1, "observed_state": "running",
            "observed_generation": 1, "last_observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    draft_template, draft_connection = _seed_org(fake_postgres, "org_rt_4")
    draft = await _create_pipeline("org_rt_4", draft_template, draft_connection)

    pipelines = {p["id"]: p for p in await pipeline_service.list_pipelines("org_rt_4")}

    assert pipelines[deployed["id"]]["desired_state"] == "running"
    assert pipelines[deployed["id"]]["observed_state"] == "running"
    assert pipelines[draft["id"]]["desired_state"] is None
    assert pipelines[draft["id"]]["observed_state"] is None
