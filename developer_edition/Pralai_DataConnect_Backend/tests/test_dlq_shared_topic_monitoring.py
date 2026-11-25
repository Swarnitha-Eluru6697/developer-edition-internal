"""NEW-04 on the monitoring screen: the Quality page's recent connector failures must name the pipeline each record belongs to, and the failure
breakdown must be readable for one pipeline when a DLQ topic is shared by several.

Before: a shared topic mapped to its FIRST pipeline, so every record's pipeline name was that one's.
"""
from datetime import UTC, datetime, timedelta

import pytest

import app.services.monitoring_service as monitoring_service_module
from app.services.dlq_attribution import CONNECTOR_NAME_HEADER
from app.services.monitoring_service import MonitoringService

ORG = "org_shared"
CONN_A = "sales--crm--orders--batch--v1"
CONN_B = "sales--crm--orders--stream--v1"
STAGE = "__connect.errors.stage"
EXC = "__connect.errors.exception.class.name"
MSG = "__connect.errors.exception.message"


def _pipeline(fake_postgres, pid: str, name: str, connector: str, mode: str) -> dict:
    return fake_postgres.create("pipelines", {
        "id": pid, "org_id": ORG, "name": name, "connector_name": connector, "direction": "SINK", "error_handling_policy": "DLQ_AND_CONTINUE",
        "domain": "sales", "source_system": "crm", "entity": "orders", "version": "v1", "mode": mode, "runtime_pool_id": "connect-sink-pool",
    })


def _count(pipeline_id: str, key: str, n: int, *, kind: str = "ingestion") -> dict:
    return {"org_id": ORG, "pipeline_id": pipeline_id, "kind": kind, "key": key, "count": n,
            "bucket_start": (datetime.now(UTC) - timedelta(minutes=2)).replace(second=0, microsecond=0)}


def _dlq_record(connector: str | None, *, when: int, exception: str = "org.apache.kafka.connect.errors.DataException", stage: str = "VALUE_CONVERTER") -> dict:
    headers = {EXC: exception, MSG: "boom", STAGE: stage}
    if connector is not None:
        headers[CONNECTOR_NAME_HEADER] = connector
    return {"timestamp_ms": 1_700_000_000_000 + when, "headers": headers}


@pytest.fixture
def shared(fake_postgres, monkeypatch):
    """Two sink pipelines sharing one DLQ topic, with 3 serialization failures counted for A and 2 connector failures for B."""
    _pipeline(fake_postgres, "pipe-a", "Orders batch", CONN_A, "batch")
    _pipeline(fake_postgres, "pipe-b", "Orders stream", CONN_B, "stream")
    fake_postgres.create("failure_counts", _count("pipe-a", "serialization", 3))
    fake_postgres.create("failure_counts", _count("pipe-b", "connector", 2))

    async def _topic_total(topic, window_seconds):
        return 5

    async def _recent(topic, window_seconds, limit=200):
        return [
            _dlq_record(CONN_A, when=1),
            _dlq_record(CONN_B, when=2, exception="java.sql.SQLException", stage="TASK_PUT"),
            _dlq_record(CONN_A, when=3),
            _dlq_record(None, when=4),                                         # no connector header on a shared topic
            _dlq_record("gone--x--y--batch--v1", when=5),              # a deleted pipeline's leftover
        ]

    monkeypatch.setattr(monitoring_service_module, "count_recent_connector_errors", _topic_total)
    monkeypatch.setattr(monitoring_service_module, "read_recent_connector_errors", _recent)
    return fake_postgres


@pytest.mark.asyncio
async def test_each_recent_failure_names_the_pipeline_whose_connector_wrote_it(shared) -> None:
    out = await MonitoringService()._ingestion_breakdown(ORG, window_seconds=3600)

    named = {(r["pipeline_name"], r["category"]) for r in out["recent"]}
    assert ("Orders batch", "schema_conversion") in named          # what its own record's headers classify as
    assert ("Orders stream", "connector") in named
    # Before the fix every one of these read "Orders batch" — the first pipeline on the topic.
    assert sum(1 for r in out["recent"] if r["pipeline_name"] == "Orders batch") == 2
    assert sum(1 for r in out["recent"] if r["pipeline_name"] == "Orders stream") == 1


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_tied_to_a_pipeline_is_not_filed_under_one(shared) -> None:
    out = await MonitoringService()._ingestion_breakdown(ORG, window_seconds=3600)

    names = [r["pipeline_name"] for r in out["recent"]]
    assert "gone--x--y--batch--v1" in names                 # shown under the connector that wrote it
    assert "Unknown pipeline" in names                               # no header: nobody's, said plainly
    assert names.count("Orders batch") == 2
    assert names.count("Orders stream") == 1


@pytest.mark.asyncio
async def test_the_org_wide_numbers_are_unchanged(shared) -> None:
    out = await MonitoringService()._ingestion_breakdown(ORG, window_seconds=3600)

    assert out["total"] == 5
    assert {c["category"]: c["count"] for c in out["by_category"]} == {"serialization": 3, "connector": 2}


@pytest.mark.asyncio
async def test_one_pipelines_breakdown_has_only_its_own_counts_and_records(shared) -> None:
    service = MonitoringService()

    a = await service._ingestion_breakdown(ORG, window_seconds=3600, pipeline_id="pipe-a")
    b = await service._ingestion_breakdown(ORG, window_seconds=3600, pipeline_id="pipe-b")

    assert {c["category"]: c["count"] for c in a["by_category"]} == {"serialization": 3}
    assert {c["category"]: c["count"] for c in b["by_category"]} == {"connector": 2}
    assert (a["total"], b["total"]) == (3, 2)                        # the sum of ITS counters: a topic's offset total cannot be split
    assert {r["pipeline_name"] for r in a["recent"]} == {"Orders batch"}
    assert {r["pipeline_name"] for r in b["recent"]} == {"Orders stream"}


@pytest.mark.asyncio
async def test_a_pipeline_with_no_failures_gets_nothing_even_though_the_topic_has_some(shared) -> None:
    out = await MonitoringService()._ingestion_breakdown(ORG, window_seconds=3600, pipeline_id="pipe-nobody")

    assert out["total"] == 0
    assert out["by_category"] == []
    assert out["recent"] == []


@pytest.mark.asyncio
async def test_another_organizations_pipeline_id_shows_nothing(shared) -> None:
    out = await MonitoringService()._ingestion_breakdown("org_someone_else", window_seconds=3600, pipeline_id="pipe-a")

    assert out["total"] == 0
    assert out["by_category"] == []


@pytest.mark.asyncio
async def test_validation_failures_can_be_read_per_pipeline_too(fake_postgres) -> None:
    for pid, n in (("pipe-a", 7), ("pipe-b", 2)):
        fake_postgres.create("failure_counts", _count(pid, "*", n, kind="validation"))
        fake_postgres.create("failure_counts", _count(pid, "not_null:id", n, kind="validation"))
    service = MonitoringService()

    assert (await service._validation_breakdown(ORG, 3600))["total"] == 9
    assert (await service._validation_breakdown(ORG, 3600, "pipe-a"))["total"] == 7
    assert (await service._validation_breakdown(ORG, 3600, "pipe-b"))["by_rule"] == [{"rule": "not_null:id", "count": 2}]


@pytest.mark.asyncio
async def test_the_failures_endpoint_passes_the_pipeline_filter_through(monkeypatch) -> None:
    from conftest import DEFAULT_GATEWAY_HEADERS
    from fastapi.testclient import TestClient

    from app.main import app

    seen = []

    async def _fake(org_id, pipeline_id=None, window_seconds=None):
        seen.append((org_id, pipeline_id))
        return {"window_seconds": 3600, "validation": {"total": 0, "by_rule": []},
                "ingestion": {"total": 0, "classified": 0, "unclassified": 0, "by_category": [], "recent": []}}

    monkeypatch.setattr("app.api.routes.monitoring.monitoring_service.failure_breakdown", _fake)
    client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
    base = "/api/v1/dataconnect/runtime/metrics/failures"

    assert client.get(base, headers={"X-Tenant-Id": ORG}).status_code == 200
    assert client.get(f"{base}?pipeline_id=pipe-a", headers={"X-Tenant-Id": ORG}).status_code == 200

    assert seen == [(ORG, None), (ORG, "pipe-a")]


# ── Validation failures grouped by QUALITY CHECK (not by rule) ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validation_failures_are_listed_per_quality_check_each_record_once(fake_postgres) -> None:
    """A check can hold any number of rules; the Quality screen lists the CHECK. A record counts once however many rules it broke, and
    pipelines that share a check add up under its name."""
    pii = fake_postgres.create("quality_policies", {"org_id": ORG, "name": "customer-pii", "rules": []})
    amounts = fake_postgres.create("quality_policies", {"org_id": ORG, "name": "amount-limits", "rules": []})
    for pid, name, policy in (("pipe-1", "orders-a", pii["id"]), ("pipe-2", "orders-b", pii["id"]), ("pipe-3", "billing", amounts["id"]), ("pipe-4", "orphan", "gone")):
        fake_postgres.create("pipelines", {"id": pid, "org_id": ORG, "name": name, "quality_policy_id": policy, "connector_name": f"c-{pid}", "direction": "SOURCE"})
    # pipe-1: 5 failing records that broke 9 rule hits between them; pipe-2: 2 records; pipe-3: 1 record; pipe-4's check no longer exists.
    for pid, records, rule_hits in (("pipe-1", 5, 9), ("pipe-2", 2, 2), ("pipe-3", 1, 3), ("pipe-4", 4, 4)):
        fake_postgres.create("failure_counts", _count(pid, "*", records, kind="validation"))
        fake_postgres.create("failure_counts", _count(pid, "rule_x", rule_hits, kind="validation"))
    service = MonitoringService()

    out = await service._validation_breakdown(ORG, 3600)

    assert out["total"] == 12
    assert [(c["name"], c["count"]) for c in out["by_check"]] == [("customer-pii", 7), ("(removed quality check)", 4), ("amount-limits", 1)]
    assert sorted(out["by_check"][0]["pipelines"]) == ["orders-a", "orders-b"]
    assert out["by_check"][0]["quality_policy_id"] == pii["id"]
    assert out["by_rule"] == [{"rule": "rule_x", "count": 18}]                  # per-rule detail is still there for anyone who wants it

    one = await service._validation_breakdown(ORG, 3600, "pipe-3")              # narrowed to one pipeline
    assert [(c["name"], c["count"]) for c in one["by_check"]] == [("amount-limits", 1)]


@pytest.mark.asyncio
async def test_no_validation_failures_means_no_checks_listed(fake_postgres) -> None:
    out = await MonitoringService()._validation_breakdown(ORG, 3600)
    assert out["by_check"] == []
