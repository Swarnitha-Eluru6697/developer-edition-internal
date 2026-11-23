import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.services import runtime_metrics

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
ORG_ID = "test_org"
ORG_HEADER = {"X-Tenant-Id": ORG_ID}


def _seed_pipeline(fake_postgres, org_id: str, **overrides) -> dict:
    pipeline = {
        "org_id": org_id,
        "connector_name": "crm--salesforce--accounts--cdc--v1",
        "topic_name": f"{org_id}.raw.crm.salesforce.accounts.cdc.v1",
        "domain": "crm",
        "source_system": "salesforce",
        "entity": "accounts",
        "version": "v1",
    }
    pipeline.update(overrides)
    return fake_postgres.create("pipelines", pipeline)


def _expanded(kind, state="RUNNING", config=None):
    """One entry of Connect's GET /connectors?expand=status&expand=info."""
    return {"status": {"type": kind, "connector": {"state": state}, "tasks": [{"state": state}]},
            "info": {"type": kind, "config": config or {}}}


@pytest.mark.asyncio
async def test_flow_summary_excludes_connectors_and_topics_this_org_does_not_own(monkeypatch, fake_postgres) -> None:
    mine = _seed_pipeline(fake_postgres, "org_flow_test")

    async def _fake_connect_get(path, base_url=None):
        assert path == runtime_metrics._BULK_PATH, "one expanded listing, never a call per connector"
        return {n: _expanded("source") for n in
                (mine["connector_name"], "some-other-orgs-connector", "some-other-apps-connector")}

    def _fake_kafka_metrics(bootstrap, sinks):
        return {
            "topics": {
                mine["topic_name"]: 42,
                "some_other_org.raw.crm.salesforce.accounts.cdc.v1": 99,
            },
            "sinks": {},
        }

    monkeypatch.setattr(runtime_metrics, "_connect_get", _fake_connect_get)
    monkeypatch.setattr(runtime_metrics, "_kafka_metrics", _fake_kafka_metrics)

    result = await runtime_metrics.flow_summary("org_flow_test")

    assert result["reachable"] is True
    connector_names = {c["name"] for c in result["connectors"]}
    assert connector_names == {mine["connector_name"]}
    assert result["connectors"][0]["pipeline_id"] == mine["id"]

    topic_names = {t["topic"] for t in result["topics"]}
    assert topic_names == {mine["topic_name"]}
    assert result["topics"][0]["pipeline_id"] == mine["id"]


# ── FE-BUG-16: GET /runtime/flow now has a real response_model (RuntimeFlow)
# instead of an untyped dict — these prove the route actually serializes a
# realistic reachable AND unreachable payload correctly, not just that the
# service function's own dict looks right in isolation. ──────────────────

@pytest.mark.asyncio
async def test_runtime_flow_route_serializes_a_reachable_response(monkeypatch, fake_postgres) -> None:
    mine = _seed_pipeline(fake_postgres, ORG_ID)

    async def _fake_connect_get(path, base_url=None):
        return {mine["connector_name"]: _expanded("source")}

    def _fake_kafka_metrics(bootstrap, sinks):
        return {"topics": {mine["topic_name"]: 7}, "sinks": {}}

    monkeypatch.setattr(runtime_metrics, "_connect_get", _fake_connect_get)
    monkeypatch.setattr(runtime_metrics, "_kafka_metrics", _fake_kafka_metrics)

    resp = client.get("/api/v1/dataconnect/runtime/flow", headers=ORG_HEADER)

    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["connectors"][0]["name"] == mine["connector_name"]
    assert body["connectors"][0]["state"] == "RUNNING"
    assert body["topics"][0]["messages"] == 7


@pytest.mark.asyncio
async def test_runtime_flow_route_is_honest_about_an_unreachable_connect(monkeypatch, fake_postgres) -> None:
    """FE-BUG-16: this is the backend's own half of "never substitute cached
    healthy state for unknown" — when Connect can't be reached, the route
    still returns 200 (this is a successful probe that got a negative
    answer, not a DataConnect-side failure), with reachable=False and no
    leftover connector data for the frontend to mistake for a real reading."""

    async def _fake_connect_get(path, base_url=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(runtime_metrics, "_connect_get", _fake_connect_get)

    resp = client.get("/api/v1/dataconnect/runtime/flow", headers=ORG_HEADER)

    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is False
    assert body["connectors"] == []
    assert "not reachable" in body["message"]


@pytest.mark.asyncio
async def test_flow_summary_reads_status_and_config_from_the_expanded_listing(monkeypatch, fake_postgres) -> None:
    """The sink's topic list and the source's prefix come out of the same single call as the state."""
    sink = _seed_pipeline(fake_postgres, "org_flow_test", connector_name="c-sink", topic_name="t-in")
    calls: list[str] = []

    async def _fake_connect_get(path, base_url=None):
        calls.append(path)
        return {"c-sink": _expanded("sink", state="FAILED", config={"topics": "t-in, t-other"})}

    monkeypatch.setattr(runtime_metrics, "_connect_get", _fake_connect_get)
    monkeypatch.setattr(runtime_metrics, "_kafka_metrics", lambda bootstrap, sinks: {"topics": {}, "sinks": {}})

    result = await runtime_metrics.flow_summary("org_flow_test")

    assert calls == [runtime_metrics._BULK_PATH]
    c = result["connectors"][0]
    assert (c["name"], c["state"], c["type"], c["pipeline_id"]) == ("c-sink", "FAILED", "sink", sink["id"])
    assert c["topics"] == ["t-in", "t-other"]


@pytest.mark.asyncio
async def test_flow_summary_falls_back_to_per_connector_reads_on_a_connect_without_expand(monkeypatch, fake_postgres) -> None:
    """Connect older than 2.3 ignores expand= and answers with bare names."""
    mine = _seed_pipeline(fake_postgres, "org_flow_test")

    async def _fake_connect_get(path, base_url=None):
        if path == runtime_metrics._BULK_PATH:
            return [mine["connector_name"], "someone-elses"]
        if path.endswith("/status"):
            return {"type": "source", "connector": {"state": "RUNNING"}, "tasks": []}
        return {"config": {"topic.prefix": "pfx"}}

    monkeypatch.setattr(runtime_metrics, "_connect_get", _fake_connect_get)
    monkeypatch.setattr(runtime_metrics, "_kafka_metrics", lambda bootstrap, sinks: {"topics": {}, "sinks": {}})

    result = await runtime_metrics.flow_summary("org_flow_test")

    assert [c["name"] for c in result["connectors"]] == [mine["connector_name"]]
    assert result["connectors"][0]["state"] == "RUNNING"
