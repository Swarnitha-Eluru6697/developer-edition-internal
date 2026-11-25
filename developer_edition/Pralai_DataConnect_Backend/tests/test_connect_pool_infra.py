"""
The infrastructure definition of the Kafka Connect worker pools agrees with the runtime design (app/core/connect_pools.py):
every pool has a worker with its own group and its own internal topics, the topics are created first with the designed
settings, DataConnect is pointed at the right clusters, and each pool's image carries only the plugins its workload needs
— including the plugins its own connector templates use.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from app.core.connect_pools import POOLS, SINK_POOL_ID, worker_environment
from app.db.seed_templates import _seed_rows

INFRA = Path(__file__).resolve().parent.parent / "infra"
COMPOSE = yaml.safe_load((INFRA / "docker-compose.yml").read_text())
DOCKERFILE = (INFRA / "kafka-connect.Dockerfile").read_text()
SERVICES = COMPOSE["services"]
RF = "${CONNECT_INTERNAL_REPLICATION_FACTOR:-1}"


@pytest.mark.parametrize("pool", POOLS, ids=lambda p: p.short)
def test_each_pool_has_a_worker_in_the_pools_profile(pool) -> None:
    worker = SERVICES[f"connect-{pool.short}"]

    assert worker["profiles"] == ["pools"]  # not started by default: a plain `up` is unchanged
    assert worker["build"]["args"]["POOL"] == pool.short  # the image carries this pool's plugins only
    assert worker["image"] == f"inai-connect-{pool.short}:local"
    assert worker["depends_on"]["connect-topics-init"]["condition"] == "service_completed_successfully"
    assert "healthcheck" in worker


@pytest.mark.parametrize("pool", POOLS, ids=lambda p: p.short)
def test_a_workers_group_and_internal_topics_are_the_designs(pool) -> None:
    env = SERVICES[f"connect-{pool.short}"]["environment"]
    expected = worker_environment(pool.id, bootstrap="kafka:29092", advertised_host=f"connect-{pool.short}")

    for key, value in expected.items():
        if key.endswith("REPLICATION_FACTOR"):
            assert env[key] == RF, key  # one variable drives all three: 1 on a dev broker, 3 in production
        else:
            assert str(env[key]) == value, key


def test_the_workers_share_no_group_and_no_internal_topic() -> None:
    envs = [SERVICES[f"connect-{p.short}"]["environment"] for p in POOLS]

    for key in ("CONNECT_GROUP_ID", "CONNECT_CONFIG_STORAGE_TOPIC", "CONNECT_OFFSET_STORAGE_TOPIC", "CONNECT_STATUS_STORAGE_TOPIC"):
        assert len({e[key] for e in envs}) == len(POOLS), key


def test_the_offsets_topic_is_given_more_partitions_than_status_in_every_worker() -> None:
    for pool in POOLS:
        env = SERVICES[f"connect-{pool.short}"]["environment"]
        assert int(env["CONNECT_OFFSET_STORAGE_PARTITIONS"]) > int(env["CONNECT_STATUS_STORAGE_PARTITIONS"]) > 1


def test_the_topics_are_created_before_any_worker_starts_for_every_pool() -> None:
    init = SERVICES["connect-topics-init"]

    assert init["profiles"] == ["pools"]
    assert init["restart"] == "no"
    command = init["command"]
    assert command[:3] == ["python", "-m", "app.tools.provision_connect_topics"]
    assert [command[i + 1] for i, c in enumerate(command) if c == "--pool"] == [p.id for p in POOLS]
    assert init["environment"]["DATACONNECT_CONNECT_INTERNAL_REPLICATION_FACTOR"] == RF
    assert init["environment"]["DATACONNECT_CONNECT_INTERNAL_MIN_INSYNC_REPLICAS"] == "${CONNECT_INTERNAL_MIN_INSYNC_REPLICAS:-1}"


def test_the_backend_and_the_workers_agree_on_the_replication_settings() -> None:
    backend = SERVICES["backend"]["environment"]

    assert backend["DATACONNECT_CONNECT_INTERNAL_REPLICATION_FACTOR"] == RF
    assert backend["DATACONNECT_CONNECT_INTERNAL_MIN_INSYNC_REPLICAS"] == "${CONNECT_INTERNAL_MIN_INSYNC_REPLICAS:-1}"


def test_the_shared_dev_worker_is_untouched_and_still_the_default() -> None:
    assert SERVICES["backend"]["environment"]["DATACONNECT_CONNECT_POOLS"] == "${CONNECT_POOLS_JSON:-{}}"  # empty = shared
    assert SERVICES["backend"]["environment"]["DATACONNECT_KAFKA_CONNECT_URL"] == "http://kafka-connect:8083"
    assert "profiles" not in SERVICES["kafka-connect"]
    assert SERVICES["kafka-connect"]["environment"]["CONNECT_GROUP_ID"] == "dataconnect-workers"  # its running state is not renamed


def test_the_example_pool_map_points_each_pool_at_its_own_worker() -> None:
    text = (INFRA / "pools.env.example").read_text()
    pools = json.loads(re.search(r"^CONNECT_POOLS_JSON=(.+)$", text, re.MULTILINE).group(1))

    assert set(pools) == {p.id for p in POOLS}
    for pool in POOLS:
        service = f"connect-{pool.short}"
        port = SERVICES[service]["environment"]["CONNECT_REST_PORT"]
        assert pools[pool.id] == f"http://{service}:{port}"
        assert SERVICES[service]["environment"]["CONNECT_REST_ADVERTISED_HOST_NAME"] == service


# ── plugin isolation in the image ──────────────────────────────────────────────────────────────────────────────────

_CASE = re.compile(r'case "\$POOL" in ([a-z|]+)\) (.*?);;', re.DOTALL)


def _installs() -> dict[str, set[str]]:
    """artifact -> the pool builds (POOL values) that install it, read from the Dockerfile's own `case "$POOL"` lines."""
    out: dict[str, set[str]] = {}
    for pools, body in _CASE.findall(DOCKERFILE):
        hub = re.search(r"confluent-hub install --no-prompt ([\w-]+/[\w-]+)", body)
        if hub:
            artifact = hub.group(1).split("/")[1]
        elif "debezium" in body:
            artifact = "debezium"
        elif "iceberg" in body:
            artifact = "iceberg"
        elif "mysql-connector" in body:
            artifact = "mysql-driver"
        else:
            continue
        out.setdefault(artifact, set()).update(pools.split("|"))
    return out


def test_the_image_takes_a_pool_argument_that_defaults_to_everything() -> None:
    assert re.search(r"^ARG POOL=all$", DOCKERFILE, re.MULTILINE)


def test_only_known_builds_are_named_in_the_dockerfile() -> None:
    named = {p for pools, _ in _CASE.findall(DOCKERFILE) for p in pools.split("|")}

    assert named <= {"all", *(p.short for p in POOLS)}


def test_each_plugin_family_is_in_the_pools_that_run_it_and_not_in_others() -> None:
    installs = _installs()

    assert installs["debezium"] == {"all", "cdc"}  # CDC plugins never load in a sink or a poller
    assert installs["kafka-connect-jdbc"] == {"all", "jdbc", "sink"}
    assert installs["kafka-connect-s3"] == {"all", "sink"}
    assert installs["kafka-connect-s3-source"] == {"all", "file"}
    assert installs["kafka-connect-elasticsearch"] == {"all", "sink"}
    assert installs["kafka-connect-mongodb"] == {"all", "jdbc", "sink"}
    assert installs["iceberg"] == {"all", "sink"}


_PLUGIN_TO_ARTIFACT = {
    "kafka-connect-jdbc": "kafka-connect-jdbc", "kafka-connect-s3": "kafka-connect-s3",
    "kafka-connect-s3-source": "kafka-connect-s3-source", "kafka-connect-elasticsearch": "kafka-connect-elasticsearch",
    "mongo-kafka-connect": "kafka-connect-mongodb",
    "debezium-connector-postgresql": "debezium", "debezium-connector-mysql": "debezium",
    "debezium-connector-mongodb": "debezium", "debezium-connector-sqlserver": "debezium", "debezium-connector-oracle": "debezium",
}


def test_a_template_whose_plugin_the_image_installs_runs_in_a_pool_whose_image_has_it() -> None:
    """The pool a template names must be a pool whose worker image actually carries the plugin — otherwise a connector would
    be placed on a cluster that cannot run it."""
    installs = _installs()
    checked = 0
    for template in _seed_rows():
        artifact = _PLUGIN_TO_ARTIFACT.get(template["connector_plugin"])
        if artifact is None:
            continue
        pool_short = next(p.short for p in POOLS if p.id == template["runtime_pool_type"])
        assert pool_short in installs[artifact], f"{template['id']} runs in {pool_short} but that image lacks {artifact}"
        checked += 1
    assert checked >= 12


def test_the_local_file_plugin_is_in_the_file_and_sink_images_only() -> None:
    assert "connect-file" in {t["connector_plugin"] for t in _seed_rows() if t["runtime_pool_type"] == "connect-file-pool"}
    assert "connect-file" in {t["connector_plugin"] for t in _seed_rows() if t["runtime_pool_type"] == SINK_POOL_ID}
    assert re.search(r'case "\$POOL" in all\|file\|sink\) ;; \*\) rm -rf /usr/share/confluent-hub-components/connect-file', DOCKERFILE)
