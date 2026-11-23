import pytest
from kafka.errors import KafkaError, TopicAlreadyExistsError

import app.services.kafka_admin as kafka_admin
from app.services.kafka_admin import (
    TopicProvisioningError,
    TopicProvisionResult,
    ensure_topic,
)
from app.services.topic_policy import platform_defaults


class _FakeAdminClient:
    def __init__(self, create_error=None, describe_response=None, broker_count=3, describe_cluster_error=None):
        self._create_error = create_error
        self._describe_response = describe_response or {}
        # Matches platform_defaults()'s replication_factor (3) exactly, so
        # every existing test that doesn't care about GAP-06 keeps passing
        # unmodified — see test_kafka_admin_replication_factor.py for the
        # dedicated broker-count-vs-replication-factor tests.
        self._broker_count = broker_count
        self._describe_cluster_error = describe_cluster_error
        self.closed = False
        self.created_topics = None

    def create_topics(self, new_topics):
        self.created_topics = new_topics
        if self._create_error:
            raise self._create_error

    def describe_configs(self, resources):
        return self._describe_response

    def describe_topics(self, names):
        # A topic "exists" for the tests that model one by having create_topics raise TopicAlreadyExistsError.
        exists = isinstance(self._create_error, TopicAlreadyExistsError)
        return [
            {"name": n, "error_code": 0 if exists else 3, "partitions": [{"partition_index": 0}] if exists else []}
            for n in names
        ]

    def describe_cluster(self):
        if self._describe_cluster_error:
            raise self._describe_cluster_error
        return {"brokers": [{"broker_id": i} for i in range(self._broker_count)]}

    def close(self):
        self.closed = True


def _install_fake_admin(monkeypatch, fake_client) -> None:
    monkeypatch.setattr(kafka_admin, "KafkaAdminClient", lambda **kwargs: fake_client)


@pytest.mark.asyncio
async def test_ensure_topic_creates_when_topic_absent(monkeypatch) -> None:
    fake = _FakeAdminClient()
    _install_fake_admin(monkeypatch, fake)
    resolved = platform_defaults()

    result = await ensure_topic("org.raw.sales.pg.orders.cdc.v1", resolved)

    assert result.action == "created"
    assert result.resolved_config == resolved
    assert fake.created_topics[0].name == "org.raw.sales.pg.orders.cdc.v1"
    assert fake.created_topics[0].num_partitions == resolved["partitions"]
    assert fake.created_topics[0].replication_factor == resolved["replication_factor"]
    assert fake.closed


@pytest.mark.asyncio
async def test_ensure_topic_verifies_matching_existing_topic(monkeypatch) -> None:
    resolved = platform_defaults()
    matching_describe = {
        "topic": {
            "orders": {
                "retention.ms": {"value": str(resolved["retention_ms"])},
                "cleanup.policy": {"value": resolved["cleanup_policy"]},
                "min.insync.replicas": {"value": str(resolved["min_insync_replicas"])},
                "compression.type": {"value": resolved["compression_type"]},
                "max.message.bytes": {"value": str(resolved["max_message_bytes"])},
            }
        }
    }
    fake = _FakeAdminClient(create_error=TopicAlreadyExistsError("exists"), describe_response=matching_describe)
    _install_fake_admin(monkeypatch, fake)

    result = await ensure_topic("orders", resolved)

    assert result.action == "verified"
    assert result.mismatches == {}


@pytest.mark.asyncio
async def test_ensure_topic_reports_config_mismatch(monkeypatch) -> None:
    resolved = platform_defaults()
    mismatched_describe = {
        "topic": {
            "orders": {
                "retention.ms": {"value": "1"},  # doesn't match resolved["retention_ms"]
                "cleanup.policy": {"value": resolved["cleanup_policy"]},
                "min.insync.replicas": {"value": str(resolved["min_insync_replicas"])},
                "compression.type": {"value": resolved["compression_type"]},
                "max.message.bytes": {"value": str(resolved["max_message_bytes"])},
            }
        }
    }
    fake = _FakeAdminClient(create_error=TopicAlreadyExistsError("exists"), describe_response=mismatched_describe)
    _install_fake_admin(monkeypatch, fake)

    result = await ensure_topic("orders", resolved)

    assert result.action == "config_mismatch"
    assert "retention.ms" in result.mismatches
    assert result.mismatches["retention.ms"] == (str(resolved["retention_ms"]), "1")


@pytest.mark.asyncio
async def test_ensure_topic_raises_on_broker_failure(monkeypatch) -> None:
    """A genuine Admin API failure must abort provisioning, never fall
    through to the broker's implicit auto-create."""
    fake = _FakeAdminClient(create_error=KafkaError("broker unreachable"))
    _install_fake_admin(monkeypatch, fake)

    with pytest.raises(TopicProvisioningError):
        await ensure_topic("orders", platform_defaults())


@pytest.mark.asyncio
async def test_provision_topic_and_record_audit_writes_expected_fields(monkeypatch, fake_postgres) -> None:
    """The shared entry point deploy() and the reconciler both call: it must
    resolve the pipeline's topic policy, call ensure_topic with it, and
    record exactly that resolved policy (and the outcome) to the audit
    trail — this is the ticket's "record the intended topic policy"."""
    canned_result = TopicProvisionResult(
        topic="org.raw.sales.pg.orders.cdc.v1",
        action="created",
        resolved_config=platform_defaults(),
    )

    async def _fake_ensure_topic(topic, resolved_config):
        assert topic == "org.raw.sales.pg.orders.cdc.v1"
        return canned_result

    monkeypatch.setattr(kafka_admin, "ensure_topic", _fake_ensure_topic)

    created_records = []

    class _FakeAuditLogRepository:
        def __init__(self, session):
            self.session = session

        async def create(self, values):
            created_records.append(values)
            return values

    class _FakeSession:
        async def commit(self):
            pass

    class _FakeSessionContext:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", lambda: _FakeSessionContext())
    monkeypatch.setattr("app.repositories.dataconnect.AuditLogRepository", _FakeAuditLogRepository)

    pipeline = {"id": "pipe-1", "domain": "sales"}
    result = await kafka_admin.provision_topic_and_record_audit(
        "org_1", pipeline, "org.raw.sales.pg.orders.cdc.v1", None
    )

    assert result is canned_result
    assert len(created_records) == 1
    record = created_records[0]
    assert record["org_id"] == "org_1"
    assert record["resource_type"] == "topic"
    assert record["resource_id"] == "org.raw.sales.pg.orders.cdc.v1"
    assert record["action"] == "topic_policy_created"
    assert record["after_state"]["resolved_policy"] == canned_result.resolved_config
