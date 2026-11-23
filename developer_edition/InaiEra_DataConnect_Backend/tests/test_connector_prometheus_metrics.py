"""Per-connector Prometheus metrics (app/services/connector_prometheus_metrics.py, GET /runtime/connectors/{name}/metrics)."""

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

import app.services.connector_prometheus_metrics as module
from app.core.config import settings
from app.main import app
from app.services.connector_prometheus_metrics import (
    METRICS,
    QUERIED_SERIES,
    ConnectorPrometheusMetrics,
    metrics_for,
    promql_string,
    render_query,
)

JMX_RULES = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "infra" / "jmx-exporter" / "kafka-connect.yml").read_text()
)


def _seed(fake_postgres, org_id: str, **overrides) -> dict:
    pipeline = {
        "org_id": org_id,
        "name": "Accounts CDC",
        "connector_name": "crm--salesforce--accounts--cdc--v1",
        "topic_name": f"{org_id}.raw.crm.salesforce.accounts.cdc.v1",
        "domain": "crm", "source_system": "salesforce", "entity": "accounts", "version": "v1",
        "runtime_pool_id": "connect-cdc-pool",
        "direction": "SOURCE",
    }
    pipeline.update(overrides)
    return fake_postgres.create("pipelines", pipeline)


def _matrix(value_pairs):
    return {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": value_pairs}]}}


def _vector(rows):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": labels, "value": [1700000000, value]} for labels, value in rows
    ]}}


class _FakePrometheus:
    """Answers Prometheus' HTTP API from canned data, recording every PromQL query it was sent."""

    def __init__(self, fail: bool = False):
        self.queries: list[str] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)["query"][0]
        self.queries.append(query)
        if self.fail:
            return httpx.Response(503, json={"status": "error", "errorType": "unavailable", "error": "tsdb not ready"})
        if request.url.path.endswith("/query_range"):
            if "poll_rate" in query:
                return httpx.Response(200, json=_matrix([[1700000000, "10"], [1700000060, "12.5"]]))
            if "running_ratio" in query:
                return httpx.Response(200, json=_matrix([[1700000000, "1"], [1700000060, "NaN"]]))
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
        if query.startswith("kafka_connect_task_status"):
            return httpx.Response(200, json=_vector([
                ({"task": "1", "status": "failed", "pool": "connect-cdc-pool"}, "1"),
                ({"task": "0", "status": "running", "pool": "connect-cdc-pool"}, "1"),
            ]))
        if "poll_rate" in query:
            return httpx.Response(200, json=_vector([({"task": "0"}, "7"), ({"task": "1"}, "5.5")]))
        return httpx.Response(200, json=_vector([]))


@pytest.fixture
def prometheus(monkeypatch):
    def install(fail: bool = False) -> _FakePrometheus:
        fake = _FakePrometheus(fail=fail)
        monkeypatch.setattr(settings, "prometheus_url", "http://prometheus:9090")

        async def _client(base_url: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(fake.handler))

        monkeypatch.setattr(module, "get_client", _client)
        return fake

    return install


# ── PromQL construction ─────────────────────────────────────────────────────

def test_promql_string_escapes_quotes_and_backslashes() -> None:
    assert promql_string('a"b\\c') == '"a\\"b\\\\c"'


def test_render_query_selects_only_the_named_connector() -> None:
    query = render_query("sum(rate(x{sel}{win}))", "org1--crm--v1", 120)
    assert query == 'sum(rate(x{connector="org1--crm--v1"}[120s]))'
    running = render_query("count(s{sel_running})", "c", 60)
    assert running == 'count(s{connector="c",status="running"})'


def test_source_and_sink_connectors_get_their_own_metric_sets() -> None:
    source_ids = {m.id for m in metrics_for("source")}
    sink_ids = {m.id for m in metrics_for("sink")}
    assert {"source_poll_rate", "source_write_rate"} <= source_ids
    assert not {"sink_read_rate", "sink_consumer_lag", "dlq_produced"} & source_ids
    assert {"sink_read_rate", "sink_send_rate", "sink_consumer_lag", "dlq_produced"} <= sink_ids
    assert not {"source_poll_rate"} & sink_ids
    assert {"running_ratio", "record_error_rate"} <= source_ids & sink_ids


# ── Contract with the JMX exporter rules ────────────────────────────────────

def _exported_name(bean_and_value: str) -> str | None:
    """Mimic the JMX exporter: rule patterns are matched as ^.*(?:pattern).*$, groups substituted into `name`,
    output lower-cased and invalid characters replaced with `_`."""
    for rule in JMX_RULES["rules"]:
        match = re.match(r"^.*(?:" + rule["pattern"] + r").*$", bean_and_value)
        if match:
            name = rule["name"]
            for i, group in enumerate(match.groups(), start=1):
                name = name.replace(f"${i}", group or "")
            return re.sub(r"[^a-zA-Z0-9_:]", "_", name).lower()
    return None


@pytest.mark.parametrize(("bean", "expected"), [
    ("kafka.connect<type=source-task-metrics, connector=o--a--v1, task=0><>source-record-poll-rate: 1.5",
     "kafka_connect_source_task_source_record_poll_rate"),
    ("kafka.connect<type=source-task-metrics, connector=o--a--v1, task=0><>source-record-write-rate: 1.5",
     "kafka_connect_source_task_source_record_write_rate"),
    ("kafka.connect<type=source-task-metrics, connector=o--a--v1, task=0><>poll-batch-avg-time-ms: 3.0",
     "kafka_connect_source_task_poll_batch_avg_time_ms"),
    ("kafka.connect<type=sink-task-metrics, connector=o--a--v1, task=2><>sink-record-read-rate: 4.0",
     "kafka_connect_sink_task_sink_record_read_rate"),
    ("kafka.connect<type=sink-task-metrics, connector=o--a--v1, task=2><>sink-record-send-rate: 4.0",
     "kafka_connect_sink_task_sink_record_send_rate"),
    ("kafka.connect<type=sink-task-metrics, connector=o--a--v1, task=2><>put-batch-avg-time-ms: NaN",
     "kafka_connect_sink_task_put_batch_avg_time_ms"),
    ("kafka.connect<type=connector-task-metrics, connector=o--a--v1, task=0><>offset-commit-avg-time-ms: 1.0",
     "kafka_connect_connector_task_offset_commit_avg_time_ms"),
    ("kafka.connect<type=connector-task-metrics, connector=o--a--v1, task=0><>offset-commit-failure-percentage: 0.0",
     "kafka_connect_connector_task_offset_commit_failure_percentage"),
    ("kafka.connect<type=connector-task-metrics, connector=o--a--v1, task=0><>running-ratio: 1.0",
     "kafka_connect_connector_task_running_ratio"),
    ("kafka.connect<type=connector-task-metrics, connector=o--a--v1, task=0><>status: running",
     "kafka_connect_task_status"),
    ("kafka.connect<type=task-error-metrics, connector=o--a--v1, task=0><>total-record-failures: 0.0",
     "kafka_connect_task_error_total_record_failures"),
    ("kafka.connect<type=task-error-metrics, connector=o--a--v1, task=0><>deadletterqueue-produce-requests: 0.0",
     "kafka_connect_task_error_deadletterqueue_produce_requests"),
])
def test_every_queried_series_is_produced_by_an_exporter_rule(bean, expected) -> None:
    assert _exported_name(bean) == expected
    assert expected in QUERIED_SERIES


def test_the_contract_list_covers_every_metric_query() -> None:
    names_in_queries = set()
    for metric in METRICS:
        for query in filter(None, (metric.query, metric.task_query)):
            names_in_queries |= set(re.findall(r"kafka_connect_[a-z_]+", query))
    assert names_in_queries <= QUERIED_SERIES


def test_sink_consumer_lag_keeps_the_connector_name_and_task_apart() -> None:
    bean = "kafka.consumer<type=consumer-fetch-manager-metrics, client-id=connector-consumer-org-1--crm--v1-10><>records-lag-max: 3"
    rule = next(r for r in JMX_RULES["rules"] if r["name"] == "kafka_connect_sink_consumer_records_lag_max")
    match = re.match(r"^.*(?:" + rule["pattern"] + r").*$", bean)
    assert match.group(1) == "org-1--crm--v1"
    assert match.group(2) == "10"


# ── Service ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_connector_this_org_does_not_own_is_not_found_and_never_queried(fake_postgres, prometheus) -> None:
    _seed(fake_postgres, "org_pm_owner")
    fake = prometheus()

    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_other", "crm--salesforce--accounts--cdc--v1")

    assert result is None
    assert fake.queries == []


@pytest.mark.asyncio
async def test_prometheus_not_configured_is_reported_not_zeroed(fake_postgres, monkeypatch) -> None:
    pipeline = _seed(fake_postgres, "org_pm_off")
    monkeypatch.setattr(settings, "prometheus_url", "")

    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_off", pipeline["connector_name"])

    assert result["prometheus_enabled"] is False
    assert result["reachable"] is False
    assert "DATACONNECT_PROMETHEUS_URL" in result["error"]
    assert all(m["current"] is None for m in result["metrics"])


@pytest.mark.asyncio
async def test_source_connector_metrics_series_current_values_and_tasks(fake_postgres, prometheus) -> None:
    pipeline = _seed(fake_postgres, "org_pm_src")
    fake = prometheus()

    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_src", pipeline["connector_name"], "15m")

    assert result["reachable"] is True
    assert result["prometheus_enabled"] is True
    assert result["direction"] == "source"
    assert result["pipeline_id"] == pipeline["id"]
    assert result["step_seconds"] == 15
    by_id = {m["id"]: m for m in result["metrics"]}
    assert by_id["source_poll_rate"]["current"] == 12.5
    assert by_id["source_poll_rate"]["series"] == [{"t": 1700000000.0, "v": 10.0}, {"t": 1700000060.0, "v": 12.5}]
    # NaN from Prometheus is "no value", and `current` falls back to the latest real sample.
    assert by_id["running_ratio"]["series"][-1]["v"] is None
    assert by_id["running_ratio"]["current"] == 1.0
    # No samples yet -> None, never 0.
    assert by_id["source_write_rate"]["current"] is None
    assert "sink_consumer_lag" not in by_id

    assert [t["task"] for t in result["tasks"]] == ["0", "1"]
    assert result["tasks"][1]["status"] == "failed"
    assert result["tasks"][0]["values"]["source_poll_rate"] == 7.0
    assert result["tasks"][0]["worker"] == "connect-cdc-pool"

    connector_selector = f'connector="{pipeline["connector_name"]}"'
    assert fake.queries
    assert all(connector_selector in q for q in fake.queries)


@pytest.mark.asyncio
async def test_sink_connector_queries_lag_and_dlq(fake_postgres, prometheus) -> None:
    pipeline = _seed(fake_postgres, "org_pm_sink", direction="SINK", runtime_pool_id="connect-sink-pool",
                     connector_name="crm--salesforce--accounts--sink--v1")
    fake = prometheus()

    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_sink", pipeline["connector_name"])

    assert result["direction"] == "sink"
    ids = {m["id"] for m in result["metrics"]}
    assert {"sink_read_rate", "sink_send_rate", "sink_consumer_lag", "dlq_produced"} <= ids
    assert not any("records_lag_max" in q for q in fake.queries)          # lag is not Connect's fetch-time JMX value any more…
    assert not any("source_task" in q for q in fake.queries)


@pytest.mark.asyncio
async def test_the_sink_consumer_lag_is_the_real_backlog_from_kafka_and_has_no_chart(fake_postgres, prometheus, monkeypatch) -> None:
    """…it is the backlog the consumer group has not committed (end offset − committed), read from Kafka, which is what the Pipelines page
    shows too. The monitoring test run saw 250 records waiting while this tile said 0."""
    from app.services import runtime_metrics

    pipeline = _seed(fake_postgres, "org_pm_lag", direction="SINK", runtime_pool_id="connect-sink-pool",
                     connector_name="crm--salesforce--accounts--sink--v1")
    prometheus()

    async def _backlogs(org_id):
        return {pipeline["connector_name"]: 250}

    monkeypatch.setattr(runtime_metrics, "sink_backlogs", _backlogs)
    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_lag", pipeline["connector_name"])

    lag = next(m for m in result["metrics"] if m["id"] == "sink_consumer_lag")
    assert lag["current"] == 250.0
    assert lag["history"] is False                        # no history, by design — not "no samples yet"
    assert lag["series"] == []
    assert all(m["history"] for m in result["metrics"] if m["id"] != "sink_consumer_lag")
    assert "sink_consumer_lag" in {c["id"] for c in result["task_columns"]}       # still a task-table column


@pytest.mark.asyncio
async def test_an_unreadable_backlog_is_unknown_never_zero(fake_postgres, prometheus, monkeypatch) -> None:
    from app.services import runtime_metrics

    pipeline = _seed(fake_postgres, "org_pm_lag2", direction="SINK", runtime_pool_id="connect-sink-pool",
                     connector_name="crm--salesforce--accounts--sink--v1")
    prometheus()

    async def _kafka_down(org_id):
        return None

    monkeypatch.setattr(runtime_metrics, "sink_backlogs", _kafka_down)
    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_lag2", pipeline["connector_name"])

    assert next(m for m in result["metrics"] if m["id"] == "sink_consumer_lag")["current"] is None


@pytest.mark.asyncio
async def test_prometheus_failure_is_reported_and_not_cached(fake_postgres, prometheus) -> None:
    pipeline = _seed(fake_postgres, "org_pm_fail")
    prometheus(fail=True)
    service = ConnectorPrometheusMetrics()

    result = await service.connector_metrics("org_pm_fail", pipeline["connector_name"])

    assert result["prometheus_enabled"] is True
    assert result["reachable"] is False
    assert "tsdb not ready" in result["error"]
    assert all(m["current"] is None and m["series"] == [] for m in result["metrics"])
    assert service._cache == {}


@pytest.mark.asyncio
async def test_results_are_cached_per_connector_and_range(fake_postgres, prometheus) -> None:
    pipeline = _seed(fake_postgres, "org_pm_cache")
    fake = prometheus()
    service = ConnectorPrometheusMetrics()

    await service.connector_metrics("org_pm_cache", pipeline["connector_name"], "1h")
    sent = len(fake.queries)
    await service.connector_metrics("org_pm_cache", pipeline["connector_name"], "1h")
    assert len(fake.queries) == sent
    await service.connector_metrics("org_pm_cache", pipeline["connector_name"], "6h")
    assert len(fake.queries) > sent


# ── Route ───────────────────────────────────────────────────────────────────

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
BASE = "/api/v1/dataconnect/runtime/connectors"


def test_route_returns_metrics_for_an_owned_connector(fake_postgres, monkeypatch) -> None:
    pipeline = _seed(fake_postgres, "org_pm_route")
    monkeypatch.setattr(settings, "prometheus_url", "")

    resp = client.get(f"{BASE}/{pipeline['connector_name']}/metrics?range=6h", headers={"X-Tenant-Id": "org_pm_route"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] == "6h"
    assert body["prometheus_enabled"] is False


def test_route_404s_another_orgs_connector(fake_postgres) -> None:
    pipeline = _seed(fake_postgres, "org_pm_route_owner")

    resp = client.get(f"{BASE}/{pipeline['connector_name']}/metrics", headers={"X-Tenant-Id": "org_pm_route_other"})

    assert resp.status_code == 404


def test_route_rejects_an_unknown_range(fake_postgres) -> None:
    pipeline = _seed(fake_postgres, "org_pm_route_range")

    resp = client.get(f"{BASE}/{pipeline['connector_name']}/metrics?range=7d", headers={"X-Tenant-Id": "org_pm_route_range"})

    assert resp.status_code == 422


# ── GET /runtime/connectors/metrics/summary — every owned connector's current rate/lag/tasks in one batch ────────────

class _SummaryPrometheus:
    """Answers the grouped `by (connector)` instant queries. Includes ANOTHER org's connector in every answer."""

    def __init__(self, fail: bool = False):
        self.queries: list[str] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)["query"][0]
        self.queries.append(query)
        if self.fail:
            return httpx.Response(503, json={"status": "error", "errorType": "unavailable", "error": "tsdb not ready"})

        def vec(rows):
            return httpx.Response(200, json=_vector([({"connector": c}, v) for c, v in rows]))

        if "source_record_write_rate" in query:
            return vec([("org_sum--src", "12.5"), ("another_org--src", "999")])
        if "sink_record_send_rate" in query:
            return vec([("org_sum--snk", "40")])
        if "records_lag_max" in query:
            return vec([("org_sum--snk", "310"), ("another_org--src", "5")])
        if 'status="running"' in query:
            return vec([("org_sum--src", "2"), ("org_sum--snk", "1")])
        if 'status="failed"' in query:
            return vec([("org_sum--snk", "1")])
        if query.startswith("count by (connector) (kafka_connect_task_status)"):
            return vec([("org_sum--src", "2"), ("org_sum--snk", "2"), ("another_org--src", "1")])
        if "task_error_total_record_failures" in query:
            return vec([("org_sum--snk", "22.5"), ("another_org--src", "7")])   # the sink is skipping records; the source isn't
        return vec([])


@pytest.fixture
def summary_prometheus(monkeypatch):
    def install(fail: bool = False) -> _SummaryPrometheus:
        fake = _SummaryPrometheus(fail=fail)
        monkeypatch.setattr(settings, "prometheus_url", "http://prometheus:9090")

        async def _client(base_url: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(fake.handler))

        monkeypatch.setattr(module, "get_client", _client)
        return fake

    return install


def _seed_pair(fake_postgres, org_id="org_sum"):
    _seed(fake_postgres, org_id, name="Src", connector_name="org_sum--src", direction="SOURCE")
    _seed(fake_postgres, org_id, name="Snk", connector_name="org_sum--snk", direction="SINK",
          topic_name="org_sum.x", entity="other")
    # a draft that was never deployed has no connector and must not show up
    _seed(fake_postgres, org_id, name="Draft", connector_name=None, entity="draft")


@pytest.mark.asyncio
async def test_summary_returns_each_owned_connectors_rate_lag_and_tasks(fake_postgres, summary_prometheus, monkeypatch) -> None:
    from app.services import runtime_metrics

    _seed_pair(fake_postgres)
    summary_prometheus()

    async def _backlogs(org_id):
        return {"org_sum--snk": 310}                     # the sink's real backlog, from Kafka committed offsets

    monkeypatch.setattr(runtime_metrics, "sink_backlogs", _backlogs)

    out = await ConnectorPrometheusMetrics().summary("org_sum")

    assert out["prometheus_enabled"] is True
    assert out["reachable"] is True
    assert out["error"] is None
    rows = {r["connector_name"]: r for r in out["connectors"]}
    assert set(rows) == {"org_sum--src", "org_sum--snk"}                    # no draft, no other org's connector
    assert rows["org_sum--src"]["rate"] == 12.5
    assert rows["org_sum--src"]["lag"] is None
    assert rows["org_sum--src"]["tasks_running"] == 2
    assert rows["org_sum--src"]["tasks_failed"] == 0
    assert rows["org_sum--snk"]["rate"] == 40.0
    assert rows["org_sum--snk"]["lag"] == 310.0
    assert rows["org_sum--snk"]["tasks_running"] == 1
    assert rows["org_sum--snk"]["tasks_failed"] == 1
    assert rows["org_sum--snk"]["direction"] == "sink"
    assert rows["org_sum--src"]["direction"] == "source"
    assert rows["org_sum--snk"]["error_rate"] == 22.5                     # failing records, though every task reports RUNNING
    assert rows["org_sum--src"]["error_rate"] == 0.0                      # samples, and no failures: 0, not None


@pytest.mark.asyncio
async def test_summary_never_turns_missing_samples_into_zero(fake_postgres, summary_prometheus) -> None:
    _seed(fake_postgres, "org_sum2", name="Brand new", connector_name="org_sum2--new", direction="SOURCE")
    summary_prometheus()

    out = await ConnectorPrometheusMetrics().summary("org_sum2")

    row = out["connectors"][0]
    assert out["reachable"] is True
    assert row["rate"] is None  # not 0
    assert row["tasks_running"] is None
    assert row["tasks_failed"] is None
    assert row["error_rate"] is None


@pytest.mark.asyncio
async def test_summary_when_prometheus_is_not_configured(fake_postgres, monkeypatch) -> None:
    _seed_pair(fake_postgres)
    monkeypatch.setattr(settings, "prometheus_url", "")

    out = await ConnectorPrometheusMetrics().summary("org_sum")

    assert out["prometheus_enabled"] is False
    assert out["reachable"] is False
    assert out["error"]
    assert {r["connector_name"] for r in out["connectors"]} == {"org_sum--src", "org_sum--snk"}
    assert all(r["rate"] is None and r["tasks_running"] is None for r in out["connectors"])


@pytest.mark.asyncio
async def test_summary_failure_is_reported_and_not_cached(fake_postgres, summary_prometheus, monkeypatch) -> None:
    _seed_pair(fake_postgres)
    fake = summary_prometheus(fail=True)
    service = ConnectorPrometheusMetrics()

    failed = await service.summary("org_sum")
    assert failed["reachable"] is False
    assert "tsdb not ready" in failed["error"]
    assert all(r["rate"] is None for r in failed["connectors"])

    fake.fail = False
    ok = await service.summary("org_sum")
    assert ok["reachable"] is True                                              # retried straight away, not served from cache


@pytest.mark.asyncio
async def test_summary_is_one_batch_and_cached_within_the_ttl(fake_postgres, summary_prometheus, monkeypatch) -> None:
    _seed_pair(fake_postgres)
    fake = summary_prometheus()
    monkeypatch.setattr(settings, "prometheus_metrics_cache_ttl_seconds", 60.0)
    service = ConnectorPrometheusMetrics()

    await service.summary("org_sum")
    first = len(fake.queries)
    await service.summary("org_sum")

    assert first == 6                      # a fixed number of queries however many connectors there are (lag comes from Kafka)
    assert len(fake.queries) == first      # second poll answered from the cache


def test_summary_route_answers_for_the_calling_org(fake_postgres, monkeypatch) -> None:
    _seed_pair(fake_postgres, "org_sum_route")
    monkeypatch.setattr(settings, "prometheus_url", "")

    resp = client.get(f"{BASE}/metrics/summary", headers={"X-Tenant-Id": "org_sum_route"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["prometheus_enabled"] is False
    assert {r["connector_name"] for r in body["connectors"]} == {"org_sum--src", "org_sum--snk"}


def test_summary_route_shows_nothing_of_another_org(fake_postgres, monkeypatch) -> None:
    _seed_pair(fake_postgres, "org_sum_owner")
    monkeypatch.setattr(settings, "prometheus_url", "")

    resp = client.get(f"{BASE}/metrics/summary", headers={"X-Tenant-Id": "org_sum_stranger"})

    assert resp.status_code == 200
    assert resp.json()["connectors"] == []


# ── The monitoring test run's findings #4 and #7 ────────────────────────────

def test_record_errors_count_failed_records_including_failed_writes() -> None:
    """Connect's total-record-errors stays 0 for records a sink reports as failed during put; total-record-failures counts them. The run had 21
    failed writes and the tile said 0/s throughout."""
    query = next(m for m in METRICS if m.id == "record_error_rate").query
    assert "kafka_connect_task_error_total_record_failures" in query
    assert "total_record_errors" not in query
    assert query.startswith("sum(rate(")


@pytest.mark.asyncio
@pytest.mark.parametrize(("range_key", "step"), [("1h", 60), ("6h", 300), ("24h", 900)])
async def test_wide_steps_summarise_each_bucket_instead_of_sampling_one_instant(fake_postgres, prometheus, range_key, step) -> None:
    """At a 5- or 15-minute step a one-minute bulk read fell between points and the 6h/24h charts showed 0 for 1150 records."""
    pipeline = _seed(fake_postgres, f"org_pm_b{step}")
    fake = prometheus()

    await ConnectorPrometheusMetrics().connector_metrics(f"org_pm_b{step}", pipeline["connector_name"], range_key)

    ranged = [q for q in fake.queries if "_over_time((" in q]
    suffix = f")[{step}s:15s])"
    assert any(q.startswith("avg_over_time((sum(kafka_connect_source_task_source_record_poll_rate") and q.endswith(suffix) for q in ranged)
    assert any(q.startswith("max_over_time((sum(kafka_connect_task_error_total_record_failures") for q in ranged)   # a cumulative count: its peak
    assert any(q.startswith("min_over_time((count(kafka_connect_task_status") for q in ranged)                     # a task that stopped still shows
    assert any(q.startswith("max_over_time((max(kafka_connect_source_task_poll_batch_avg_time_ms") for q in ranged)


@pytest.mark.asyncio
async def test_on_a_wide_step_the_tile_still_shows_the_latest_reading_not_a_bucket_average(fake_postgres, prometheus) -> None:
    pipeline = _seed(fake_postgres, "org_pm_cur")
    fake = prometheus()

    result = await ConnectorPrometheusMetrics().connector_metrics("org_pm_cur", pipeline["connector_name"], "6h")

    poll = next(m for m in result["metrics"] if m["id"] == "source_poll_rate")
    assert poll["current"] == 7.0                                  # from the instant query …
    assert [p["v"] for p in poll["series"]] == [10.0, 12.5]        # … while the chart keeps the bucketed series
    assert any(q.startswith("sum(kafka_connect_source_task_source_record_poll_rate") for q in fake.queries)


@pytest.mark.asyncio
async def test_the_finest_range_is_not_bucketed(fake_postgres, prometheus) -> None:
    """15m steps at the scrape interval: every scrape is already a point."""
    pipeline = _seed(fake_postgres, "org_pm_fine")
    fake = prometheus()

    await ConnectorPrometheusMetrics().connector_metrics("org_pm_fine", pipeline["connector_name"], "15m")

    assert not any("_over_time" in q for q in fake.queries)
