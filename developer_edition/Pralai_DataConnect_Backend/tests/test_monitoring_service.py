from datetime import UTC, datetime, timedelta

import pytest

import app.services.monitoring_service as monitoring_service_module
from app.services.monitoring_service import MonitoringService


def test_aggregate_connector_statuses() -> None:
    summary = MonitoringService().aggregate_connector_statuses(
        [
            {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]},
            {"connector": {"state": "FAILED"}, "tasks": [{"state": "FAILED"}]},
        ]
    )

    assert summary == {
        "connector_running_count": 1,
        "failed_connector_count": 1,
        "failed_task_count": 1,
    }


def _seed_pipeline(fake_postgres, org_id: str, **overrides) -> dict:
    pipeline = {
        "org_id": org_id,
        "connector_name": "crm--salesforce--accounts--cdc--v1",
        "topic_name": f"{org_id}.raw.crm.salesforce.accounts.cdc.v1",
        "domain": "crm",
        "source_system": "salesforce",
        "entity": "accounts",
        "version": "v1",
        "runtime_pool_id": "connect-cdc-pool",
        # Only a SINK with a dead-letter policy writes a DLQ topic (app/core/error_policies.py).
        "direction": "SINK",
        "error_handling_policy": "DLQ_AND_CONTINUE",
    }
    pipeline.update(overrides)
    return fake_postgres.create("pipelines", pipeline)


def _fake_status(connector_name: str, *, state: str = "RUNNING") -> dict:
    return {
        "name": connector_name,
        "connector": {"state": state},
        "tasks": [],
        "observed_state": "running" if state == "RUNNING" else "failed",
        "observed_at": "2026-01-01T00:00:00Z",
        "runtime_endpoint": "http://connect",
        "error": None,
    }


@pytest.mark.asyncio
async def test_connector_status_returns_none_for_a_connector_this_org_does_not_own(monkeypatch, fake_postgres) -> None:
    _seed_pipeline(fake_postgres, "org_mon_status_none")
    service = MonitoringService()

    result = await service.connector_status("org_mon_status_none", "some-other-apps-connector")

    assert result is None


@pytest.mark.asyncio
async def test_connector_status_includes_pipeline_id_and_topic_name(monkeypatch, fake_postgres) -> None:
    pipeline = _seed_pipeline(fake_postgres, "org_mon_status_found")
    service = MonitoringService()

    async def _fake_fetch_status(name, pool_id=None):
        return _fake_status(name)

    monkeypatch.setattr(service, "_fetch_status", _fake_fetch_status)

    result = await service.connector_status("org_mon_status_found", pipeline["connector_name"])

    assert result["pipeline_id"] == pipeline["id"]
    assert result["topic_name"] == pipeline["topic_name"]


@pytest.mark.asyncio
async def test_connector_tasks_returns_none_for_a_connector_this_org_does_not_own(fake_postgres) -> None:
    _seed_pipeline(fake_postgres, "org_mon_tasks_none")
    service = MonitoringService()

    result = await service.connector_tasks("org_mon_tasks_none", "some-other-apps-connector")

    assert result is None


@pytest.mark.asyncio
async def test_metrics_summary_only_aggregates_this_orgs_own_connectors(monkeypatch, fake_postgres) -> None:
    mine = _seed_pipeline(fake_postgres, "org_mon_metrics")
    service = MonitoringService()

    async def _fake_list_connectors(pool_id=None):
        # Connect reports this org's connector AND one belonging to some
        # other org/application sharing the cluster.
        return [mine["connector_name"], "some-other-orgs-connector"], None

    async def _fake_fetch_status(connector_name, pool_id=None):
        return _fake_status(connector_name)

    monkeypatch.setattr(service, "_list_connectors", _fake_list_connectors)
    monkeypatch.setattr(service, "_fetch_status", _fake_fetch_status)

    summary = await service.metrics_summary("org_mon_metrics")

    assert summary["connector_running_count"] == 1
    assert "some-other-orgs-connector" not in summary["extra"].get("errors", [])


@pytest.mark.asyncio
async def test_list_cluster_connectors_only_returns_this_orgs_pipelines(monkeypatch, fake_postgres) -> None:
    mine = _seed_pipeline(fake_postgres, "org_mon_cluster")
    _seed_pipeline(fake_postgres, "org_mon_cluster_other")
    service = MonitoringService()

    async def _fake_fetch_status(name, pool_id=None):
        return _fake_status(name)

    monkeypatch.setattr(service, "_fetch_status", _fake_fetch_status)

    result = await service.list_cluster_connectors("org_mon_cluster", "connect-cdc-pool")

    assert len(result) == 1
    assert result[0]["pipeline_id"] == mine["id"]
    assert result[0]["topic_name"] == mine["topic_name"]


# ── FE-BUG-17: validation_failure_count / ingestion_failure_count ──────────

def _count_row(org_id: str, key: str, minutes_ago: float, count: int, *, kind: str = "validation", pipeline_id: str = "pipe-1") -> dict:
    return {
        "org_id": org_id, "pipeline_id": pipeline_id, "kind": kind, "key": key, "count": count,
        "bucket_start": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0),
    }


@pytest.mark.asyncio
async def test_validation_failure_count_only_counts_buckets_within_the_window(fake_postgres) -> None:
    fake_postgres.create("failure_counts", _count_row("org_val_1", "*", 2, 40))
    fake_postgres.create("failure_counts", _count_row("org_val_1", "*", 5, 2))
    fake_postgres.create("failure_counts", _count_row("org_val_1", "*", 90, 999))  # outside a 1-hour window
    service = MonitoringService()

    count = await service._validation_failure_count("org_val_1", window_seconds=3600)

    # Exact however many records failed — no longer capped by a 50-record sample.
    assert count == 42


@pytest.mark.asyncio
async def test_validation_failure_count_is_org_scoped(fake_postgres) -> None:
    fake_postgres.create("failure_counts", _count_row("org_val_other", "*", 1, 7))
    service = MonitoringService()

    count = await service._validation_failure_count("org_val_mine", window_seconds=3600)

    assert count == 0


@pytest.mark.asyncio
async def test_ingestion_failure_count_sums_connector_dlq_depth_across_pipelines(monkeypatch, fake_postgres) -> None:
    _seed_pipeline(fake_postgres, "org_ing_1")
    _seed_pipeline(
        fake_postgres, "org_ing_1",
        connector_name="crm--salesforce--orders--cdc--v1",
        topic_name="raw-crm-salesforce-orders-cdc-v1", entity="orders",
    )
    service = MonitoringService()

    async def _fake_count(topic, window_seconds):
        return 5 if "accounts" in topic else 2

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)

    total = await service._ingestion_failure_count("org_ing_1", window_seconds=3600)

    assert total == 7


@pytest.mark.asyncio
async def test_ingestion_failure_count_skips_a_never_deployed_pipeline(monkeypatch, fake_postgres) -> None:
    """No connector_name means no connector was ever created — there's no
    DLQ topic to even check, not zero errors on one that doesn't exist."""
    _seed_pipeline(fake_postgres, "org_ing_2", connector_name=None)
    service = MonitoringService()

    called = False

    async def _fake_count(topic, window_seconds):
        nonlocal called
        called = True
        return 99

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)

    total = await service._ingestion_failure_count("org_ing_2", window_seconds=3600)

    assert total == 0
    assert called is False


@pytest.mark.asyncio
async def test_metrics_summary_exposes_validation_and_ingestion_failures_independently(monkeypatch, fake_postgres) -> None:
    """The exact FE-BUG-17 fix: two real, independently-sourced numbers —
    never one undifferentiated "quarantine" count mixing a DataConnect
    quality-rule failure with a Kafka Connect DLQ record."""
    mine = _seed_pipeline(fake_postgres, "org_mon_split")
    fake_postgres.create("failure_counts", _count_row("org_mon_split", "*", 3, 2))
    service = MonitoringService()

    async def _fake_list_connectors(pool_id=None):
        return [mine["connector_name"]], None

    async def _fake_fetch_status(connector_name, pool_id=None):
        return _fake_status(connector_name)

    async def _fake_count(topic, window_seconds):
        return 4

    monkeypatch.setattr(service, "_list_connectors", _fake_list_connectors)
    monkeypatch.setattr(service, "_fetch_status", _fake_fetch_status)
    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)

    summary = await service.metrics_summary("org_mon_split")

    assert summary["validation_failure_count"] == 2
    assert summary["ingestion_failure_count"] == 4
    assert "quarantine_count" not in summary



# ── FE-BUG-17: failure_breakdown (by rule / by cause) ───────────────────────

@pytest.mark.asyncio
async def test_validation_breakdown_groups_windowed_failures_by_rule(fake_postgres) -> None:
    # Three failing records: customer_id x2, email x2 (one record broke both rules).
    fake_postgres.create("failure_counts", _count_row("org_bd_1", "*", 2, 2))
    fake_postgres.create("failure_counts", _count_row("org_bd_1", "*", 3, 1))
    fake_postgres.create("failure_counts", _count_row("org_bd_1", "not_null:customer_id", 2, 2))
    fake_postgres.create("failure_counts", _count_row("org_bd_1", "regex:email", 3, 1))
    fake_postgres.create("failure_counts", _count_row("org_bd_1", "regex:email", 4, 1))
    fake_postgres.create("failure_counts", _count_row("org_bd_1", "accepted_values:status", 120, 5))  # outside the 1h window

    out = await MonitoringService()._validation_breakdown("org_bd_1", window_seconds=3600)

    assert out["total"] == 3
    assert {r["rule"]: r["count"] for r in out["by_rule"]} == {"not_null:customer_id": 2, "regex:email": 2}


@pytest.mark.asyncio
async def test_validation_breakdown_is_exact_beyond_the_old_sample_cap(fake_postgres) -> None:
    """The point of the counters: 5,000 failing records in a minute are 5,000, not the newest 50."""
    fake_postgres.create("failure_counts", _count_row("org_bd_big", "*", 1, 5000))
    fake_postgres.create("failure_counts", _count_row("org_bd_big", "range:amount", 1, 5000))

    out = await MonitoringService()._validation_breakdown("org_bd_big", window_seconds=3600)

    assert out["total"] == 5000
    assert out["by_rule"] == [{"rule": "range:amount", "count": 5000}]


@pytest.mark.asyncio
async def test_ingestion_breakdown_uses_exact_cause_counters_and_scrubs_recent_messages(monkeypatch, fake_postgres) -> None:
    _seed_pipeline(fake_postgres, "org_bd_2")
    fake_postgres.create("failure_counts", _count_row("org_bd_2", "serialization", 3, 40_000, kind="ingestion"))
    fake_postgres.create("failure_counts", _count_row("org_bd_2", "connector", 4, 10_000, kind="ingestion"))
    service = MonitoringService()

    async def _fake_count(topic, window_seconds):
        return 50_000

    async def _fake_read(topic, window_seconds, limit=200):
        return [
            {"timestamp_ms": 1_700_000_000_000, "headers": {
                "__connect.errors.exception.class.name": "org.apache.kafka.common.errors.SerializationException",
                "__connect.errors.exception.message": "Unknown magic byte!",
                "__connect.errors.stage": "VALUE_CONVERTER"}},
            {"timestamp_ms": 1_700_000_001_000, "headers": {
                "__connect.errors.exception.class.name": "java.sql.SQLException",
                "__connect.errors.exception.message": "cannot reach postgres://user:hunter2@db:5432/x",
                "__connect.errors.stage": "TASK_PUT"}},
        ]

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(monitoring_service_module, "read_recent_connector_errors", _fake_read)

    out = await service._ingestion_breakdown("org_bd_2", window_seconds=3600)

    assert out["total"] == 50_000
    assert out["classified"] == 50_000  # exact, not "up to 200 per pipeline"
    assert out["unclassified"] == 0
    assert {c["category"]: c["count"] for c in out["by_category"]} == {"serialization": 40_000, "connector": 10_000}
    assert all("hunter2" not in (r["message"] or "") for r in out["recent"])
    assert out["recent"][0]["category"] == "connector"  # newest first


@pytest.mark.asyncio
async def test_ingestion_breakdown_reports_how_far_the_counters_lag(monkeypatch, fake_postgres) -> None:
    _seed_pipeline(fake_postgres, "org_bd_lag")
    fake_postgres.create("failure_counts", _count_row("org_bd_lag", "serialization", 3, 30, kind="ingestion"))

    async def _fake_count(topic, window_seconds):
        return 100

    async def _no_recent(topic, window_seconds, limit=200):
        return []

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(monitoring_service_module, "read_recent_connector_errors", _no_recent)

    out = await MonitoringService()._ingestion_breakdown("org_bd_lag", window_seconds=3600)

    assert out["total"] == 100
    assert out["classified"] == 30
    assert out["unclassified"] == 70


@pytest.mark.asyncio
async def test_a_dlq_topic_shared_by_two_pipelines_is_counted_once(monkeypatch, fake_postgres) -> None:
    """A source and a sink with the same domain/source_system/entity/version share one DLQ topic."""
    _seed_pipeline(fake_postgres, "org_bd_shared")
    _seed_pipeline(fake_postgres, "org_bd_shared", connector_name="org_bd_shared--sink")
    seen = []

    async def _fake_count(topic, window_seconds):
        seen.append(topic)
        return 6

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)

    total = await MonitoringService()._ingestion_failure_count("org_bd_shared", window_seconds=3600)

    assert total == 6
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_ingestion_breakdown_skips_a_never_deployed_pipeline(monkeypatch, fake_postgres) -> None:
    _seed_pipeline(fake_postgres, "org_bd_3", connector_name=None)

    async def _boom(*a, **k):
        raise AssertionError("must not read a DLQ topic for a never-deployed pipeline")

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _boom)
    monkeypatch.setattr(monitoring_service_module, "read_recent_connector_errors", _boom)

    out = await MonitoringService()._ingestion_breakdown("org_bd_3", window_seconds=3600)

    assert out["total"] == 0
    assert out["by_category"] == []
    assert out["unclassified"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"direction": "SOURCE", "error_handling_policy": "SKIP_AND_LOG"},   # a source has no dead-letter queue
    {"direction": "SINK", "error_handling_policy": "SKIP_AND_LOG"},     # a sink that chose not to keep them
    {"direction": "SINK", "error_handling_policy": "FAIL_FAST"},
])
async def test_only_a_sink_with_a_dead_letter_policy_has_a_dlq_topic_to_count(monkeypatch, fake_postgres, overrides) -> None:
    _seed_pipeline(fake_postgres, "org_dlq_none", **overrides)
    called: list[str] = []

    async def _fake_count(topic, window_seconds):
        called.append(topic)
        return 99

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)

    total = await MonitoringService()._ingestion_failure_count("org_dlq_none", window_seconds=3600)

    assert total == 0
    assert called == []  # no topic exists to read, so none is read


# ── The dashboard/monitoring slowness: dozens of DLQ topics, read one after another and recomputed per request ──────

async def _seed_dlq_sinks(fake_postgres, org_id: str, n: int) -> list[str]:
    """n sink pipelines, each with its own DLQ topic; returns those topic names, in creation order."""
    for i in range(n):
        _seed_pipeline(fake_postgres, org_id, connector_name=f"sink{i}", entity=f"entity{i}")
    return list(await MonitoringService()._dlq_topics(org_id))


@pytest.mark.asyncio
async def test_dlq_topics_are_counted_concurrently_not_one_after_another(monkeypatch, fake_postgres) -> None:
    import asyncio

    topics = await _seed_dlq_sinks(fake_postgres, "org_dlq_par", 12)
    monkeypatch.setattr(monitoring_service_module.settings, "monitoring_dlq_read_concurrency", 4)
    running = peak = 0

    async def _fake_count(topic, window_seconds):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return 1

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)

    counts = await MonitoringService()._dlq_counts("org_dlq_par", 3600)

    assert set(counts) == set(topics)
    assert 1 < peak <= 4   # in parallel, but bounded — never an unbounded burst of Kafka consumers


@pytest.mark.asyncio
async def test_the_dlq_scan_is_shared_between_the_summary_and_the_breakdown_within_the_ttl(monkeypatch, fake_postgres) -> None:
    import asyncio

    await _seed_dlq_sinks(fake_postgres, "org_dlq_cache", 5)
    calls = 0

    async def _fake_count(topic, window_seconds):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return 0

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)
    service = MonitoringService()

    # Two tabs polling at the same instant, then the other endpoint right after: one scan in total.
    await asyncio.gather(service._ingestion_failure_count("org_dlq_cache", 3600), service._ingestion_failure_count("org_dlq_cache", 3600))
    await service._ingestion_breakdown("org_dlq_cache", 3600)

    assert calls == 5


@pytest.mark.asyncio
async def test_the_dlq_scan_is_redone_once_the_ttl_has_passed(monkeypatch, fake_postgres) -> None:
    await _seed_dlq_sinks(fake_postgres, "org_dlq_ttl", 2)
    calls = 0

    async def _fake_count(topic, window_seconds):
        nonlocal calls
        calls += 1
        return calls

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(monitoring_service_module.settings, "monitoring_dlq_cache_ttl_seconds", 0.0)
    service = MonitoringService()

    first = await service._ingestion_failure_count("org_dlq_ttl", 3600)
    second = await service._ingestion_failure_count("org_dlq_ttl", 3600)

    assert calls == 4
    assert first != second


@pytest.mark.asyncio
async def test_a_failed_dlq_scan_is_not_cached_as_zero(monkeypatch, fake_postgres) -> None:
    await _seed_dlq_sinks(fake_postgres, "org_dlq_fail", 2)
    outcomes = [ConnectionError("broker down"), 3, 3, 3]

    async def _fake_count(topic, window_seconds):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)
    service = MonitoringService()

    with pytest.raises(ConnectionError):
        await service._ingestion_failure_count("org_dlq_fail", 3600)

    assert await service._ingestion_failure_count("org_dlq_fail", 3600) == 6   # retried, not a cached failure or zero


@pytest.mark.asyncio
async def test_the_breakdown_reads_only_dlq_topics_that_hold_records(monkeypatch, fake_postgres) -> None:
    topics = await _seed_dlq_sinks(fake_postgres, "org_dlq_busy", 6)
    read: list[str] = []

    async def _fake_count(topic, window_seconds):
        return 4 if topic == topics[2] else 0

    async def _fake_read(topic, window_seconds, limit=200):
        read.append(topic)
        return []

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _fake_count)
    monkeypatch.setattr(monitoring_service_module, "read_recent_connector_errors", _fake_read)

    out = await MonitoringService()._ingestion_breakdown("org_dlq_busy", 3600)

    assert out["total"] == 4
    assert read == [topics[2]]   # five empty topics cost no read at all


# ── The Quality page's "Last 15m / 1h / 6h / 24h": failure_breakdown takes the window ──────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_breakdown_uses_the_window_it_is_given_and_defaults_to_the_configured_one(monkeypatch) -> None:
    from app.core.config import settings

    seen: list[float] = []

    async def _validation(self, org_id, window_seconds, pipeline_id=None):
        seen.append(window_seconds)
        return {"total": 0, "by_rule": []}

    async def _ingestion(self, org_id, window_seconds, pipeline_id=None):
        seen.append(window_seconds)
        return {"total": 0, "classified": 0, "unclassified": 0, "by_category": [], "recent": []}

    monkeypatch.setattr(MonitoringService, "_validation_breakdown", _validation)
    monkeypatch.setattr(MonitoringService, "_ingestion_breakdown", _ingestion)
    service = MonitoringService()

    assert (await service.failure_breakdown("org_w", None, 6 * 3600))["window_seconds"] == 6 * 3600
    assert seen == [6 * 3600, 6 * 3600]                                  # both groups count over the same chosen window
    seen.clear()
    assert (await service.failure_breakdown("org_w"))["window_seconds"] == int(settings.monitoring_failure_window_seconds)


def test_the_failures_endpoint_accepts_a_window_from_one_minute_to_a_day_and_nothing_else(monkeypatch) -> None:
    from conftest import DEFAULT_GATEWAY_HEADERS
    from fastapi.testclient import TestClient

    from app.main import app

    seen: list[int | None] = []

    async def _fake(org_id, pipeline_id=None, window_seconds=None):
        seen.append(window_seconds)
        return {"window_seconds": window_seconds or 3600, "validation": {"total": 0, "by_rule": []},
                "ingestion": {"total": 0, "classified": 0, "unclassified": 0, "by_category": [], "recent": []}}

    monkeypatch.setattr("app.api.routes.monitoring.monitoring_service.failure_breakdown", _fake)
    client = TestClient(app, headers={**DEFAULT_GATEWAY_HEADERS, "X-Tenant-Id": "org_w"})
    base = "/api/v1/dataconnect/runtime/metrics/failures"

    assert client.get(base).status_code == 200
    assert client.get(f"{base}?window_seconds=86400").status_code == 200
    assert client.get(f"{base}?window_seconds=900").json()["window_seconds"] == 900
    assert seen == [None, 86400, 900]
    for bad in ("59", "86401", "0", "-5", "abc"):
        assert client.get(f"{base}?window_seconds={bad}").status_code == 422, bad
