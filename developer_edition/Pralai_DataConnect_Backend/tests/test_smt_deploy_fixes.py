"""Three SMT problems found by deploying a real Custom SMT on sql_practice.products (docs/CONNECTOR_TEST_LOG.md):

* HeaderFrom could not deploy: the backend asked Kafka Connect for the abstract class, not HeaderFrom$Key / HeaderFrom$Value.
* Debezium's ExtractNewRecordState was sent last (the form lists it last), so every field transform ran on the change envelope.
* A RegexRouter on a source did nothing (the platform's own routing to the pipeline's topic ran after it); a source now refuses it.
"""

import pytest

from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate
from app.services.connect_config import DBZ_PG, _unwrap_first, build_connector
from app.services.pipeline_service import pipeline_service
from app.services.pipeline_spec_validator import validate_pipeline_references
from app.services.transform_policy import (
    SMT_TYPES,
    SOURCE_REFUSED_TRANSFORMS,
    generate_smt_config,
    source_transform_problems,
)

UNWRAP = "io.debezium.transforms.ExtractNewRecordState"


def _step(order: int, name: str, kind: str, scope: str = "value", **config) -> dict:
    return {"order": order, "name": name, "type": kind, "scope": scope, "config": config}


# ── HeaderFrom ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_header_from_deploys_as_its_value_or_key_variant():
    value = generate_smt_config([_step(1, "h", "HeaderFrom", fields="category", headers="product_category", operation="copy")])
    assert value["transforms.h.type"] == "org.apache.kafka.connect.transforms.HeaderFrom$Value"
    key = generate_smt_config([_step(1, "h", "HeaderFrom", scope="key", fields="id", headers="k", operation="move")])
    assert key["transforms.h.type"] == "org.apache.kafka.connect.transforms.HeaderFrom$Key"


def test_every_type_the_form_offers_generates_a_class_kafka_connect_can_instantiate():
    # Kafka Connect ships these as an abstract class plus $Key/$Value variants — and only these are plain, concrete classes.
    concrete_as_is = {"ValueToKey", "RegexRouter", "ExtractNewRecordState"}
    for kind in SMT_TYPES:
        cfg = generate_smt_config([_step(1, "s", kind)])
        cls = cfg["transforms.s.type"]
        assert (cls.endswith(("$Key", "$Value")) if kind not in concrete_as_is else "$" not in cls), f"{kind} -> {cls}"


# ── the Debezium unwrap runs first ───────────────────────────────────────────────────────────────────────────────────────

def test_an_explicit_unwrap_moves_to_the_front_and_the_rest_keep_their_order():
    smt = generate_smt_config([
        _step(1, "keyById", "ValueToKey", fields="id"), _step(2, "mask", "MaskField", fields="email"), _step(3, "flat", "ExtractNewRecordState"),
    ])
    assert smt["transforms"] == "keyById,mask,flat"
    fixed = _unwrap_first(smt)
    assert fixed["transforms"] == "flat,keyById,mask"
    assert fixed["transforms.flat.type"] == UNWRAP                      # nothing else about it changed
    assert {k: v for k, v in fixed.items() if k != "transforms"} == {k: v for k, v in smt.items() if k != "transforms"}


def test_nothing_changes_when_there_is_no_unwrap_or_it_is_already_first():
    plain = generate_smt_config([_step(1, "mask", "MaskField", fields="email")])
    assert _unwrap_first(plain) is plain
    first = generate_smt_config([_step(1, "u", "ExtractNewRecordState"), _step(2, "mask", "MaskField", fields="email")])
    assert _unwrap_first(first) is first
    assert _unwrap_first({}) == {}


@pytest.mark.asyncio
async def test_a_debezium_connector_runs_the_unwrap_before_the_users_field_transforms(fake_postgres):
    pipeline = {
        "id": "p1", "org_id": "org_1", "domain": "d", "source_system": "pg", "entity": "products", "version": "v1", "connector_name": "raw--d--pg--products--cdc--v1",
        "topic_name": "raw-d-pg-products-cdc-v1", "connector_config": {},
    }
    connection = {"connection_config": {"hostname": "db", "database": "shop", "username": "u", "password": "p"}}
    policy = {"transforms": [_step(1, "mask", "MaskField", fields="sku"), _step(2, "flat", "ExtractNewRecordState")]}
    config, _ = await build_connector(pipeline, connection, {"connector_class": DBZ_PG}, schema=None, transform_policy=policy)
    names = config["transforms"].split(",")
    assert names == ["flat", "mask", "reroute"]                          # unwrap, the user's transform, then the platform's reroute last
    assert config["transforms.flat.type"] == UNWRAP


# ── a source refuses RegexRouter ─────────────────────────────────────────────────────────────────────────────────────────

def test_source_transform_problems_names_regex_router_once():
    steps = [_step(1, "a", "RegexRouter", regex="(.*)", replacement="$1"), _step(2, "b", "RegexRouter", regex="x", replacement="y"), _step(3, "m", "MaskField", fields="x")]
    problems = source_transform_problems(steps)
    assert len(problems) == 1
    assert problems[0].startswith("RegexRouter: ")
    assert "sink" in problems[0]
    assert source_transform_problems([_step(1, "m", "MaskField", fields="x")]) == []
    assert set(SOURCE_REFUSED_TRANSFORMS) == {"RegexRouter"}


def _payload(**overrides) -> PipelineCreate:
    base = {"name": "p", "template_id": "t", "connection_profile_id": "c", "zone": "raw", "domain": "d", "source_system": "s", "entity": "e", "mode": "cdc", "version": "v1"}
    return PipelineCreate(**{**base, **overrides})


@pytest.mark.asyncio
async def test_creating_a_source_pipeline_with_a_regex_router_policy_is_refused(fake_postgres):
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "source"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    bad = fake_postgres.create("transform_policies", {"org_id": "org_a", "transforms": [_step(1, "r", "RegexRouter", regex="(.*)", replacement="$1")]})
    good = fake_postgres.create("transform_policies", {"org_id": "org_a", "transforms": [_step(1, "m", "MaskField", fields="x")]})

    with pytest.raises(ValidationError) as refused:
        await validate_pipeline_references("org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"], transform_policy_id=bad["id"]))
    assert "RegexRouter can't be used on a source" in str(refused.value)

    refs = await validate_pipeline_references("org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"], transform_policy_id=good["id"]))
    assert refs.transform["id"] == good["id"]


@pytest.mark.asyncio
async def test_a_sink_may_use_a_regex_router(fake_postgres):
    template = fake_postgres.create("connector_templates", {"connector_class": "x", "connector_type": "sink"})
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a"})
    policy = fake_postgres.create("transform_policies", {"org_id": "org_a", "transforms": [_step(1, "r", "RegexRouter", regex="(.*)", replacement="$1")]})
    payload = _payload(
        template_id=template["id"], connection_profile_id=connection["id"], transform_policy_id=policy["id"], mode="batch",
        connector_config={"topics": "raw-d-s-e-batch-v1"},
    )
    refs = await validate_pipeline_references("org_a", payload)
    assert refs.transform["id"] == policy["id"]


@pytest.mark.asyncio
async def test_a_policy_edited_after_it_was_attached_is_caught_at_deploy_readiness(fake_postgres):
    template = fake_postgres.create(
        "connector_templates", {"connector_class": "io.confluent.connect.jdbc.JdbcSourceConnector", "connector_type": "source", "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": "org_a", "name": "c", "connection_config": {}, "verification_status": "ok"})
    policy = fake_postgres.create("transform_policies", {"org_id": "org_a", "transforms": [_step(1, "m", "MaskField", fields="x")]})
    created = await pipeline_service.create_pipeline("org_a", _payload(template_id=template["id"], connection_profile_id=connection["id"], transform_policy_id=policy["id"]))

    ready = await pipeline_service.check_deploy_readiness("org_a", created["id"])
    assert next(c for c in ready["checks"] if c["key"] == "transform_configuration_valid")["ok"] is True

    # someone edits the shared SMT (the fake keeps its rows in fake_postgres.transform_policies)
    fake_postgres.transform_policies[policy["id"]].transforms = [_step(1, "m", "MaskField", fields="x"), _step(2, "r", "RegexRouter", regex="(.*)", replacement="$1")]
    ready = await pipeline_service.check_deploy_readiness("org_a", created["id"])
    check = next(c for c in ready["checks"] if c["key"] == "transform_configuration_valid")
    assert check["ok"] is False
    assert "RegexRouter can't be used on a source" in check["reason"]
    assert ready["ready"] is False
