"""
Kafka Connect worker pools, end to end through the application: a pipeline's pool is derived from its connector, fixed for
its life, recorded with its runtime state, and everything that talks to Kafka Connect — deploy, the reconciler, monitoring,
health — goes to the cluster that pool runs on, with one pool's cluster being down never stalling another's.
"""

import pytest

import app.services.runtime_reconciler as reconciler_module
from app.core.config import settings
from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services import monitoring_service as monitoring_module
from app.services import runtime_metrics
from app.services.monitoring_service import MonitoringService
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient
from app.services.runtime_reconciler import RuntimeReconciler

CDC = "http://connect-cdc:8083"
SINK = "http://connect-sink:8083"
SHARED = "http://shared:8083"


@pytest.fixture(autouse=True)
def _pools(monkeypatch):
    monkeypatch.setattr(settings, "kafka_connect_url", SHARED)
    monkeypatch.setattr(settings, "connect_pools", {"connect-cdc-pool": CDC, "connect-sink-pool": SINK})


def _template(fake_postgres, connector_type: str, pool: str | None, **extra) -> dict:
    klass = "io.confluent.connect.jdbc.JdbcSinkConnector" if connector_type == "sink" else "io.debezium.connector.postgresql.PostgresConnector"
    values = {"connector_class": klass, "connector_type": connector_type, "default_config": {}, **extra}
    if pool is not None:
        values["runtime_pool_type"] = pool
    return fake_postgres.create("connector_templates", values)


def _connection(fake_postgres, org_id: str) -> dict:
    return fake_postgres.create("connection_profiles", {"org_id": org_id, "name": "c", "connection_config": {}})


def _payload(template: dict, connection: dict, **overrides) -> PipelineCreate:
    base = {
        "name": "P", "template_id": template["id"], "connection_profile_id": connection["id"], "zone": "raw", "domain": "erp",
        "source_system": "sap", "entity": "customers", "mode": "cdc", "version": "v1",
    }
    base.update(overrides)
    return PipelineCreate(**base)


async def _create(org: str, template: dict, connection: dict, **overrides) -> dict:
    return await pipeline_service.create_pipeline(org, _payload(template, connection, **overrides))


# ── a pipeline's pool ──────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_pipelines_pool_comes_from_its_connector_template(fake_postgres) -> None:
    connection = _connection(fake_postgres, "org_a")
    cdc = _template(fake_postgres, "source", "connect-cdc-pool")
    sink = _template(fake_postgres, "sink", "connect-sink-pool")

    source_pipeline = await _create("org_a", cdc, connection)
    sink_pipeline = await _create(
        "org_a", sink, connection, zone="prepared", entity="customers_copy", mode="batch",
        connector_config={"topics": "raw-erp-sap-customers-cdc-v1"},
    )

    assert source_pipeline["runtime_pool_id"] == "connect-cdc-pool"
    assert sink_pipeline["runtime_pool_id"] == "connect-sink-pool"


@pytest.mark.asyncio
async def test_a_client_cannot_choose_a_pool_different_from_its_connectors(fake_postgres) -> None:
    template = _template(fake_postgres, "source", "connect-cdc-pool")

    with pytest.raises(ValidationError) as exc_info:
        await _create("org_a", template, _connection(fake_postgres, "org_a"), runtime_pool_id="connect-file-pool")

    assert "runs in the 'connect-cdc-pool' worker pool" in exc_info.value.message
    assert exc_info.value.field_errors[0].field == "runtime_pool_id"
    assert fake_postgres.pipelines == {}


@pytest.mark.asyncio
async def test_a_client_that_names_the_right_pool_is_accepted(fake_postgres) -> None:
    template = _template(fake_postgres, "source", "connect-stream-pool")
    connection = _connection(fake_postgres, "org_a")

    assert (await _create("org_a", template, connection, runtime_pool_id="connect-stream-pool"))["runtime_pool_id"] == "connect-stream-pool"
    assert (await _create("org_a", template, connection, runtime_pool_id="connect-queue-pool", entity="x"))["runtime_pool_id"] == "connect-stream-pool"


@pytest.mark.asyncio
async def test_a_template_naming_an_unknown_pool_cannot_be_used(fake_postgres) -> None:
    template = _template(fake_postgres, "source", "connect-imaginary-pool")

    with pytest.raises(ValidationError, match="unknown worker pool"):
        await _create("org_a", template, _connection(fake_postgres, "org_a"))


@pytest.mark.asyncio
async def test_a_sink_cannot_run_outside_the_sink_pool_and_a_source_cannot_run_inside_it(fake_postgres) -> None:
    connection = _connection(fake_postgres, "org_a")
    misplaced_sink = _template(fake_postgres, "sink", "connect-jdbc-pool")
    misplaced_source = _template(fake_postgres, "source", "connect-sink-pool")

    with pytest.raises(ValidationError, match="sinks belong in 'connect-sink-pool'"):
        await _create("org_a", misplaced_sink, connection, connector_config={"topics": "raw-t-s-x-batch-v1"})
    with pytest.raises(ValidationError, match="only sinks run in 'connect-sink-pool'"):
        await _create("org_a", misplaced_source, connection)


# ── a pipeline's pool is fixed ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_pool_cannot_be_edited(fake_postgres) -> None:
    created = await _create("org_a", _template(fake_postgres, "source", "connect-cdc-pool"), _connection(fake_postgres, "org_a"))

    with pytest.raises(ValidationError, match="cannot be changed"):
        await pipeline_service.update_pipeline("org_a", created["id"], PipelineUpdate(runtime_pool_id="connect-file-pool"))

    same = await pipeline_service.update_pipeline(
        "org_a", created["id"], PipelineUpdate(runtime_pool_id="connect-cdc-pool", description="note"),
    )
    assert same["runtime_pool_id"] == "connect-cdc-pool"


@pytest.mark.asyncio
async def test_a_draft_may_be_retargeted_to_a_connector_in_another_pool(fake_postgres) -> None:
    connection = _connection(fake_postgres, "org_a")
    created = await _create("org_a", _template(fake_postgres, "source", "connect-cdc-pool"), connection)
    other = _template(fake_postgres, "source", "connect-jdbc-pool", connector_class="io.confluent.connect.jdbc.JdbcSourceConnector")

    updated = await pipeline_service.update_pipeline("org_a", created["id"], PipelineUpdate(template_id=other["id"]))

    assert updated["runtime_pool_id"] == "connect-jdbc-pool"  # nothing deployed yet, nothing stranded


@pytest.mark.asyncio
async def test_a_deployed_pipeline_cannot_move_to_a_connector_in_another_pool(fake_postgres) -> None:
    connection = _connection(fake_postgres, "org_a")
    created = await _create("org_a", _template(fake_postgres, "source", "connect-cdc-pool"), connection)
    fake_postgres.pipelines[created["id"]].status = "running"
    other = _template(fake_postgres, "source", "connect-jdbc-pool", connector_class="io.confluent.connect.jdbc.JdbcSourceConnector")

    with pytest.raises(ValidationError, match="cannot move between pools"):
        await pipeline_service.update_pipeline("org_a", created["id"], PipelineUpdate(template_id=other["id"]))

    assert (await pipeline_service.get_pipeline("org_a", created["id"]))["runtime_pool_id"] == "connect-cdc-pool"


@pytest.mark.asyncio
async def test_a_deployed_pipeline_may_move_to_another_connector_in_the_same_pool(fake_postgres) -> None:
    connection = _connection(fake_postgres, "org_a")
    created = await _create("org_a", _template(fake_postgres, "source", "connect-cdc-pool"), connection)
    fake_postgres.pipelines[created["id"]].status = "running"
    same_pool = _template(fake_postgres, "source", "connect-cdc-pool", connector_class="io.debezium.connector.mysql.MySqlConnector")

    updated = await pipeline_service.update_pipeline("org_a", created["id"], PipelineUpdate(template_id=same_pool["id"]))

    assert updated["runtime_pool_id"] == "connect-cdc-pool"


@pytest.mark.asyncio
async def test_the_runtime_state_remembers_the_pool_even_after_the_pipeline_is_gone(fake_postgres) -> None:
    reconciler = RuntimeReconciler(client_factory=lambda: None, worker_id="w")

    await reconciler.record_desired("org_a", "pipe-1", "conn-1", "deployed", "h", pool_id="connect-cdc-pool")
    await reconciler.record_desired("org_a", "pipe-1", "conn-1", "paused")  # a later call that doesn't repeat it

    row = next(iter(fake_postgres.pipeline_runtime_states.values()))
    assert row.runtime_pool_id == "connect-cdc-pool"


@pytest.mark.asyncio
async def test_a_runtime_row_from_before_pools_learns_its_pool_on_the_next_call(fake_postgres) -> None:
    fake_postgres.create("pipeline_runtime_states", {"org_id": "org_a", "pipeline_id": "pipe-1", "connector_name": "c", "desired_state": "running", "desired_generation": 1})
    reconciler = RuntimeReconciler(client_factory=lambda: None, worker_id="w")

    await reconciler.record_desired("org_a", "pipe-1", "c", "running", pool_id="connect-sink-pool")

    assert next(iter(fake_postgres.pipeline_runtime_states.values())).runtime_pool_id == "connect-sink-pool"


# ── deploy goes to the pool's cluster ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deploy_preflights_and_deploys_on_the_cluster_of_the_pipelines_pool(monkeypatch, fake_postgres) -> None:
    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    seen: list[tuple[str, str]] = []
    recorded: list[dict] = []

    async def _plugins(self):
        seen.append(("plugins", self.base_url))
        return [{"class": "io.debezium.connector.postgresql.PostgresConnector", "type": "source", "version": "2.5.4.Final"}]

    async def _deploy(self, name, config):
        seen.append(("deploy", self.base_url))
        return {"config": config}

    async def _worker(self):
        seen.append(("worker", self.base_url))
        return {"version": "7.6.0"}

    async def _record(*args, **kwargs):
        recorded.append(kwargs)

    async def _lineage(*args, **kwargs):
        return {"nodes": [], "edges": []}

    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _plugins)
    monkeypatch.setattr(KafkaConnectClient, "get_worker_info", _worker)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _deploy)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _record)
    monkeypatch.setattr("app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _lineage)
    template = _template(fake_postgres, "source", "connect-cdc-pool", connector_version="2.5.0")
    created = await _create("org_a", template, _connection(fake_postgres, "org_a"))

    await pipeline_service.deploy("org_a", created["id"])

    assert {url for _, url in seen} == {CDC}  # every Connect call went to the CDC pool's cluster, none to the shared one
    assert ("deploy", CDC) in seen
    assert recorded[0]["pool_id"] == "connect-cdc-pool"


# ── the reconciler works each pool against its own cluster ─────────────────────────────────────────────────────────


class _Cluster:
    def __init__(self, url: str) -> None:
        self.url = url


@pytest.fixture
def clusters(monkeypatch):
    """Fake Connect clusters keyed by URL; records which cluster each row was reconciled against."""
    state = {"connectors": {CDC: ["conn-cdc"], SINK: ["conn-sink"], SHARED: ["conn-jdbc"]}, "down": set(), "lists": [], "handled": []}

    async def _list(client):
        state["lists"].append(client.url)
        if client.url in state["down"]:
            raise ConnectionError("refused")
        return state["connectors"][client.url]

    async def _reconcile_row(self, session, repo, client, existing, row):
        state["handled"].append((row.pipeline_id, client.url, set(existing)))
        return {"pipeline_id": row.pipeline_id}

    monkeypatch.setattr(reconciler_module, "KafkaConnectClient", _Cluster)
    monkeypatch.setattr(reconciler_module, "list_connectors_retryable", _list)
    monkeypatch.setattr(RuntimeReconciler, "_reconcile_row", _reconcile_row)
    return state


def _row(fake_postgres, pipeline_id: str, pool: str | None) -> None:
    fake_postgres.create("pipeline_runtime_states", {"org_id": "org_a", "pipeline_id": pipeline_id, "connector_name": f"conn-{pipeline_id}", "runtime_pool_id": pool})


@pytest.mark.asyncio
async def test_each_row_is_reconciled_against_the_cluster_of_its_pool(fake_postgres, clusters) -> None:
    _row(fake_postgres, "cdc-1", "connect-cdc-pool")
    _row(fake_postgres, "sink-1", "connect-sink-pool")
    _row(fake_postgres, "jdbc-1", "connect-jdbc-pool")  # no cluster of its own -> shared
    _row(fake_postgres, "legacy-1", None)  # from before pools existed -> shared

    await RuntimeReconciler(worker_id="w").reconcile_once()

    by_pipeline = {pid: (url, existing) for pid, url, existing in clusters["handled"]}
    assert by_pipeline["cdc-1"] == (CDC, {"conn-cdc"})
    assert by_pipeline["sink-1"] == (SINK, {"conn-sink"})
    assert by_pipeline["jdbc-1"] == (SHARED, {"conn-jdbc"})
    assert by_pipeline["legacy-1"] == (SHARED, {"conn-jdbc"})


@pytest.mark.asyncio
async def test_a_clusters_connector_list_is_fetched_once_however_many_rows_use_it(fake_postgres, clusters) -> None:
    clusters["connectors"][CDC] = [f"conn-cdc-{i}" for i in range(4)]
    clusters["connectors"][SINK] = ["conn-sink-1"]
    for i in range(4):
        _row(fake_postgres, f"cdc-{i}", "connect-cdc-pool")
    _row(fake_postgres, "sink-1", "connect-sink-pool")

    await RuntimeReconciler(worker_id="w").reconcile_once()

    assert sorted(clusters["lists"]) == [CDC, SINK]


@pytest.mark.asyncio
async def test_one_pools_cluster_being_down_does_not_stall_the_others(fake_postgres, clusters) -> None:
    clusters["down"].add(CDC)
    _row(fake_postgres, "cdc-1", "connect-cdc-pool")
    _row(fake_postgres, "sink-1", "connect-sink-pool")
    _row(fake_postgres, "jdbc-1", "connect-jdbc-pool")

    await RuntimeReconciler(worker_id="w").reconcile_once()

    assert {pid for pid, _, _ in clusters["handled"]} == {"sink-1", "jdbc-1"}  # the CDC row waits; nothing else does


@pytest.mark.asyncio
async def test_the_cluster_that_is_down_is_retried_on_the_next_tick(fake_postgres, clusters) -> None:
    clusters["down"].add(SINK)
    _row(fake_postgres, "sink-1", "connect-sink-pool")
    reconciler = RuntimeReconciler(worker_id="w")
    await reconciler.reconcile_once()
    assert clusters["handled"] == []

    clusters["down"].clear()
    await reconciler.reconcile_once()

    assert [pid for pid, _, _ in clusters["handled"]] == ["sink-1"]


# ── monitoring ─────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def live(monkeypatch):
    """Fake Connect REST per cluster URL: which connectors exist and what state they report."""
    state = {"connectors": {CDC: {"conn-cdc": "RUNNING"}, SINK: {"conn-sink": "FAILED"}, SHARED: {}}, "down": set(), "calls": []}

    async def _list(self):
        state["calls"].append(("list", self.base_url))
        if self.base_url in state["down"]:
            raise ConnectionError("refused")
        return list(state["connectors"][self.base_url])

    async def _status(self, name):
        state["calls"].append(("status", self.base_url, name))
        connector_state = state["connectors"][self.base_url][name]
        return {"connector": {"state": connector_state}, "tasks": [{"id": 0, "state": connector_state}]}

    monkeypatch.setattr(KafkaConnectClient, "list_connectors", _list)
    monkeypatch.setattr(KafkaConnectClient, "get_status", _status)
    monkeypatch.setattr(monitoring_module.settings, "monitoring_cache_ttl_seconds", 0.0)
    return state


def _pipeline(fake_postgres, name: str, pool: str | None, connector: str) -> dict:
    return fake_postgres.create("pipelines", {
        "org_id": "org_a", "name": name, "connector_name": connector, "runtime_pool_id": pool, "topic_name": f"org_a.raw.{name}",
        "domain": "d", "source_system": "s", "entity": name, "version": "v1", "direction": "SOURCE", "connector_config": {},
    })


@pytest.mark.asyncio
async def test_monitoring_lists_all_six_pools_with_their_clusters_and_this_orgs_state(fake_postgres, live) -> None:
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")

    pools = {p["id"]: p for p in await MonitoringService().list_connect_clusters("org_a")}

    assert list(pools) == [
        "connect-cdc-pool", "connect-jdbc-pool", "connect-file-pool", "connect-api-pool", "connect-stream-pool", "connect-sink-pool",
    ]
    assert pools["connect-cdc-pool"]["status"] == "healthy"
    assert pools["connect-cdc-pool"]["runtime_endpoint"] == CDC
    assert pools["connect-cdc-pool"]["shared_cluster"] is False
    assert pools["connect-cdc-pool"]["connector_count"] == 1
    assert pools["connect-sink-pool"]["status"] == "degraded"  # its connector is FAILED
    assert pools["connect-jdbc-pool"]["status"] == "idle"  # this org has nothing there
    assert pools["connect-jdbc-pool"]["shared_cluster"] is True  # jdbc/file/api/stream all share one cluster
    assert pools["connect-jdbc-pool"]["connector_count"] == 0


@pytest.mark.asyncio
async def test_a_connector_is_looked_up_on_its_pools_cluster_not_the_shared_one(fake_postgres, live) -> None:
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")

    status = await MonitoringService().connector_status("org_a", "conn-cdc")

    assert status["observed_state"] == "running"
    assert status["runtime_endpoint"] == CDC
    assert {call[1] for call in live["calls"]} == {CDC}  # the shared cluster was never asked


@pytest.mark.asyncio
async def test_a_pools_connector_list_contains_only_that_pools_pipelines(fake_postgres, live) -> None:
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")

    connectors = await MonitoringService().list_cluster_connectors("org_a", "connect-cdc-pool")

    assert [c["connector_name"] for c in connectors] == ["conn-cdc"]
    assert connectors[0]["runtime_pool_id"] == "connect-cdc-pool"
    assert await MonitoringService().list_cluster_connectors("org_a", "connect-nonsense-pool") == []


@pytest.mark.asyncio
async def test_one_pools_cluster_down_makes_only_its_status_unknown(fake_postgres, live) -> None:
    live["down"].add(CDC)
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    live["connectors"][SINK]["conn-sink"] = "RUNNING"
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")

    pools = {p["id"]: p for p in await MonitoringService().list_connect_clusters("org_a")}

    assert pools["connect-cdc-pool"]["status"] == "unknown"
    assert pools["connect-sink-pool"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_the_metrics_summary_spans_every_cluster_the_org_uses(fake_postgres, live, monkeypatch) -> None:
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")

    async def _zero(*args, **kwargs):
        return 0

    service = MonitoringService()
    monkeypatch.setattr(service, "_validation_failure_count", _zero)
    monkeypatch.setattr(service, "_ingestion_failure_count", _zero)

    summary = await service.metrics_summary("org_a")

    assert summary["connector_running_count"] == 1
    assert summary["failed_connector_count"] == 1
    assert {call[1] for call in live["calls"] if call[0] == "list"} == {CDC, SINK}


@pytest.mark.asyncio
async def test_the_metrics_summary_reports_unreachable_when_any_cluster_is_down(fake_postgres, live, monkeypatch) -> None:
    live["down"].add(SINK)
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")

    async def _zero(*args, **kwargs):
        return 0

    service = MonitoringService()
    monkeypatch.setattr(service, "_validation_failure_count", _zero)
    monkeypatch.setattr(service, "_ingestion_failure_count", _zero)

    summary = await service.metrics_summary("org_a")

    assert summary["extra"]["error"]
    assert summary["extra"]["unreachable_clusters"] == 1


@pytest.mark.asyncio
async def test_the_flow_view_reads_each_connector_from_its_pools_cluster(fake_postgres, monkeypatch) -> None:
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")
    asked: list[tuple[str, str]] = []

    async def _get(path, base_url=None):
        asked.append((path, base_url))
        name = {CDC: "conn-cdc", SINK: "conn-sink"}[base_url]
        return {name: {"status": {"type": "source", "connector": {"state": "RUNNING"}, "tasks": []},
                       "info": {"config": {}}}}

    async def _no_kafka(*args, **kwargs):
        raise RuntimeError("no kafka in this test")

    monkeypatch.setattr(runtime_metrics, "_connect_get", _get)
    monkeypatch.setattr(runtime_metrics.asyncio, "to_thread", _no_kafka)

    result = await runtime_metrics.flow_summary("org_a")

    assert {c["name"] for c in result["connectors"]} == {"conn-cdc", "conn-sink"}
    assert (runtime_metrics._BULK_PATH, CDC) in asked
    assert (runtime_metrics._BULK_PATH, SINK) in asked
    assert not any(url == SHARED for _, url in asked)


@pytest.mark.asyncio
async def test_the_flow_view_survives_one_cluster_being_down(fake_postgres, monkeypatch) -> None:
    _pipeline(fake_postgres, "cdc", "connect-cdc-pool", "conn-cdc")
    _pipeline(fake_postgres, "sink", "connect-sink-pool", "conn-sink")

    async def _get(path, base_url=None):
        if base_url == CDC:
            raise ConnectionError("cdc cluster down")
        return {"conn-sink": {"status": {"type": "sink", "connector": {"state": "RUNNING"}, "tasks": []},
                              "info": {"config": {}}}}

    async def _no_kafka(*args, **kwargs):
        raise RuntimeError("no kafka in this test")

    monkeypatch.setattr(runtime_metrics, "_connect_get", _get)
    monkeypatch.setattr(runtime_metrics.asyncio, "to_thread", _no_kafka)

    result = await runtime_metrics.flow_summary("org_a")

    assert [c["name"] for c in result["connectors"]] == ["conn-sink"]  # the reachable pool's connector is still shown
    assert result["reachable"] is False
    assert CDC in result["message"]


# ── health ─────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_checks_every_cluster_and_names_the_one_that_is_down(monkeypatch) -> None:
    from app.api.routes.health import _check_kafka_connect

    async def _list(self):
        if self.base_url == SINK:
            raise ConnectionError("sink cluster refused")
        return []

    monkeypatch.setattr(KafkaConnectClient, "list_connectors", _list)

    result = await _check_kafka_connect()

    assert result["ok"] is False
    assert result["clusters"][SINK]["ok"] is False
    assert result["clusters"][SINK]["pools"] == ["connect-sink-pool"]
    assert result["clusters"][CDC]["ok"] is True


@pytest.mark.asyncio
async def test_readiness_stays_a_single_flag_for_a_one_cluster_setup(monkeypatch) -> None:
    from app.api.routes.health import _check_kafka_connect

    async def _list(self):
        return []

    monkeypatch.setattr(settings, "connect_pools", {})
    monkeypatch.setattr(KafkaConnectClient, "list_connectors", _list)

    assert await _check_kafka_connect() == {"ok": True}


# ── giving a pool its own cluster after connectors already ran on the shared one ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_connector_still_on_the_shared_cluster_is_not_duplicated_onto_its_pools_new_cluster(fake_postgres, clusters) -> None:
    clusters["connectors"][SHARED] = ["conn-cdc-1"]  # the original, still running on the shared cluster
    clusters["connectors"][CDC] = []  # the pool's new cluster is empty
    _row(fake_postgres, "cdc-1", "connect-cdc-pool")
    row = next(iter(fake_postgres.pipeline_runtime_states.values()))

    await RuntimeReconciler(worker_id="w").reconcile_once()

    assert clusters["handled"] == []  # no second copy is created
    assert "still running on the shared Kafka Connect cluster" in row.last_runtime_error
    assert row.last_reconciled_at is not None


@pytest.mark.asyncio
async def test_once_the_original_is_gone_from_the_shared_cluster_the_connector_is_created_on_the_pools_cluster(fake_postgres, clusters) -> None:
    clusters["connectors"][SHARED] = []
    clusters["connectors"][CDC] = []
    _row(fake_postgres, "cdc-1", "connect-cdc-pool")

    await RuntimeReconciler(worker_id="w").reconcile_once()

    assert [(pid, url) for pid, url, _ in clusters["handled"]] == [("cdc-1", CDC)]


@pytest.mark.asyncio
async def test_a_connector_already_on_its_pools_cluster_is_reconciled_normally_even_if_the_name_exists_on_the_shared_one(fake_postgres, clusters) -> None:
    clusters["connectors"][SHARED] = ["conn-cdc-1"]
    clusters["connectors"][CDC] = ["conn-cdc-1"]
    _row(fake_postgres, "cdc-1", "connect-cdc-pool")

    await RuntimeReconciler(worker_id="w").reconcile_once()

    assert [pid for pid, _, _ in clusters["handled"]] == ["cdc-1"]


@pytest.mark.asyncio
async def test_a_connector_being_deleted_is_never_held_back_by_the_shared_cluster_check(fake_postgres, clusters) -> None:
    clusters["connectors"][SHARED] = ["conn-cdc-1"]
    clusters["connectors"][CDC] = []
    fake_postgres.create("pipeline_runtime_states", {
        "org_id": "org_a", "pipeline_id": "cdc-1", "connector_name": "conn-cdc-1", "runtime_pool_id": "connect-cdc-pool",
        "desired_state": "deleted",
    })

    await RuntimeReconciler(worker_id="w").reconcile_once()

    assert [pid for pid, _, _ in clusters["handled"]] == ["cdc-1"]
