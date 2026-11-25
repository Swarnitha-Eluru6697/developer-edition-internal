"""
GAP-06: a topic policy is checked against the Kafka cluster's REAL topology before anything asks the cluster to create the topic —
whether or not explicit topic provisioning is on — at readiness, deploy, in the reconciler, and when a policy is saved.

    Kafka has 2 brokers; the policy asks for replication factor 3  ->  "Replication factor 3 exceeds available brokers 2."
"""

import pytest
from kafka.errors import KafkaError

import app.services.kafka_cluster as kafka_cluster
import app.services.runtime_reconciler as reconciler_module
from app.core.config import settings
from app.core.errors import TopicReplicationFactorError, ValidationError
from app.schemas.pipeline import PipelineCreate
from app.schemas.topic import TopicPolicyCreate
from app.services import pipeline_service as pipeline_service_module
from app.services.kafka_cluster import (
    PolicyProblem,
    broker_count,
    invalidate_broker_count_cache,
    topic_policy_problems,
)
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient
from app.services.topic_policy import topic_policy_service
from app.services.topic_preflight import assert_topic_policy_feasible, evaluate_topic_policy

# ── a fake Kafka cluster ───────────────────────────────────────────────────────────────────────────────────────────


class _Cluster:
    def __init__(self, brokers: int | None = 2, topics: set[str] | None = None, error: Exception | None = None) -> None:
        self.brokers = brokers
        self.topics = topics or set()
        self.error = error
        self.constructed = 0
        self.describe_calls = 0

    def admin(self, **kwargs):
        self.constructed += 1
        if self.error is not None and self.brokers == -1:
            raise self.error
        return self

    def describe_cluster(self):
        self.describe_calls += 1
        if self.error is not None:
            raise self.error
        return {"brokers": [{"node_id": i} for i in range(self.brokers or 0)]}

    def describe_topics(self, names):
        return [
            {"name": n, "error_code": 0 if n in self.topics else 3, "partitions": [{"partition_index": 0}] if n in self.topics else []}
            for n in names
        ]

    def close(self):
        pass


@pytest.fixture
def kafka(monkeypatch):
    """A cluster with 2 brokers and the feasibility check switched on."""
    cluster = _Cluster(brokers=2)
    monkeypatch.setattr(settings, "topic_feasibility_check_enabled", True)
    monkeypatch.setattr(settings, "topic_default_replication_factor", 3)
    monkeypatch.setattr(settings, "topic_default_min_insync_replicas", 2)
    monkeypatch.setattr(kafka_cluster, "KafkaAdminClient", cluster.admin)
    invalidate_broker_count_cache()
    yield cluster
    invalidate_broker_count_cache()


# ── the pure rules ─────────────────────────────────────────────────────────────────────────────────────────────────


def _codes(problems: list[PolicyProblem]) -> list[str]:
    return [p.code for p in problems]


def test_a_replication_factor_above_the_broker_count_is_the_ticket_message() -> None:
    problems = topic_policy_problems({"replication_factor": 3, "min_insync_replicas": 2}, 2)

    assert [p.message for p in problems] == ["Replication factor 3 exceeds available brokers 2."]


@pytest.mark.parametrize(
    ("resolved", "brokers", "expected"),
    [
        ({"replication_factor": 3, "min_insync_replicas": 2}, 3, []),  # the defaults on a 3-broker cluster
        ({"replication_factor": 3, "min_insync_replicas": 2}, 5, []),
        ({"replication_factor": 1, "min_insync_replicas": 1}, 1, []),  # a single-broker dev cluster
        ({"replication_factor": 3, "min_insync_replicas": 2}, 2, ["replication_exceeds_brokers"]),
        ({"replication_factor": 2, "min_insync_replicas": 3}, 5, ["min_isr_exceeds_replication"]),
        ({"replication_factor": 3, "min_insync_replicas": 3}, 2, ["replication_exceeds_brokers", "min_isr_exceeds_brokers"]),
        ({"replication_factor": 2, "min_insync_replicas": 2}, 1, ["replication_exceeds_brokers", "min_isr_exceeds_brokers"]),
    ],
)
def test_each_way_a_policy_can_be_unsatisfiable(resolved, brokers, expected) -> None:
    got = _codes(topic_policy_problems(resolved, brokers))

    # (a min-ISR above BOTH the replication factor and the brokers is reported once, as exceeding the replication factor)
    assert got == expected or got == [c for c in expected if c != "min_isr_exceeds_brokers"] + ["min_isr_exceeds_replication"]


def test_an_unknown_broker_count_runs_only_the_checks_that_need_no_cluster() -> None:
    assert topic_policy_problems({"replication_factor": 3, "min_insync_replicas": 2}, None) == []
    assert _codes(topic_policy_problems({"replication_factor": 2, "min_insync_replicas": 3}, None)) == ["min_isr_exceeds_replication"]


# ── the cached broker count ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_broker_count_is_asked_once_and_cached(kafka) -> None:
    assert await broker_count() == 2
    assert await broker_count() == 2

    assert kafka.constructed == 1


@pytest.mark.asyncio
async def test_a_cached_count_expires(kafka, monkeypatch) -> None:
    monkeypatch.setattr(settings, "kafka_broker_count_cache_seconds", 0.0)

    await broker_count()
    await broker_count()

    assert kafka.constructed == 2


@pytest.mark.asyncio
async def test_an_unreachable_cluster_is_unknown_and_is_asked_again_next_time(kafka) -> None:
    kafka.brokers, kafka.error = -1, KafkaError("no brokers")  # constructing the client fails

    assert await broker_count() is None
    assert await broker_count() is None
    assert kafka.constructed == 2  # a failure is not cached

    kafka.error, kafka.brokers = None, 4
    assert await broker_count() == 4


@pytest.mark.asyncio
async def test_a_cluster_answering_with_no_brokers_is_unknown_not_zero(kafka) -> None:
    kafka.brokers = 0

    assert await broker_count() is None


@pytest.mark.asyncio
async def test_a_failing_describe_is_unknown(kafka) -> None:
    kafka.error = KafkaError("timeout")

    assert await broker_count() is None


# ── the pipeline preflight ─────────────────────────────────────────────────────────────────────────────────────────

SOURCE = {"id": "p1", "org_id": "org_a", "direction": "SOURCE", "domain": "sales", "topic_name": "raw-sales-pg-orders-cdc-v1"}


@pytest.mark.asyncio
async def test_a_source_whose_topic_policy_needs_more_brokers_than_exist_is_blocked(kafka, fake_postgres) -> None:
    with pytest.raises(TopicReplicationFactorError) as exc_info:
        await assert_topic_policy_feasible("org_a", SOURCE, None)  # platform defaults: 3 replicas

    error = exc_info.value
    assert error.message == "Cannot create topic 'raw-sales-pg-orders-cdc-v1': Replication factor 3 exceeds available brokers 2."
    assert (error.status_code, error.retryable) == (422, False)
    assert error.details == {
        "topic": SOURCE["topic_name"], "requested_replication_factor": 3, "min_insync_replicas": 2, "broker_count": 2,
        "problems": ["replication_exceeds_brokers"],
    }


@pytest.mark.asyncio
async def test_a_policy_the_cluster_can_satisfy_passes(kafka, fake_postgres) -> None:
    await assert_topic_policy_feasible("org_a", SOURCE, {"replication_factor": 2, "min_insync_replicas": 1})


@pytest.mark.asyncio
async def test_a_topic_that_already_exists_is_never_blocked(kafka, fake_postgres) -> None:
    kafka.topics = {SOURCE["topic_name"]}  # it exists, with whatever replication it was created with

    result = await evaluate_topic_policy("org_a", SOURCE, None)

    assert result.ok
    assert result.topic_exists is True


@pytest.mark.asyncio
async def test_a_sink_creates_no_topic_so_nothing_is_checked(kafka, fake_postgres) -> None:
    sink = {**SOURCE, "direction": "SINK", "topic_name": None}

    result = await evaluate_topic_policy("org_a", sink, None)

    assert result.applicable is False
    assert kafka.constructed == 0


@pytest.mark.asyncio
async def test_the_check_can_be_switched_off(kafka, fake_postgres, monkeypatch) -> None:
    monkeypatch.setattr(settings, "topic_feasibility_check_enabled", False)

    await assert_topic_policy_feasible("org_a", SOURCE, None)

    assert kafka.constructed == 0


@pytest.mark.asyncio
async def test_a_cluster_that_cannot_be_asked_never_blocks_on_brokers_but_still_catches_what_needs_no_cluster(kafka, fake_postgres) -> None:
    kafka.brokers, kafka.error = -1, KafkaError("down")

    await assert_topic_policy_feasible("org_a", SOURCE, None)  # 3 replicas: can't say it is wrong, so it is not blocked
    with pytest.raises(TopicReplicationFactorError, match="exceeds the replication factor"):
        await assert_topic_policy_feasible("org_a", SOURCE, {"replication_factor": 1, "min_insync_replicas": 2})


# ── readiness ──────────────────────────────────────────────────────────────────────────────────────────────────────


def _seed(fake_postgres, org_id: str, connector_type: str = "source") -> tuple[dict, dict]:
    klass = "io.confluent.connect.jdbc.JdbcSinkConnector" if connector_type == "sink" else "io.confluent.connect.jdbc.JdbcSourceConnector"
    template = fake_postgres.create("connector_templates", {"connector_class": klass, "connector_type": connector_type, "default_config": {}})
    connection = fake_postgres.create("connection_profiles", {"org_id": org_id, "name": "c", "connection_config": {}})
    return template, connection


async def _source(org_id: str, template: dict, connection: dict, **overrides) -> dict:
    entity = overrides.pop("entity", "orders")
    return await pipeline_service.create_pipeline(org_id, PipelineCreate(
        name="S", template_id=template["id"], connection_profile_id=connection["id"], zone="raw", domain="sales",
        source_system="pg", entity=entity, mode="batch", version="v1", **overrides,
    ))


def _check(readiness: dict, key: str) -> dict:
    return next(c for c in readiness["checks"] if c["key"] == key)


@pytest.mark.asyncio
async def test_readiness_fails_with_the_broker_count_and_the_requested_replication(kafka, fake_postgres) -> None:
    template, connection = _seed(fake_postgres, "org_a")
    created = await _source("org_a", template, connection)

    readiness = await pipeline_service.check_deploy_readiness("org_a", created["id"])

    check = _check(readiness, "kafka_brokers_sufficient")
    assert check["ok"] is False
    assert "Kafka has 2 broker(s)" in check["reason"]
    assert "replication factor 3" in check["reason"]
    assert "Replication factor 3 exceeds available brokers 2." in check["reason"]
    assert readiness["ready"] is False


@pytest.mark.asyncio
async def test_readiness_passes_when_the_cluster_has_enough_brokers(kafka, fake_postgres) -> None:
    kafka.brokers = 3
    template, connection = _seed(fake_postgres, "org_a")
    created = await _source("org_a", template, connection)

    assert _check(await pipeline_service.check_deploy_readiness("org_a", created["id"]), "kafka_brokers_sufficient")["ok"]


@pytest.mark.asyncio
async def test_readiness_passes_for_a_policy_sized_for_the_cluster(kafka, fake_postgres) -> None:
    policy = fake_postgres.create("topic_policies", {"org_id": "org_a", "name": "small", "replication_factor": 2, "min_insync_replicas": 1})
    template, connection = _seed(fake_postgres, "org_a")
    created = await _source("org_a", template, connection, topic_policy_id=policy["id"])

    assert _check(await pipeline_service.check_deploy_readiness("org_a", created["id"]), "kafka_brokers_sufficient")["ok"]


@pytest.mark.asyncio
async def test_readiness_does_not_fail_for_a_sink_or_an_unreachable_cluster(kafka, fake_postgres) -> None:
    sink_template, connection = _seed(fake_postgres, "org_a", "sink")
    sink = await pipeline_service.create_pipeline("org_a", PipelineCreate(
        name="K", template_id=sink_template["id"], connection_profile_id=connection["id"], zone="prepared", domain="sales",
        source_system="pg", entity="orders_copy", mode="batch", version="v1", connector_config={"topics": "raw-sales-pg-orders-cdc-v1"},
    ))
    assert _check(await pipeline_service.check_deploy_readiness("org_a", sink["id"]), "kafka_brokers_sufficient")["ok"]

    kafka.brokers, kafka.error = -1, KafkaError("down")
    invalidate_broker_count_cache()
    template, _ = _seed(fake_postgres, "org_a")
    source = await _source("org_a", template, connection, entity="other")
    assert _check(await pipeline_service.check_deploy_readiness("org_a", source["id"]), "kafka_brokers_sufficient")["ok"]


# ── deploy (with explicit provisioning OFF, the default) ───────────────────────────────────────────────────────────


async def _noop(*args, **kwargs):
    return None


async def _plugins(self):
    return [{"class": "io.confluent.connect.jdbc.JdbcSourceConnector", "type": "source", "version": "10.9.7"}]


@pytest.fixture
def deploy_env(monkeypatch):
    calls: list[str] = []

    async def _deploy(self, name, config):
        calls.append("deploy_connector")
        return {"config": config}

    async def _record(*args, **kwargs):
        calls.append("record_desired")

    async def _worker(self):
        return {"version": "7.6.0"}

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr(settings, "topic_admin_provisioning_enabled", False)
    monkeypatch.setattr(settings, "deploy_requires_readiness", False)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _plugins)
    monkeypatch.setattr(KafkaConnectClient, "get_worker_info", _worker)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _deploy)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _record)
    monkeypatch.setattr("app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop)
    return calls


@pytest.mark.asyncio
async def test_deploy_is_refused_before_anything_is_sent_when_the_cluster_cannot_create_the_topic(kafka, fake_postgres, deploy_env) -> None:
    template, connection = _seed(fake_postgres, "org_a")
    created = await _source("org_a", template, connection)

    with pytest.raises(TopicReplicationFactorError, match="Replication factor 3 exceeds available brokers 2."):
        await pipeline_service.deploy("org_a", created["id"])

    assert deploy_env == []  # no desired state recorded, nothing sent to Kafka Connect
    assert (await pipeline_service.get_or_404("org_a", created["id"]))["status"] == "draft"


@pytest.mark.asyncio
async def test_deploy_proceeds_when_the_cluster_can_create_the_topic(kafka, fake_postgres, deploy_env) -> None:
    kafka.brokers = 3
    template, connection = _seed(fake_postgres, "org_a")
    created = await _source("org_a", template, connection)

    await pipeline_service.deploy("org_a", created["id"])

    assert "deploy_connector" in deploy_env


@pytest.mark.asyncio
async def test_deploy_of_a_pipeline_whose_topic_already_exists_is_not_blocked(kafka, fake_postgres, deploy_env) -> None:
    template, connection = _seed(fake_postgres, "org_a")
    created = await _source("org_a", template, connection)
    kafka.topics = {created["topic_name"]}

    await pipeline_service.deploy("org_a", created["id"])

    assert "deploy_connector" in deploy_env


# ── the reconciler ─────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reconciler_does_not_create_a_connector_whose_topic_the_cluster_cannot_create(kafka, fake_postgres, monkeypatch) -> None:
    from tests.test_runtime_reconciler_convergence import (
        _FakeRepo,
        _FakeSession,
        _install_fakes,
        _row,
    )

    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", False)
    calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])

    async def _get_pipeline(org_id, pipeline_id):
        return {**SOURCE, "id": "pipe-1", "status": "running", "topic_policy_id": None, "error_handling_policy": "SKIP_AND_LOG"}

    async def _resolve(pipeline):
        return {"connector.class": "x", "topic.creation.enable": "true"}, "hash-new"

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _get_pipeline)
    monkeypatch.setattr(reconciler_module, "_resolve_config", _resolve)
    row = _row(desired_state="running", desired_generation=1, config_hash="hash-old")

    await reconciler_module.RuntimeReconciler(worker_id="w")._reconcile_row(
        _session := _FakeSession(1), _FakeRepo(_session), client=None, existing=set(), row=row,
    )

    assert calls["deploy"] == []  # nothing sent to Connect
    assert "Replication factor 3 exceeds available brokers 2." in row.last_runtime_error
    assert row.config_hash == "hash-old"  # not recorded as applied, so the next tick tries again


@pytest.mark.asyncio
async def test_the_reconciler_creates_the_connector_once_the_cluster_has_the_brokers(kafka, fake_postgres, monkeypatch) -> None:
    from tests.test_runtime_reconciler_convergence import (
        _FakeRepo,
        _FakeSession,
        _install_fakes,
        _row,
    )

    kafka.brokers = 3
    monkeypatch.setattr(reconciler_module.settings, "reconciler_preflight_enabled", False)
    calls, _ = _install_fakes(monkeypatch, status_sequence=["RUNNING"])

    async def _get_pipeline(org_id, pipeline_id):
        return {**SOURCE, "id": "pipe-1", "status": "running", "topic_policy_id": None, "error_handling_policy": "SKIP_AND_LOG"}

    async def _resolve(pipeline):
        return {"connector.class": "x", "topic.creation.enable": "true"}, "hash-new"

    monkeypatch.setattr(pipeline_service_module.pipeline_service, "get_pipeline", _get_pipeline)
    monkeypatch.setattr(reconciler_module, "_resolve_config", _resolve)
    row = _row(desired_state="running", desired_generation=1, config_hash="hash-old")

    await reconciler_module.RuntimeReconciler(worker_id="w")._reconcile_row(
        _session := _FakeSession(1), _FakeRepo(_session), client=None, existing=set(), row=row,
    )

    assert calls["deploy"] == ["conn-1"]


# ── saving a topic policy ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_policy_asking_for_more_replicas_than_brokers_is_refused_when_saved(kafka, fake_postgres) -> None:
    with pytest.raises(ValidationError) as exc_info:
        await topic_policy_service.create_policy("org_a", {"name": "big", "replication_factor": 3})

    assert "Replication factor 3 exceeds available brokers 2." in exc_info.value.message
    assert "This Kafka cluster has 2 broker(s)." in exc_info.value.message
    assert exc_info.value.field_errors[0].field == "replication_factor"
    assert fake_postgres.topic_policies == {}


@pytest.mark.asyncio
async def test_a_min_isr_above_the_brokers_is_refused_when_saved(kafka, fake_postgres) -> None:
    with pytest.raises(ValidationError) as exc_info:
        await topic_policy_service.create_policy("org_a", {"name": "strict", "min_insync_replicas": 3})

    assert exc_info.value.field_errors[0].field == "min_insync_replicas"


@pytest.mark.asyncio
async def test_a_policy_the_cluster_can_satisfy_is_saved_and_an_update_is_judged_the_same_way(kafka, fake_postgres) -> None:
    saved = await topic_policy_service.create_policy("org_a", {"name": "ok", "replication_factor": 2, "min_insync_replicas": 1})

    with pytest.raises(ValidationError):
        await topic_policy_service.update_policy("org_a", saved["id"], {"name": "ok", "replication_factor": 4})


@pytest.mark.asyncio
async def test_saving_never_blocks_on_a_cluster_that_cannot_be_asked_or_a_disabled_check(kafka, fake_postgres, monkeypatch) -> None:
    kafka.brokers, kafka.error = -1, KafkaError("down")
    await topic_policy_service.create_policy("org_a", {"name": "unknown cluster", "replication_factor": 3})

    kafka.error, kafka.brokers = None, 2
    invalidate_broker_count_cache()
    monkeypatch.setattr(settings, "topic_feasibility_check_enabled", False)
    await topic_policy_service.create_policy("org_a", {"name": "check off", "replication_factor": 3})

    assert len(fake_postgres.topic_policies) == 2


@pytest.mark.asyncio
async def test_a_policy_that_sets_neither_field_is_not_judged_here(kafka, fake_postgres) -> None:
    await topic_policy_service.create_policy("org_a", {"name": "retention only", "retention_ms": 1000})

    assert kafka.constructed == 0  # the platform defaults are checked at readiness/deploy, where the pipeline is known


def test_a_policy_whose_min_isr_exceeds_its_own_replication_factor_is_a_validation_error() -> None:
    with pytest.raises(ValueError, match=r"min_insync_replicas \(3\) cannot exceed replication_factor \(2\)"):
        TopicPolicyCreate(name="x", replication_factor=2, min_insync_replicas=3)

    assert TopicPolicyCreate(name="x", replication_factor=3, min_insync_replicas=3).min_insync_replicas == 3
    assert TopicPolicyCreate(name="x", min_insync_replicas=5).replication_factor is None  # one set alone: judged later


def test_platform_defaults_whose_min_isr_exceeds_their_replication_factor_stop_the_app_starting(monkeypatch) -> None:
    from app.startup import validate_configuration

    monkeypatch.setattr(settings, "topic_default_replication_factor", 1)
    monkeypatch.setattr(settings, "topic_default_min_insync_replicas", 2)

    with pytest.raises(RuntimeError, match="exceeds DATACONNECT_TOPIC_DEFAULT_REPLICATION_FACTOR"):
        validate_configuration()


def test_the_admin_client_options_are_ones_the_kafka_client_accepts() -> None:
    """An unrecognised option makes KafkaAdminClient raise KafkaConfigurationError — a KafkaError, which the broker-count lookup
    would swallow as "Kafka unreachable", silently disabling the whole check. Found against a live broker, where every call
    returned "unknown"; this keeps the options honest."""
    from kafka.admin import KafkaAdminClient

    unknown = set(kafka_cluster.ADMIN_CLIENT_OPTIONS) - set(KafkaAdminClient.DEFAULT_CONFIG)

    assert unknown == set()


# ── a client library quirk found against a live broker ─────────────────────────────────────────────────────────────


class _Quirky:
    """describe_cluster() blows up the way kafka-python 3.0 does against a real 3.6 broker; the brokers are in the client's cluster metadata."""

    def __init__(self, holder: str | None = "_manager", brokers: int = 1, cluster_error: bool = False) -> None:
        class _ClusterMetadata:
            def brokers(inner):
                if cluster_error:
                    raise RuntimeError("no metadata")
                return [object() for _ in range(brokers)]

        if holder:
            setattr(self, holder, type("Holder", (), {"cluster": _ClusterMetadata()})())

    def describe_cluster(self):
        raise KeyError("endpoint_type")


def test_a_describe_cluster_client_quirk_falls_back_to_the_clients_own_broker_list() -> None:
    from app.services.kafka_cluster import brokers_of

    assert brokers_of(_Quirky("_manager", brokers=3)) == 3
    assert brokers_of(_Quirky("_client", brokers=2)) == 2


def test_when_neither_source_can_say_the_count_is_unknown_not_zero_brokers_and_not_a_crash() -> None:
    from app.services.kafka_cluster import brokers_of

    assert brokers_of(_Quirky(None)) == 0
    assert brokers_of(_Quirky("_manager", cluster_error=True)) == 0


def test_a_genuine_kafka_error_is_not_swallowed_as_a_quirk() -> None:
    from app.services.kafka_cluster import brokers_of

    class _Down:
        def describe_cluster(self):
            raise KafkaError("unreachable")

    with pytest.raises(KafkaError):
        brokers_of(_Down())


@pytest.mark.asyncio
async def test_the_broker_count_works_through_the_quirk(kafka, monkeypatch) -> None:
    quirky = _Quirky("_manager", brokers=4)
    quirky.close = lambda: None
    monkeypatch.setattr(kafka_cluster, "KafkaAdminClient", lambda **kwargs: quirky)

    assert await broker_count() == 4
