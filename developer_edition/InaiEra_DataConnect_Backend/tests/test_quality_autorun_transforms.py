"""The create-time quality sample (PipelineService.autorun_quality_from_source) reads rows straight from the source table. A quality policy
checks records as they are on the topic, after the producing pipeline's transformations, so for a pipeline that transforms its records the
sample would be judged on fields that do not exist yet (renamed, added, masked or converted) — every rule on such a field failed, and that
"50/50 failed" went to UCS as the topic's quality. Such a pipeline is not sampled: the quality stream worker checks the real records."""
import pytest

from app.services import connection_discovery
from app.services.pipeline_service import pipeline_service
from app.services.quality_service import quality_service

POLICY = {"id": "dqp_1", "name": "checks", "rules": []}
SOURCE = {"id": "p_src", "org_id": "org_1", "quality_policy_id": "dqp_1", "connection_profile_id": "c1", "entity": "products",
          "connector_config": {"tables": ["products"]}}


@pytest.fixture
def harness(monkeypatch):
    calls = {"skipped": [], "sampled": 0}

    async def _get_policy(org_id, policy_id):
        return POLICY

    async def _skip(org_id, pipeline, policy, reason):
        calls["skipped"].append(reason)
        return {"status": "skipped"}

    async def _sample(*args, **kwargs):
        calls["sampled"] += 1
        return {"records": []}

    async def _connection(org_id, connection_id):
        return {"id": connection_id}

    async def _hydrate(connection):
        return connection

    monkeypatch.setattr(quality_service, "get_policy", _get_policy)
    monkeypatch.setattr(pipeline_service, "_record_quality_skip", _skip)
    monkeypatch.setattr(connection_discovery, "sample_rows", _sample)
    monkeypatch.setattr("app.services.pipeline_service.connection_service.get_connection", _connection)
    monkeypatch.setattr("app.services.pipeline_service.hydrate_connection_secrets", _hydrate)
    return calls


@pytest.mark.asyncio
async def test_a_source_that_transforms_its_records_is_not_sampled(harness) -> None:
    await pipeline_service.autorun_quality_from_source("org_1", {**SOURCE, "transform_policy_id": "tp_1"})

    assert harness["sampled"] == 0
    assert len(harness["skipped"]) == 1
    assert "transforms records before they reach the topic" in harness["skipped"][0]


@pytest.mark.asyncio
async def test_a_sink_whose_producer_transforms_its_records_is_not_sampled(harness, monkeypatch) -> None:
    async def _upstream(org_id, topic):
        return {**SOURCE, "transform_policy_id": "tp_1"}
    monkeypatch.setattr(pipeline_service, "_find_pipeline_by_topic", _upstream)
    sink = {"id": "p_sink", "org_id": "org_1", "quality_policy_id": "dqp_1", "connection_profile_id": "c2", "entity": "products_sink",
            "connector_config": {"topics": "raw-shop-pg-products-batch-v1"}}   # the sink's own transforms run after the topic

    await pipeline_service.autorun_quality_from_source("org_1", sink)

    assert harness["sampled"] == 0
    assert "transforms records before they reach the topic" in harness["skipped"][0]


@pytest.mark.asyncio
async def test_a_source_without_transformations_is_still_sampled(harness) -> None:
    await pipeline_service.autorun_quality_from_source("org_1", SOURCE)

    assert harness["sampled"] == 1                       # sampled; the empty sample is then recorded as its own skip
    assert "transforms records" not in harness["skipped"][0]
