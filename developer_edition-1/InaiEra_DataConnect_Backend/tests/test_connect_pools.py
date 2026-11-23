"""
Kafka Connect runtime design: worker pools, internal topics, connector names (app/core/connect_pools.py,
app/services/connect_runtime.py, app/services/topic_naming.py).
"""

import pytest

from app.core.config import settings
from app.core.connect_pools import (
    INTERNAL_TOPIC_PARTITIONS,
    POOL_BY_ID,
    POOLS,
    SINK_POOL_ID,
    canonical_pool_id,
    expected_internal_topic_settings,
    internal_topic_names,
    worker_environment,
    worker_group_id,
)
from app.db.seed_templates import _seed_rows
from app.services import connect_runtime
from app.services.topic_naming import (
    TopicParts,
    build_connector_name,
    is_conforming_connector_name,
    parse_connector_name,
)

# ── the pool catalogue ─────────────────────────────────────────────────────────────────────────────────────────────


def test_there_are_exactly_the_six_designed_pools() -> None:
    assert [p.id for p in POOLS] == [
        "connect-cdc-pool", "connect-jdbc-pool", "connect-file-pool", "connect-api-pool", "connect-stream-pool",
        "connect-sink-pool",
    ]
    assert [p.short for p in POOLS] == ["cdc", "jdbc", "file", "api", "stream", "sink"]


def test_the_retired_queue_pool_resolves_to_the_stream_pool() -> None:
    assert canonical_pool_id("connect-queue-pool") == "connect-stream-pool"
    assert canonical_pool_id("connect-cdc-pool") == "connect-cdc-pool"
    assert canonical_pool_id("connect-nonsense-pool") is None
    assert canonical_pool_id(None) is None


# ── internal topics ────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pool", POOLS, ids=lambda p: p.short)
def test_each_pool_has_its_own_three_internal_topics_named_as_designed(pool) -> None:
    assert internal_topic_names(pool.id) == {
        "configs": f"_inai_connect_{pool.short}_configs",
        "offsets": f"_inai_connect_{pool.short}_offsets",
        "status": f"_inai_connect_{pool.short}_status",
    }


def test_no_two_pools_share_an_internal_topic_or_a_group() -> None:
    topics = [t for p in POOLS for t in internal_topic_names(p.id).values()]
    assert len(topics) == len(set(topics)) == 18
    assert len({worker_group_id(p.id) for p in POOLS}) == 6


def test_the_offsets_topic_has_more_partitions_than_configs_and_status() -> None:
    assert INTERNAL_TOPIC_PARTITIONS["offsets"] > INTERNAL_TOPIC_PARTITIONS["status"]
    assert INTERNAL_TOPIC_PARTITIONS["offsets"] > INTERNAL_TOPIC_PARTITIONS["configs"]
    assert INTERNAL_TOPIC_PARTITIONS["configs"] == 1  # Connect requires exactly one


@pytest.mark.parametrize("kind", ["configs", "offsets", "status"])
def test_internal_topics_are_compacted_replicated_and_min_isr_as_designed(kind, monkeypatch) -> None:
    expected = expected_internal_topic_settings(kind)

    assert expected["cleanup.policy"] == "compact"
    assert expected["replication_factor"] == 3  # the design's defaults
    assert expected["min.insync.replicas"] == 2
    monkeypatch.setattr(settings, "connect_internal_replication_factor", 1)
    assert expected_internal_topic_settings(kind)["replication_factor"] == 1  # a one-broker dev cluster overrides


def test_a_workers_environment_carries_its_pools_group_topics_and_replication() -> None:
    env = worker_environment("connect-cdc-pool", bootstrap="kafka:29092", advertised_host="connect-cdc")

    assert env["CONNECT_GROUP_ID"] == "inai-connect-cdc"
    assert env["CONNECT_CONFIG_STORAGE_TOPIC"] == "_inai_connect_cdc_configs"
    assert env["CONNECT_OFFSET_STORAGE_TOPIC"] == "_inai_connect_cdc_offsets"
    assert env["CONNECT_STATUS_STORAGE_TOPIC"] == "_inai_connect_cdc_status"
    assert env["CONNECT_OFFSET_STORAGE_PARTITIONS"] == "25"
    assert env["CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR"] == "3"


# ── connector names ────────────────────────────────────────────────────────────────────────────────────────────────


def _parts(**overrides) -> TopicParts:
    base = {"zone": "raw", "domain": "erp", "source_system": "sap", "entity": "customers", "mode": "cdc", "version": "v1"}
    base.update(overrides)
    return TopicParts(**base)


def test_the_connector_name_is_the_designed_example() -> None:
    assert build_connector_name(_parts()) == "erp--sap--customers--cdc--v1"


def test_a_connector_name_splits_back_into_its_six_parts() -> None:
    assert parse_connector_name("erp--sap--customers--cdc--v1") == {
        "domain": "erp", "source_system": "sap", "entity": "customers", "mode": "cdc", "version": "v1",
    }


def test_a_name_with_a_single_hyphen_inside_a_part_round_trips() -> None:
    name = build_connector_name(_parts(source_system="sap-erp"))

    assert parse_connector_name(name)["source_system"] == "sap-erp"


@pytest.mark.parametrize("field", ["domain", "source_system", "entity"])
@pytest.mark.parametrize("value", ["a--b", "-a", "a-"])
def test_a_part_that_would_make_the_name_ambiguous_is_refused(field, value) -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        build_connector_name(_parts(**{field: value}))


@pytest.mark.parametrize(
    "name",
    ["erp--sap--customers--cdc", "a--b--c--d--e--f--g", "erp--sap--cust--weird--v1", "erp--sap--cust--cdc--1",
     "ORG_1--erp--sap--cust--cdc--v1", ""],
)
def test_a_name_that_does_not_follow_the_format_is_not_conforming(name) -> None:
    assert not is_conforming_connector_name(name)


# ── the template catalogue obeys the design ────────────────────────────────────────────────────────────────────────


def test_every_seeded_template_names_a_real_pool() -> None:
    for template in _seed_rows():
        assert canonical_pool_id(template["runtime_pool_type"]) == template["runtime_pool_type"], template["id"]


def test_every_sink_runs_in_the_sink_pool_and_nothing_else_does() -> None:
    for template in _seed_rows():
        is_sink = template["connector_type"] == "sink"
        assert (template["runtime_pool_type"] == SINK_POOL_ID) == is_sink, template["id"]


def test_the_retired_queue_pool_is_not_used_by_any_template() -> None:
    assert all(t["runtime_pool_type"] != "connect-queue-pool" for t in _seed_rows())


# ── routing a pool to its cluster ──────────────────────────────────────────────────────────────────────────────────


def test_a_pool_without_its_own_cluster_runs_on_the_shared_one(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kafka_connect_url", "http://shared:8083/")
    monkeypatch.setattr(settings, "connect_pools", {})

    assert all(connect_runtime.pool_endpoint(p.id) == "http://shared:8083" for p in POOLS)
    assert connect_runtime.pool_endpoint(None) == "http://shared:8083"
    assert list(connect_runtime.endpoints()) == ["http://shared:8083"]


def test_each_configured_pool_routes_to_its_own_cluster(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kafka_connect_url", "http://shared:8083")
    monkeypatch.setattr(settings, "connect_pools", {"connect-cdc-pool": "http://connect-cdc:8083", "connect-sink-pool": "http://connect-sink:8083/"})

    assert connect_runtime.pool_endpoint("connect-cdc-pool") == "http://connect-cdc:8083"
    assert connect_runtime.pool_endpoint("connect-sink-pool") == "http://connect-sink:8083"
    assert connect_runtime.pool_endpoint("connect-jdbc-pool") == "http://shared:8083"  # not configured -> shared
    assert connect_runtime.client_for_pool("connect-cdc-pool").base_url == "http://connect-cdc:8083"
    grouped = connect_runtime.endpoints()
    assert grouped["http://connect-cdc:8083"] == ["connect-cdc-pool"]
    assert grouped["http://shared:8083"] == [
        "connect-jdbc-pool", "connect-file-pool", "connect-api-pool", "connect-stream-pool",
    ]


def test_the_retired_pool_id_routes_to_the_stream_pools_cluster(monkeypatch) -> None:
    monkeypatch.setattr(settings, "connect_pools", {"connect-stream-pool": "http://connect-stream:8083"})

    assert connect_runtime.pool_endpoint("connect-queue-pool") == "http://connect-stream:8083"


def test_a_pipelines_client_is_the_cluster_of_its_pool(monkeypatch) -> None:
    monkeypatch.setattr(settings, "connect_pools", {"connect-cdc-pool": "http://connect-cdc:8083"})

    assert connect_runtime.client_for_pipeline({"runtime_pool_id": "connect-cdc-pool"}).base_url == "http://connect-cdc:8083"
    assert connect_runtime.pool_of({"runtime_pool_id": "connect-queue-pool"}) == "connect-stream-pool"
    assert connect_runtime.pool_of({}) is None


def test_a_template_places_its_connectors_in_its_declared_pool() -> None:
    assert connect_runtime.pool_for_template({"runtime_pool_type": "connect-cdc-pool"}) == "connect-cdc-pool"
    assert connect_runtime.pool_for_template({"runtime_pool_type": "connect-queue-pool"}) == "connect-stream-pool"


@pytest.mark.parametrize("declared", [None, "", "connect-nonsense-pool"])
def test_a_template_naming_no_real_pool_is_refused(declared) -> None:
    with pytest.raises(ValueError, match="unknown worker pool"):
        connect_runtime.pool_for_template({"runtime_pool_type": declared})


def test_a_misconfigured_pool_map_is_reported_at_startup(monkeypatch) -> None:
    monkeypatch.setattr(settings, "connect_pools", {"connect-bogus-pool": "http://x:8083", "connect-cdc-pool": "connect-cdc:8083"})

    problems = connect_runtime.configuration_problems()

    assert any("unknown pool 'connect-bogus-pool'" in p for p in problems)
    assert any("not an http(s) URL" in p for p in problems)
    monkeypatch.setattr(settings, "connect_pools", {"connect-cdc-pool": "http://connect-cdc:8083"})
    assert connect_runtime.configuration_problems() == []


def test_pool_ids_are_the_known_set() -> None:
    assert set(POOL_BY_ID) == {p.id for p in POOLS}
