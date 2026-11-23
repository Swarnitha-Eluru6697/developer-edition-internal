"""NEW-09 — connector availability: which catalog entries can actually run, as supported / requires_configuration / unavailable with a stable code.

The catalog lists 128 connectors; most have no plugin installed on a typical Kafka Connect worker. Availability is decided from the same checks the
deploy preflight makes, so the catalog never offers a connector a deploy would reject for its plugin or version, and never hides one it would accept.
"""
import asyncio
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.core.capability import CapabilityState
from app.main import app
from app.services import connector_availability as ca
from app.services.deploy_preflight import collect_preflight_failures

JDBC_SINK = "io.confluent.connect.jdbc.JdbcSinkConnector"
JDBC_SOURCE = "io.confluent.connect.jdbc.JdbcSourceConnector"
INSTALLED = [
    {"class": JDBC_SINK, "type": "sink", "version": "10.7.4"},
    {"class": JDBC_SOURCE, "type": "source", "version": "10.7.4"},
]
WORKER = {"version": "7.6.0-ccs"}


def template(**over) -> dict:
    return {"id": "t1", "name": "T", "connector_type": "sink", "connector_class": JDBC_SINK, "runtime_pool_type": "connect-sink-pool",
            "is_active": True, "connector_version": "10.7.4", "supported_kafka_connect_versions": [], **over}


def snapshot(plugins=INSTALLED, worker=WORKER) -> ca.RuntimeSnapshot:
    return ca.RuntimeSnapshot(plugins, worker)


# ── One template, given what its cluster reported ──────────────────────────────────────────────────────────────────────

def test_a_template_whose_plugin_is_installed_is_supported() -> None:
    result = ca.template_availability(template(), snapshot())

    assert result.state is CapabilityState.SUPPORTED
    assert (result.code, result.message, result.verified) == (None, None, True)


@pytest.mark.parametrize(("over", "plugins", "code"), [
    ({}, [], "PLUGIN_NOT_INSTALLED"),
    ({"connector_class": "com.example.NotThere"}, INSTALLED, "PLUGIN_NOT_INSTALLED"),
    ({}, [{"class": JDBC_SINK, "type": "source", "version": "10.7.4"}], "PLUGIN_WRONG_KIND"),                  # installed, but as a source
    ({"connector_version": "10.7.4"}, [{"class": JDBC_SINK, "type": "sink", "version": "9.0.0"}], "PLUGIN_VERSION_INCOMPATIBLE"),
    ({"connector_version": "10.7.4"}, [{"class": JDBC_SINK, "type": "sink", "version": "10.2.0"}], "PLUGIN_VERSION_INCOMPATIBLE"),
])
def test_a_missing_or_unsuitable_plugin_makes_the_template_unavailable_with_the_reason(over, plugins, code) -> None:
    result = ca.template_availability(template(**over), snapshot(plugins))

    assert result.state is CapabilityState.UNAVAILABLE
    assert result.code == code
    assert result.message
    assert result.remediation


def test_a_newer_minor_version_of_the_plugin_is_still_supported() -> None:
    assert ca.template_availability(template(connector_version="10.7.4"), snapshot([{"class": JDBC_SINK, "type": "sink", "version": "10.9.1"}])).state \
        is CapabilityState.SUPPORTED


def test_a_worker_version_the_template_was_not_qualified_for_is_unavailable() -> None:
    result = ca.template_availability(template(supported_kafka_connect_versions=["3.5"]), snapshot(worker={"version": "7.6.0-ccs"}))

    assert (result.state, result.code) == (CapabilityState.UNAVAILABLE, "WORKER_VERSION_UNSUPPORTED")


def test_a_template_that_pins_no_worker_version_is_not_held_to_one() -> None:
    assert ca.template_availability(template(supported_kafka_connect_versions=[]), snapshot(worker={"version": "anything"})).state \
        is CapabilityState.SUPPORTED


def test_an_inactive_template_is_unavailable_whatever_is_installed() -> None:
    result = ca.template_availability(template(is_active=False), snapshot())

    assert (result.state, result.code) == (CapabilityState.UNAVAILABLE, "TEMPLATE_INACTIVE")


@pytest.mark.parametrize("not_a_class", ["Contact Onibex for connector class", "StarTree native integration (no Kafka Connect class)", "", None])
def test_a_catalog_placeholder_with_no_installable_class_is_unavailable(not_a_class) -> None:
    result = ca.template_availability(template(connector_class=not_a_class), snapshot())

    assert (result.state, result.code) == (CapabilityState.UNAVAILABLE, "NO_CONNECTOR_CLASS")


def test_a_template_in_a_pool_that_does_not_exist_is_unavailable() -> None:
    result = ca.template_availability(template(runtime_pool_type="kafka-connect"), None)

    assert (result.state, result.code) == (CapabilityState.UNAVAILABLE, "UNKNOWN_POOL")


def test_an_unreachable_cluster_is_requires_configuration_never_supported_and_says_it_could_not_be_checked() -> None:
    result = ca.template_availability(template(), ca.RuntimeSnapshot(None, None, error="ConnectError"))

    assert result.state is CapabilityState.REQUIRES_CONFIGURATION
    assert result.code == "RUNTIME_UNREACHABLE"
    assert result.verified is False
    assert "DATACONNECT_KAFKA_CONNECT_URL" in result.remediation


def test_an_inactive_template_stays_unavailable_even_when_the_cluster_is_unreachable() -> None:
    """A fact about the catalog entry needs no live check, so it is not downgraded to "could not be confirmed"."""
    assert ca.template_availability(template(is_active=False), ca.RuntimeSnapshot(None, None, error="x")).code == "TEMPLATE_INACTIVE"


# ── Parity with the deploy preflight ───────────────────────────────────────────────────────────────────────────────────

class FakeConnect:
    def __init__(self, plugins, worker=WORKER) -> None:
        self.plugins, self.worker = plugins, worker

    async def list_connector_plugins(self):
        return self.plugins

    async def get_worker_info(self):
        return self.worker


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {}, {"connector_class": "com.example.NotThere"}, {"connector_version": "99.0.0"}, {"supported_kafka_connect_versions": ["3.5"]},
    {"supported_kafka_connect_versions": ["7.6"]}, {"connector_version": "10.7.4"},
])
@pytest.mark.parametrize("plugins", [INSTALLED, [], [{"class": JDBC_SINK, "type": "source", "version": "10.7.4"}], [{"class": JDBC_SINK, "type": "sink", "version": "9.9.9"}]])
async def test_the_catalog_and_the_deploy_preflight_never_disagree_about_a_plugin(over, plugins) -> None:
    """Supported in the catalog <=> the deploy preflight finds no plugin or worker-version problem. Asked of the SAME plugin list."""
    t = template(**over)
    pipeline = {"id": "p", "mode": None}

    in_catalog = ca.template_availability(t, snapshot(plugins)).state is CapabilityState.SUPPORTED
    failures = await collect_preflight_failures(template=t, pipeline=pipeline, config={}, client=FakeConnect(plugins))
    preflight_ok = not [f for f in failures if f["check"] in {"connector_class_installed", "connector_plugin_version", "kafka_connect_version_compatible"}]

    assert in_catalog == preflight_ok


# ── Asking the clusters ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def clusters(monkeypatch):
    """Replaces the live query with canned answers per endpoint, recording who was asked."""
    box = SimpleNamespace(asked=[], answers={})

    async def _snapshot(pool_id, need_worker):
        box.asked.append((ca.pool_endpoint(pool_id), need_worker))
        return box.answers.get(ca.pool_endpoint(pool_id), snapshot())

    monkeypatch.setattr(ca, "_snapshot", _snapshot)
    return box


@pytest.mark.asyncio
async def test_templates_on_one_cluster_ask_it_once(clusters, monkeypatch) -> None:
    monkeypatch.setattr(ca.settings, "connect_pools", {})
    templates = [template(id=f"t{i}", runtime_pool_type=pool) for i, pool in enumerate(["connect-sink-pool", "connect-jdbc-pool", "connect-cdc-pool"] * 4)]

    result = await ca.availability_for_templates(templates)

    assert len(result) == 12
    assert len(clusters.asked) == 1                                        # one shared cluster: one question, however many templates and pools


@pytest.mark.asyncio
async def test_each_distinct_cluster_is_asked_once_and_answers_its_own_templates(clusters, monkeypatch) -> None:
    monkeypatch.setattr(ca.settings, "connect_pools", {"connect-sink-pool": "http://sinks:8083", "connect-jdbc-pool": "http://jdbc:8083"})
    clusters.answers = {"http://sinks:8083": snapshot(INSTALLED), "http://jdbc:8083": snapshot([])}      # the JDBC cluster has nothing installed
    templates = [template(id="sink", runtime_pool_type="connect-sink-pool"), template(id="src", runtime_pool_type="connect-jdbc-pool", connector_type="source",
                                                                                       connector_class=JDBC_SOURCE)]

    result = await ca.availability_for_templates(templates)

    assert result["sink"].state is CapabilityState.SUPPORTED
    assert (result["src"].state, result["src"].code) == (CapabilityState.UNAVAILABLE, "PLUGIN_NOT_INSTALLED")
    assert sorted(e for e, _ in clusters.asked) == ["http://jdbc:8083", "http://sinks:8083"]


@pytest.mark.asyncio
async def test_the_worker_version_is_only_requested_when_a_template_pins_one(clusters, monkeypatch) -> None:
    monkeypatch.setattr(ca.settings, "connect_pools", {})

    await ca.availability_for_templates([template(id="a"), template(id="b")])
    await ca.availability_for_templates([template(id="a"), template(id="b", supported_kafka_connect_versions=["7.6"])])

    assert [need for _e, need in clusters.asked] == [False, True]


@pytest.mark.asyncio
async def test_one_unreachable_cluster_does_not_spoil_the_others(clusters, monkeypatch) -> None:
    monkeypatch.setattr(ca.settings, "connect_pools", {"connect-sink-pool": "http://down:8083"})
    clusters.answers = {"http://down:8083": ca.RuntimeSnapshot(None, None, error="ConnectError")}
    templates = [template(id="on-the-dead-cluster", runtime_pool_type="connect-sink-pool"),
                 template(id="on-the-live-one", runtime_pool_type="connect-jdbc-pool", connector_type="source", connector_class=JDBC_SOURCE)]

    result = await ca.availability_for_templates(templates)

    assert result["on-the-dead-cluster"].code == "RUNTIME_UNREACHABLE"
    assert result["on-the-live-one"].state is CapabilityState.SUPPORTED


@pytest.mark.asyncio
async def test_a_template_in_an_unknown_pool_is_answered_without_asking_any_cluster(clusters) -> None:
    result = await ca.availability_for_templates([template(id="odd", runtime_pool_type="kafka-connect")])

    assert result["odd"].code == "UNKNOWN_POOL"
    assert clusters.asked == []


@pytest.mark.asyncio
async def test_an_empty_catalog_asks_nobody(clusters) -> None:
    assert await ca.availability_for_templates([]) == {}
    assert clusters.asked == []


@pytest.mark.asyncio
async def test_annotate_adds_a_serializable_availability_block_and_leaves_the_inputs_alone(clusters) -> None:
    original = template()

    annotated = await ca.annotate([original])

    assert "availability" not in original
    assert annotated[0]["availability"] == {"state": "supported", "code": None, "message": None, "remediation": None, "verified": True}


# ── The live query itself ──────────────────────────────────────────────────────────────────────────────────────────────

class _Live:
    def __init__(self, plugins=None, worker=None, error=None, delay=0.0) -> None:
        self.plugins, self.worker, self.error, self.delay, self.calls = plugins, worker, error, delay, 0

    async def list_connector_plugins(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.plugins

    async def get_worker_info(self):
        return self.worker


@pytest.mark.asyncio
async def test_a_cluster_that_errors_is_reported_unreachable_with_only_the_error_class(monkeypatch) -> None:
    from app.services import connector_availability as module

    monkeypatch.setattr(module, "_client_for", lambda pool_id: _Live(error=RuntimeError("boom http://user:pw@internal:8083")))

    result = await module._query_cluster("connect-sink-pool", False)

    assert (result.plugins, result.error) == (None, "RuntimeError")          # the message, which could name a host, never leaves


@pytest.mark.asyncio
async def test_a_cluster_that_does_not_answer_in_time_is_reported_unreachable(monkeypatch) -> None:
    from app.services import connector_availability as module

    monkeypatch.setattr(module.settings, "connector_availability_timeout_seconds", 0.05)
    monkeypatch.setattr(module, "_client_for", lambda pool_id: _Live(plugins=INSTALLED, delay=2.0))

    result = await module._query_cluster("connect-sink-pool", False)

    assert (result.plugins, result.error) == (None, "TimeoutError")


@pytest.mark.asyncio
async def test_a_cluster_that_answers_gives_its_plugins_and_optionally_its_worker(monkeypatch) -> None:
    from app.services import connector_availability as module

    live_client = _Live(plugins=INSTALLED, worker=WORKER)
    monkeypatch.setattr(module, "_client_for", lambda pool_id: live_client)

    without_worker = await module._query_cluster("connect-sink-pool", False)
    with_worker = await module._query_cluster("connect-sink-pool", True)

    assert (without_worker.plugins, without_worker.worker) == (INSTALLED, None)
    assert (with_worker.plugins, with_worker.worker) == (INSTALLED, WORKER)


# ── The API ────────────────────────────────────────────────────────────────────────────────────────────────────────────

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
BASE = "/api/v1/dataconnect/connector-templates"


def _api_template(tid, **over):
    return {"id": tid, "name": tid, "connector_type": "sink", "category": "database", "connector_class": JDBC_SINK,
            "runtime_pool_type": "connect-sink-pool", "version": "v1", "is_active": True, **over}


def test_listing_the_catalog_says_for_each_template_whether_it_can_run(monkeypatch, clusters) -> None:
    async def _list():
        return [_api_template("installed"), _api_template("missing", connector_class="com.example.NotThere"), _api_template("off", is_active=False)]

    monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", _list)

    response = client.get(BASE)

    assert response.status_code == 200
    by_id = {t["id"]: t["availability"] for t in response.json()}
    assert by_id["installed"]["state"] == "supported"
    assert (by_id["missing"]["state"], by_id["missing"]["code"]) == ("unavailable", "PLUGIN_NOT_INSTALLED")
    assert (by_id["off"]["state"], by_id["off"]["code"]) == ("unavailable", "TEMPLATE_INACTIVE")


def test_one_template_carries_its_availability_too(monkeypatch, clusters) -> None:
    async def _get(template_id):
        return _api_template(template_id, connector_class="com.example.NotThere")

    monkeypatch.setattr("app.api.routes.catalog.catalog_service.get_template", _get)

    body = client.get(f"{BASE}/t9").json()

    assert body["availability"]["code"] == "PLUGIN_NOT_INSTALLED"
    assert body["availability"]["remediation"] == "Install the connector plugin on the Kafka Connect worker."


def test_when_kafka_connect_cannot_be_reached_the_catalog_still_answers_and_says_so(monkeypatch) -> None:
    """The autouse default makes every cluster unreachable: the list must still work, never 500, and never claim "supported"."""
    async def _list():
        return [_api_template("a"), _api_template("b")]

    monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", _list)

    response = client.get(BASE)

    assert response.status_code == 200
    for t in response.json():
        assert t["availability"]["state"] == "requires_configuration"
        assert t["availability"]["code"] == "RUNTIME_UNREACHABLE"
        assert t["availability"]["verified"] is False


def test_the_existing_capability_flags_are_still_there(monkeypatch, clusters) -> None:
    async def _list():
        return [_api_template("a")]

    monkeypatch.setattr("app.api.routes.catalog.catalog_service.list_templates", _list)

    t = client.get(BASE).json()[0]

    assert "capabilities" in t
    assert "availability" in t
