"""
LineageService's actual idempotency guarantee (stable qualified names +
get-then-create against a real unique constraint, recovering from a lost
race via a SAVEPOINT — see the module docstring in app/services/lineage_service.py)
needs a live Postgres connection to exercise for real: even the "nothing
exists yet, create it" path opens a SAVEPOINT on the real session, which
isn't something a fake repository can stand in for. Consistent with every
other Postgres-backed service touched in this codebase (quality, schema
inference, schema drift), that path is verified by the DB-level unique
constraint (see alembic/versions/0011_lineage_edge_uniqueness.py and
Asset's own UniqueConstraint) plus code review, not a live-DB unit test.

What's tested here without a database:
  - the pure dict-conversion helpers
  - that every route awaits the (now-async) service correctly
"""

from datetime import UTC, datetime

import pytest
from conftest import DEFAULT_GATEWAY_HEADERS
from fastapi.testclient import TestClient

from app.main import app
from app.models.lineage import Asset, LineageEdge
from app.services.lineage_service import _asset_to_dict, _edge_to_dict, lineage_service

client = TestClient(app, headers=DEFAULT_GATEWAY_HEADERS)
HDR = {"X-Tenant-Id": "org-1"}


def _seed_chain(fake_postgres, org_id: str) -> dict[str, dict]:
    """source -CONNECTS_TO-> connector -INGESTS_TO-> raw_topic
    -VALIDATED_BY-> processor -EMITS_TO-> prepared_topic -WRITTEN_TO-> sink
    — the ticket's own default 5-node chain, plus one extra downstream hop
    (a sink) to actually exercise depth/direction bounding beyond it."""
    a = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "source", "name": "A", "qualified_name": f"{org_id}:a"})
    b = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "connector", "name": "B", "qualified_name": f"{org_id}:b"})
    c = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "kafka_topic", "name": "C", "qualified_name": f"{org_id}:c"})
    d = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "processor", "name": "D", "qualified_name": f"{org_id}:d"})
    e = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "kafka_topic", "name": "E", "qualified_name": f"{org_id}:e"})
    f = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "sink", "name": "F", "qualified_name": f"{org_id}:f"})

    def edge(frm: dict, to: dict, edge_type: str) -> None:
        fake_postgres.create("lineage_edges", {
            "org_id": org_id, "from_asset_id": frm["id"], "to_asset_id": to["id"],
            "edge_type": edge_type, "pipeline_id": "pipe-1",
        })

    edge(a, b, "CONNECTS_TO")
    edge(b, c, "INGESTS_TO")
    edge(c, d, "VALIDATED_BY")
    edge(d, e, "EMITS_TO")
    edge(e, f, "WRITTEN_TO")
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f}


def test_asset_to_dict_maps_metadata_json_column_to_metadata_key() -> None:
    asset = Asset(
        id="a1", org_id="org-1", asset_type="kafka_topic", name="t",
        qualified_name="org-1:topic:t", metadata_json={"zone": "raw"},
    )

    result = _asset_to_dict(asset)

    assert result == {
        "id": "a1", "org_id": "org-1", "asset_type": "kafka_topic",
        "name": "t", "qualified_name": "org-1:topic:t", "metadata": {"zone": "raw"},
    }


def test_edge_to_dict_maps_metadata_json_column_to_metadata_key() -> None:
    edge = LineageEdge(
        id="e1", org_id="org-1", from_asset_id="a1", to_asset_id="a2",
        edge_type="INGESTS_TO", pipeline_id="pipe_1", metadata_json={"connector_id": "c1"},
        created_at=datetime.now(UTC),
    )

    result = _edge_to_dict(edge)

    assert result == {
        "id": "e1", "org_id": "org-1", "from_asset_id": "a1", "to_asset_id": "a2",
        "edge_type": "INGESTS_TO", "pipeline_id": "pipe_1", "metadata": {"connector_id": "c1"},
    }


def test_list_assets_route_awaits_the_service(monkeypatch) -> None:
    asset = {
        "id": "a1", "org_id": "org-1", "asset_type": "kafka_topic",
        "name": "t", "qualified_name": "org-1:topic:t", "metadata": {},
    }

    async def _list_assets(org_id):
        return [asset]

    monkeypatch.setattr("app.api.routes.lineage.lineage_service.list_assets", _list_assets)

    resp = client.get("/api/v1/dataconnect/assets", headers=HDR)

    assert resp.status_code == 200
    assert resp.json() == [asset]


def test_get_lineage_route_awaits_the_service(monkeypatch) -> None:
    graph = {"nodes": [], "edges": []}
    captured = {}

    async def _graph_for_asset(org_id, asset_id, *, depth, direction, asset_type, limit, offset):
        assert asset_id == "a1"
        captured.update(depth=depth, direction=direction, asset_type=asset_type, limit=limit, offset=offset)
        return graph

    monkeypatch.setattr("app.api.routes.lineage.lineage_service.graph_for_asset", _graph_for_asset)

    resp = client.get("/api/v1/dataconnect/lineage/a1", headers=HDR)

    assert resp.status_code == 200
    assert resp.json() == {**graph, "truncated": False, "omitted_edges": 0, "next_offset": None}
    # FE-BUG-20: the route's own defaults, unbounded-graph-growth-safe (limit None = the configured per-hop cap).
    assert captured == {"depth": 1, "direction": "both", "asset_type": None, "limit": None, "offset": 0}


def test_get_lineage_route_passes_through_depth_direction_asset_type(monkeypatch) -> None:
    captured = {}

    async def _graph_for_asset(org_id, asset_id, *, depth, direction, asset_type, limit, offset):
        captured.update(depth=depth, direction=direction, asset_type=asset_type, limit=limit, offset=offset)
        return {"nodes": [], "edges": []}

    monkeypatch.setattr("app.api.routes.lineage.lineage_service.graph_for_asset", _graph_for_asset)

    resp = client.get(
        "/api/v1/dataconnect/lineage/a1",
        headers=HDR,
        params={"depth": 3, "direction": "upstream", "asset_type": "kafka_topic", "limit": 25, "offset": 50},
    )

    assert resp.status_code == 200
    assert captured == {"depth": 3, "direction": "upstream", "asset_type": "kafka_topic", "limit": 25, "offset": 50}


def test_get_lineage_route_rejects_an_out_of_range_limit() -> None:
    assert client.get("/api/v1/dataconnect/lineage/a1", headers=HDR, params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/dataconnect/lineage/a1", headers=HDR, params={"limit": 100000}).status_code == 422


def test_get_lineage_route_rejects_an_invalid_direction(monkeypatch) -> None:
    resp = client.get("/api/v1/dataconnect/lineage/a1", headers=HDR, params={"direction": "sideways"})
    assert resp.status_code == 422


def test_get_lineage_route_caps_depth_at_the_service_maximum(monkeypatch) -> None:
    resp = client.get("/api/v1/dataconnect/lineage/a1", headers=HDR, params={"depth": 999})
    assert resp.status_code == 422


def test_get_pipeline_lineage_route_awaits_the_service(monkeypatch) -> None:
    graph = {"nodes": [], "edges": []}

    async def _graph_for_pipeline(org_id, pipeline_id):
        assert pipeline_id == "pipe_1"
        return graph

    monkeypatch.setattr("app.api.routes.lineage.lineage_service.graph_for_pipeline", _graph_for_pipeline)

    resp = client.get("/api/v1/dataconnect/lineage/pipeline/pipe_1", headers=HDR)

    assert resp.status_code == 200
    # A pipeline's own chain is already bounded, so the truncation fields stay at their defaults.
    assert resp.json() == {**graph, "truncated": False, "omitted_edges": 0, "next_offset": None}


def test_consume_lineage_event_route_awaits_the_service(monkeypatch) -> None:
    captured = {}

    async def _consume_runtime_event(event):
        captured.update(event)
        return {
            "nodes": [],
            "edges": [
                {
                    "id": "e1", "org_id": event["org_id"], "from_asset_id": "a1", "to_asset_id": "a2",
                    "edge_type": event["edge_type"], "pipeline_id": event["pipeline_id"],
                }
            ],
        }

    monkeypatch.setattr("app.api.routes.lineage.lineage_service.consume_runtime_event", _consume_runtime_event)

    resp = client.post(
        "/api/v1/dataconnect/lineage/events",
        headers=HDR,
        json={
            "org_id": "org-1",
            "event_type": "pipeline_deployed",
            "from_asset": {"asset_type": "connector", "name": "c", "qualified_name": "org-1:connector:c"},
            "to_asset": {"asset_type": "kafka_topic", "name": "t", "qualified_name": "org-1:topic:t"},
            "edge_type": "INGESTS_TO",
            "pipeline_id": "pipe_1",
        },
    )

    assert resp.status_code == 200
    assert captured["pipeline_id"] == "pipe_1"
    assert resp.json()["edges"][0]["pipeline_id"] == "pipe_1"


# ── FE-BUG-20: bounded BFS traversal ────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_for_asset_default_depth_one_both_directions(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_lineage_1")

    graph = await lineage_service.graph_for_asset("org_lineage_1", nodes["c"]["id"])

    node_names = {n["name"] for n in graph["nodes"]}
    assert node_names == {"B", "C", "D"}  # one hop either side of the seed
    edge_types = {e["edge_type"] for e in graph["edges"]}
    assert edge_types == {"INGESTS_TO", "VALIDATED_BY"}


@pytest.mark.asyncio
async def test_graph_for_asset_depth_zero_returns_only_the_seed(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_lineage_2")

    graph = await lineage_service.graph_for_asset("org_lineage_2", nodes["c"]["id"], depth=0)

    assert [n["name"] for n in graph["nodes"]] == ["C"]
    assert graph["edges"] == []


@pytest.mark.asyncio
async def test_graph_for_asset_downstream_only_walks_forward(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_lineage_3")

    graph = await lineage_service.graph_for_asset("org_lineage_3", nodes["a"]["id"], depth=2, direction="downstream")

    assert {n["name"] for n in graph["nodes"]} == {"A", "B", "C"}


@pytest.mark.asyncio
async def test_graph_for_asset_upstream_only_walks_backward(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_lineage_4")

    graph = await lineage_service.graph_for_asset("org_lineage_4", nodes["c"]["id"], depth=1, direction="upstream")

    # B feeds INTO C; D is downstream of C — upstream-only must not include it.
    assert {n["name"] for n in graph["nodes"]} == {"B", "C"}


@pytest.mark.asyncio
async def test_graph_for_asset_asset_type_filter_drops_the_node_and_its_edge(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_lineage_5")

    graph = await lineage_service.graph_for_asset(
        "org_lineage_5", nodes["e"]["id"], depth=1, direction="downstream", asset_type="kafka_topic",
    )

    # F (sink) is the only downstream neighbor of E and isn't a kafka_topic —
    # filtered out, and its WRITTEN_TO edge must not dangle in the response.
    assert [n["name"] for n in graph["nodes"]] == ["E"]
    assert graph["edges"] == []


@pytest.mark.asyncio
async def test_graph_for_asset_is_org_scoped(fake_postgres) -> None:
    mine = _seed_chain(fake_postgres, "org_lineage_6")
    _seed_chain(fake_postgres, "org_lineage_other")

    graph = await lineage_service.graph_for_asset("org_lineage_6", mine["c"]["id"], depth=5)

    assert all(n["name"] in {"A", "B", "C", "D", "E", "F"} for n in graph["nodes"])
    assert len(graph["nodes"]) == 6  # never picks up org_lineage_other's identically-named chain


@pytest.mark.asyncio
async def test_graph_for_asset_unknown_id_returns_an_empty_graph(fake_postgres) -> None:
    graph = await lineage_service.graph_for_asset("org_lineage_7", "no-such-asset")
    assert graph == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_graph_for_asset_depth_is_capped_at_the_service_maximum(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_lineage_8")

    # Requesting far more than MAX_LINEAGE_DEPTH still only reaches what the
    # actual chain length allows — proving the cap doesn't error, just bounds.
    graph = await lineage_service.graph_for_asset("org_lineage_8", nodes["a"]["id"], depth=999, direction="downstream")

    assert {n["name"] for n in graph["nodes"]} == {"A", "B", "C", "D", "E", "F"}


@pytest.mark.asyncio
async def test_graph_for_pipeline_resolves_only_its_own_referenced_assets(fake_postgres) -> None:
    """FE-BUG-20: no longer scans the org's whole asset table — an asset
    belonging to a different pipeline in the same org must never leak in."""
    nodes = _seed_chain(fake_postgres, "org_lineage_9")
    other = fake_postgres.create("assets", {"org_id": "org_lineage_9", "asset_type": "source", "name": "Unrelated", "qualified_name": "org_lineage_9:unrelated"})
    fake_postgres.create("lineage_edges", {
        "org_id": "org_lineage_9", "from_asset_id": other["id"], "to_asset_id": nodes["a"]["id"],
        "edge_type": "CONNECTS_TO", "pipeline_id": "pipe-2",
    })

    graph = await lineage_service.graph_for_pipeline("org_lineage_9", "pipe-1")

    node_names = {n["name"] for n in graph["nodes"]}
    assert node_names == {"A", "B", "C", "D", "E", "F"}
    assert "Unrelated" not in node_names


# ── FE-BUG-20: width bound (per-hop edge limit + paging) ────────────────────

def _seed_hub(fake_postgres, org_id: str, fan_out: int) -> dict:
    """One topic feeding `fan_out` sinks, plus one upstream connector."""
    hub = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "kafka_topic", "name": "hub", "qualified_name": f"{org_id}:hub"})
    upstream = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "connector", "name": "src", "qualified_name": f"{org_id}:src"})
    fake_postgres.create("lineage_edges", {
        "org_id": org_id, "from_asset_id": upstream["id"], "to_asset_id": hub["id"], "edge_type": "INGESTS_TO", "pipeline_id": None,
    })
    sinks = []
    for i in range(fan_out):
        sink = fake_postgres.create("assets", {"org_id": org_id, "asset_type": "sink", "name": f"sink{i}", "qualified_name": f"{org_id}:sink{i}"})
        sinks.append(sink)
        fake_postgres.create("lineage_edges", {
            "org_id": org_id, "from_asset_id": hub["id"], "to_asset_id": sink["id"], "edge_type": "CONSUMED_BY", "pipeline_id": None,
        })
    return {"hub": hub, "upstream": upstream, "sinks": sinks}


@pytest.mark.asyncio
async def test_a_hub_hop_is_capped_and_reports_what_was_left_out(fake_postgres) -> None:
    seeded = _seed_hub(fake_postgres, "org_hub_1", fan_out=25)

    graph = await lineage_service.graph_for_asset(
        "org_hub_1", seeded["hub"]["id"], depth=1, direction="downstream", limit=10,
    )

    assert len(graph["edges"]) == 10
    assert len(graph["nodes"]) == 11  # the hub plus 10 sinks
    assert graph["truncated"] is True
    assert graph["omitted_edges"] == 15
    assert graph["next_offset"] == 10


@pytest.mark.asyncio
async def test_paging_with_next_offset_visits_every_edge_exactly_once(fake_postgres) -> None:
    seeded = _seed_hub(fake_postgres, "org_hub_2", fan_out=25)
    seen_sinks: list[str] = []
    offset: int | None = 0
    pages = 0

    while offset is not None:
        graph = await lineage_service.graph_for_asset(
            "org_hub_2", seeded["hub"]["id"], depth=1, direction="downstream", limit=10, offset=offset,
        )
        seen_sinks += [n["name"] for n in graph["nodes"] if n["name"].startswith("sink")]
        offset = graph["next_offset"]
        pages += 1

    assert pages == 3
    assert sorted(seen_sinks) == sorted(f"sink{i}" for i in range(25))


@pytest.mark.asyncio
async def test_direction_is_applied_before_the_cap_so_the_other_side_does_not_use_it_up(fake_postgres) -> None:
    seeded = _seed_hub(fake_postgres, "org_hub_3", fan_out=3)

    graph = await lineage_service.graph_for_asset(
        "org_hub_3", seeded["hub"]["id"], depth=1, direction="upstream", limit=1,
    )

    assert graph["truncated"] is False  # one upstream edge exists, not four
    assert {n["name"] for n in graph["nodes"]} == {"hub", "src"}


@pytest.mark.asyncio
async def test_a_graph_within_the_limit_is_not_truncated(fake_postgres) -> None:
    nodes = _seed_chain(fake_postgres, "org_hub_4")

    graph = await lineage_service.graph_for_asset("org_hub_4", nodes["c"]["id"])

    assert graph["truncated"] is False
    assert graph["omitted_edges"] == 0
    assert graph["next_offset"] is None
