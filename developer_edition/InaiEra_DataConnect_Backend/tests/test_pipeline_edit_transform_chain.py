"""Editing a pipeline can change its SMT chain, switch it to another, or remove it — "no transformations" (null) clears the link."""

import pytest
from test_pipeline_direction import _payload, _seed_source

from app.schemas.pipeline import PipelineUpdate
from app.services.pipeline_service import pipeline_service

STEP = {"order": 1, "name": "k", "type": "ValueToKey", "scope": "value", "config": {"fields": "id"}}


@pytest.mark.asyncio
async def test_a_pipeline_can_be_given_a_chain_switched_to_another_and_then_have_it_removed(fake_postgres) -> None:
    template, connection = _seed_source(fake_postgres, "org_chain_1")
    first = fake_postgres.create("transform_policies", {"org_id": "org_chain_1", "name": "one", "transforms": [STEP], "origin": "custom"})
    second = fake_postgres.create("transform_policies", {"org_id": "org_chain_1", "name": "two", "transforms": [STEP], "origin": "custom"})
    created = await pipeline_service.create_pipeline("org_chain_1", _payload(template, connection))
    assert created["transform_policy_id"] is None

    given = await pipeline_service.update_pipeline("org_chain_1", created["id"], PipelineUpdate(transform_policy_id=first["id"]))
    assert given["transform_policy_id"] == first["id"]
    assert "transforms" in given["generated_config"]                       # the connector config now carries the chain

    switched = await pipeline_service.update_pipeline("org_chain_1", created["id"], PipelineUpdate(transform_policy_id=second["id"]))
    assert switched["transform_policy_id"] == second["id"]

    removed = await pipeline_service.update_pipeline("org_chain_1", created["id"], PipelineUpdate(transform_policy_id=None))
    assert removed["transform_policy_id"] is None                          # null clears the link, it is not "leave it as it was"
    assert "transforms" not in removed["generated_config"]


@pytest.mark.asyncio
async def test_an_edit_that_does_not_mention_the_chain_leaves_it_alone(fake_postgres) -> None:
    template, connection = _seed_source(fake_postgres, "org_chain_2")
    policy = fake_postgres.create("transform_policies", {"org_id": "org_chain_2", "name": "one", "transforms": [STEP], "origin": "custom"})
    created = await pipeline_service.create_pipeline("org_chain_2", _payload(template, connection, transform_policy_id=policy["id"]))

    renamed = await pipeline_service.update_pipeline("org_chain_2", created["id"], PipelineUpdate(description="a note"))
    assert renamed["transform_policy_id"] == policy["id"]
