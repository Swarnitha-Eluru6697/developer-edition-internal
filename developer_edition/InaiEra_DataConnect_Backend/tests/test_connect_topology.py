"""
Each worker pool's Kafka Connect internal topics: checked against the runtime design (compacted, replicated, min-ISR, offsets with
more partitions than configs/status) and provisioned with exactly those settings (app/services/connect_topology.py).
"""

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient
from kafka.errors import KafkaError, TopicAlreadyExistsError

import app.services.connect_topology as topology
from app.core.config import settings
from app.core.connect_pools import internal_topic_names
from app.main import app
from app.services.connect_topology import provision_internal_topics, verify_topology

CDC = "connect-cdc-pool"


class _Admin:
    """A Kafka admin client over an in-memory cluster: {topic: (partitions, replication, configs)}."""

    def __init__(self, cluster: dict, create_errors: dict | None = None) -> None:
        self.cluster = cluster
        self.created: list = []
        self.create_errors = create_errors or {}
        self.closed = False

    def describe_topics(self, names):
        out = []
        for name in names:
            if name not in self.cluster:
                out.append({"error_code": 3, "name": name, "partitions": []})
                continue
            partitions, replication, _ = self.cluster[name]
            out.append({
                "error_code": 0, "name": name,
                "partitions": [{"partition_index": i, "replica_nodes": list(range(replication))} for i in range(partitions)],
            })
        return out

    def describe_configs(self, resources):
        return {"topic": {r.name: {k: {"value": v} for k, v in self.cluster[r.name][2].items()} for r in resources}}

    def create_topics(self, new_topics):
        for topic in new_topics:
            if topic.name in self.create_errors:
                raise self.create_errors[topic.name]
            self.created.append(topic)
            self.cluster[topic.name] = (
                topic.num_partitions, topic.replication_factor, dict(topic.topic_configs),
            )

    def close(self):
        self.closed = True


def _healthy(pool_id: str = CDC, **overrides) -> dict:
    names = internal_topic_names(pool_id)
    base = {"configs": (1, 3, {"cleanup.policy": "compact", "min.insync.replicas": "2"}),
            "status": (5, 3, {"cleanup.policy": "compact", "min.insync.replicas": "2"}),
            "offsets": (25, 3, {"cleanup.policy": "compact", "min.insync.replicas": "2"})}
    base.update(overrides)
    return {names[kind]: spec for kind, spec in base.items()}


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setattr(settings, "connect_pools", {CDC: "http://connect-cdc:8083"})
    state = {"admin": _Admin(_healthy())}
    monkeypatch.setattr(topology, "KafkaAdminClient", lambda **kwargs: state["admin"])
    return state


def _problems(report) -> dict[str, list[str]]:
    return {t.kind: t.problems for t in report.topics if t.problems}


@pytest.mark.asyncio
async def test_a_pool_whose_topics_match_the_design_is_ok(cluster) -> None:
    [report] = await verify_topology([CDC])

    assert report.ok
    assert report.cluster_mode == "dedicated"
    assert [t.topic for t in report.topics] == list(internal_topic_names(CDC).values())
    assert cluster["admin"].closed


@pytest.mark.asyncio
async def test_a_topic_that_does_not_exist_is_reported(cluster) -> None:
    del cluster["admin"].cluster["_inai_connect_cdc_offsets"]

    [report] = await verify_topology([CDC])

    assert not report.ok
    assert _problems(report) == {"offsets": ["topic does not exist"]}


@pytest.mark.asyncio
async def test_each_way_a_topic_can_miss_the_design_is_named(cluster) -> None:
    cluster["admin"].cluster.update(_healthy(
        configs=(3, 3, {"cleanup.policy": "compact", "min.insync.replicas": "2"}),  # config topic must be one partition
        status=(5, 1, {"cleanup.policy": "delete", "min.insync.replicas": "2"}),  # under-replicated, not compacted
        offsets=(25, 3, {"cleanup.policy": "compact"}),  # min.insync.replicas unset
    ))

    [report] = await verify_topology([CDC])

    problems = _problems(report)
    assert any("must have exactly 1" in p for p in problems["configs"])
    assert any("replication factor is 1" in p for p in problems["status"])
    assert any("cleanup.policy is delete" in p for p in problems["status"])
    assert any("min.insync.replicas is not set" in p for p in problems["offsets"])


@pytest.mark.asyncio
async def test_a_min_isr_below_the_designed_value_is_reported(cluster) -> None:
    cluster["admin"].cluster.update(_healthy(status=(5, 3, {"cleanup.policy": "compact", "min.insync.replicas": "1"})))

    [report] = await verify_topology([CDC])

    assert any("min.insync.replicas is 1; expected at least 2" in p for p in _problems(report)["status"])


@pytest.mark.asyncio
async def test_the_offsets_topic_must_have_more_partitions_than_configs_and_status(cluster) -> None:
    cluster["admin"].cluster.update(_healthy(
        status=(25, 3, {"cleanup.policy": "compact", "min.insync.replicas": "2"}),
    ))  # offsets (25) no longer exceeds status (25)

    [report] = await verify_topology([CDC])

    assert any("more than the config and status topics" in p for p in _problems(report)["offsets"])


@pytest.mark.asyncio
async def test_extra_partitions_and_replicas_beyond_the_design_are_fine(cluster) -> None:
    cluster["admin"].cluster.update(_healthy(
        offsets=(50, 5, {"cleanup.policy": "compact", "min.insync.replicas": "3"}),
        status=(8, 4, {"cleanup.policy": "compact", "min.insync.replicas": "2"}),
    ))

    [report] = await verify_topology([CDC])

    assert report.ok


@pytest.mark.asyncio
async def test_a_one_broker_dev_cluster_can_lower_the_replication_expectations(cluster, monkeypatch) -> None:
    monkeypatch.setattr(settings, "connect_internal_replication_factor", 1)
    monkeypatch.setattr(settings, "connect_internal_min_insync_replicas", 1)
    cluster["admin"].cluster.update(_healthy(
        configs=(1, 1, {"cleanup.policy": "compact", "min.insync.replicas": "1"}),
        status=(5, 1, {"cleanup.policy": "compact", "min.insync.replicas": "1"}),
        offsets=(25, 1, {"cleanup.policy": "compact", "min.insync.replicas": "1"}),
    ))

    [report] = await verify_topology([CDC])

    assert report.ok


@pytest.mark.asyncio
async def test_a_pool_on_the_shared_cluster_is_reported_as_shared_and_not_checked(cluster) -> None:
    reports = {r.pool_id: r for r in await verify_topology()}

    assert reports["connect-jdbc-pool"].cluster_mode == "shared"
    assert reports["connect-jdbc-pool"].ok
    assert reports["connect-jdbc-pool"].topics == []
    assert "shared Connect cluster" in reports["connect-jdbc-pool"].note
    assert reports[CDC].cluster_mode == "dedicated"
    assert list(reports) == [
        "connect-cdc-pool", "connect-jdbc-pool", "connect-file-pool", "connect-api-pool", "connect-stream-pool", "connect-sink-pool",
    ]


@pytest.mark.asyncio
async def test_an_unreachable_kafka_is_an_error_not_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(settings, "connect_pools", {CDC: "http://connect-cdc:8083"})

    def _refuse(**kwargs):
        raise KafkaError("no brokers")

    monkeypatch.setattr(topology, "KafkaAdminClient", _refuse)

    [report] = await verify_topology([CDC])

    assert not report.ok
    assert "Kafka unreachable" in report.error


@pytest.mark.asyncio
async def test_an_unknown_pool_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown worker pool"):
        await verify_topology(["connect-bogus-pool"])


# ── provisioning ───────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provisioning_creates_each_missing_topic_with_the_designed_settings(cluster) -> None:
    cluster["admin"] = _Admin({})

    [result] = await provision_internal_topics([CDC])

    assert sorted(result.created) == sorted(internal_topic_names(CDC).values())
    created = {t.name: t for t in cluster["admin"].created}
    offsets = created["_inai_connect_cdc_offsets"]
    assert (offsets.num_partitions, offsets.replication_factor) == (25, 3)
    assert offsets.topic_configs == {"cleanup.policy": "compact", "min.insync.replicas": "2"}
    assert created["_inai_connect_cdc_configs"].num_partitions == 1
    assert created["_inai_connect_cdc_status"].num_partitions == 5
    assert [r.ok for r in await verify_topology([CDC])] == [True]  # what it created passes its own check


@pytest.mark.asyncio
async def test_provisioning_is_idempotent_and_never_alters_an_existing_topic(cluster) -> None:
    existing = cluster["admin"]
    existing.create_errors = {t: TopicAlreadyExistsError() for t in internal_topic_names(CDC).values()}
    before = dict(existing.cluster)

    [result] = await provision_internal_topics([CDC])

    assert result.created == []
    assert sorted(result.already_present) == sorted(internal_topic_names(CDC).values())
    assert existing.cluster == before


@pytest.mark.asyncio
async def test_provisioning_defaults_to_the_pools_that_have_their_own_cluster(cluster) -> None:
    cluster["admin"] = _Admin({})

    results = await provision_internal_topics()

    assert [r.pool_id for r in results] == [CDC]


@pytest.mark.asyncio
async def test_provisioning_refuses_an_unknown_pool() -> None:
    with pytest.raises(ValueError, match="Unknown worker pool"):
        await provision_internal_topics(["connect-bogus-pool"])


# ── the API ────────────────────────────────────────────────────────────────────────────────────────────────────────

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)


def test_the_topology_endpoint_reports_each_pool(cluster, fake_postgres) -> None:
    cluster["admin"].cluster.pop("_inai_connect_cdc_status")

    response = client.get("/api/v1/dataconnect/runtime/connect-pools/topology", headers={"X-Tenant-Id": "org-1"})

    assert response.status_code == 200
    pools = {p["pool_id"]: p for p in response.json()}
    assert len(pools) == 6
    assert pools[CDC]["ok"] is False
    assert pools[CDC]["topics"][1]["problems"] == ["topic does not exist"] or any(
        t["problems"] == ["topic does not exist"] for t in pools[CDC]["topics"]
    )
    assert pools["connect-sink-pool"]["cluster_mode"] == "shared"
