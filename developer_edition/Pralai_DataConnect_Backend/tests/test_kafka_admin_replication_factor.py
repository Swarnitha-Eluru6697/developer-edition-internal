"""
GAP-06: a resolved TopicPolicy's replication_factor must be checked against
the live Kafka cluster's actual broker count BEFORE issuing CreateTopics —
see app/services/kafka_admin.py's _check_replication_factor.
"""

import pytest
from kafka.errors import KafkaError, TopicAlreadyExistsError

import app.services.kafka_admin as kafka_admin
from app.core.errors import TopicProvisioningError, TopicReplicationFactorError
from app.services.kafka_admin import ensure_topic
from app.services.topic_policy import platform_defaults


class _FakeAdminClient:
    """Mirrors tests/test_kafka_admin.py's own fake — duplicated rather than
    imported cross-file, matching this test suite's convention of each file
    owning its fixtures (there's no tests/__init__.py making this a package
    anyway)."""

    def __init__(self, create_error=None, broker_count=3, describe_cluster_error=None):
        self._create_error = create_error
        self._broker_count = broker_count
        self._describe_cluster_error = describe_cluster_error
        self.closed = False
        self.created_topics = None

    def create_topics(self, new_topics):
        self.created_topics = new_topics
        if self._create_error:
            raise self._create_error

    def describe_topics(self, names):
        # A topic "exists" for the tests that model one by having create_topics raise TopicAlreadyExistsError.
        exists = isinstance(self._create_error, TopicAlreadyExistsError)
        return [
            {"name": n, "error_code": 0 if exists else 3, "partitions": [{"partition_index": 0}] if exists else []}
            for n in names
        ]

    def describe_configs(self, resources):
        return {"topic": {r.name: {} for r in resources}}

    def describe_cluster(self):
        if self._describe_cluster_error:
            raise self._describe_cluster_error
        return {"brokers": [{"broker_id": i} for i in range(self._broker_count)]}

    def close(self):
        self.closed = True


def _install_fake_admin(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(kafka_admin, "KafkaAdminClient", lambda **kwargs: fake_client)


@pytest.mark.asyncio
async def test_blocks_when_replication_factor_exceeds_broker_count(monkeypatch) -> None:
    fake = _FakeAdminClient(broker_count=2)
    _install_fake_admin(monkeypatch, fake)
    resolved = platform_defaults() | {"replication_factor": 3}

    with pytest.raises(TopicReplicationFactorError) as exc_info:
        await ensure_topic("org.raw.sales.pg.orders.cdc.v1", resolved)

    assert "Replication factor 3 exceeds available brokers 2." in exc_info.value.message
    assert exc_info.value.status_code == 422
    assert exc_info.value.retryable is False
    assert exc_info.value.details == {
        "topic": "org.raw.sales.pg.orders.cdc.v1", "requested_replication_factor": 3, "broker_count": 2,
        "min_insync_replicas": 2, "problems": ["replication_exceeds_brokers"],
    }
    # Never even reached CreateTopics.
    assert fake.created_topics is None
    assert fake.closed


@pytest.mark.asyncio
async def test_allows_when_replication_factor_equals_broker_count(monkeypatch) -> None:
    fake = _FakeAdminClient(broker_count=3)
    _install_fake_admin(monkeypatch, fake)
    resolved = platform_defaults() | {"replication_factor": 3}

    result = await ensure_topic("orders", resolved)

    assert result.action == "created"
    assert fake.created_topics[0].replication_factor == 3


@pytest.mark.asyncio
async def test_allows_when_replication_factor_below_broker_count(monkeypatch) -> None:
    fake = _FakeAdminClient(broker_count=5)
    _install_fake_admin(monkeypatch, fake)
    resolved = platform_defaults() | {"replication_factor": 3}

    result = await ensure_topic("orders", resolved)

    assert result.action == "created"


@pytest.mark.asyncio
async def test_check_skipped_when_broker_count_cannot_be_determined(monkeypatch) -> None:
    """describe_cluster() reporting zero brokers is treated as "couldn't
    determine," not "there are no brokers" — must not block every deploy on
    an unexpected/empty response shape."""
    fake = _FakeAdminClient(broker_count=0)
    _install_fake_admin(monkeypatch, fake)
    resolved = platform_defaults() | {"replication_factor": 3}

    result = await ensure_topic("orders", resolved)

    assert result.action == "created"


@pytest.mark.asyncio
async def test_describe_cluster_failure_raises_topic_provisioning_error(monkeypatch) -> None:
    """A genuinely unreachable Admin API must still abort provisioning — the
    new preflight call is just as much a live Kafka call as create_topics
    itself, and gets the same treatment on failure."""
    fake = _FakeAdminClient(describe_cluster_error=KafkaError("broker unreachable"))
    _install_fake_admin(monkeypatch, fake)

    with pytest.raises(TopicProvisioningError):
        await ensure_topic("orders", platform_defaults())

    assert fake.created_topics is None


@pytest.mark.asyncio
async def test_replication_factor_error_is_distinct_from_generic_provisioning_error(monkeypatch) -> None:
    """A caller that only catches the broad TopicProvisioningError must not
    accidentally swallow this — it's a distinct, non-retryable config
    problem, not an infrastructure outage."""
    fake = _FakeAdminClient(broker_count=1)
    _install_fake_admin(monkeypatch, fake)

    with pytest.raises(TopicReplicationFactorError) as exc_info:
        await ensure_topic("orders", platform_defaults() | {"replication_factor": 3})

    assert not isinstance(exc_info.value, TopicProvisioningError)


@pytest.mark.asyncio
async def test_a_topic_that_already_exists_is_verified_not_blocked_by_the_brokers_check(monkeypatch) -> None:
    """GAP-06: only a topic about to be CREATED is checked against the cluster. An existing one is verified (its config
    diffed, never changed); what the policy says about its replication now can't be applied to it, so it must not block."""
    fake = _FakeAdminClient(create_error=TopicAlreadyExistsError("exists"), broker_count=2)
    _install_fake_admin(monkeypatch, fake)

    result = await ensure_topic("orders", platform_defaults() | {"replication_factor": 3})

    assert result.action in ("verified", "config_mismatch")  # reported, not refused
    assert fake.created_topics is None  # and nothing was created over it


@pytest.mark.asyncio
async def test_min_insync_replicas_above_the_replication_factor_is_refused_before_creating(monkeypatch) -> None:
    fake = _FakeAdminClient(broker_count=5)
    _install_fake_admin(monkeypatch, fake)

    with pytest.raises(TopicReplicationFactorError, match="min.insync.replicas 3 exceeds the replication factor 2") as exc_info:
        await ensure_topic("orders", platform_defaults() | {"replication_factor": 2, "min_insync_replicas": 3})

    assert exc_info.value.details["problems"] == ["min_isr_exceeds_replication"]
    assert fake.created_topics is None


@pytest.mark.asyncio
async def test_min_insync_replicas_above_the_broker_count_is_refused_before_creating(monkeypatch) -> None:
    fake = _FakeAdminClient(broker_count=2)
    _install_fake_admin(monkeypatch, fake)

    with pytest.raises(TopicReplicationFactorError, match="min.insync.replicas 3 exceeds the replication factor 2|exceeds available brokers"):
        await ensure_topic("orders", platform_defaults() | {"replication_factor": 3, "min_insync_replicas": 3})

    assert fake.created_topics is None
