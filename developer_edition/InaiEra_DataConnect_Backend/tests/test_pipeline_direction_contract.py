"""
GAP-12, the rest of the direction contract: a pipeline's direction decides its shape, deterministically.

  SOURCE  source object required; generated output topic; sink-only settings (`topics`) prohibited.
  SINK    input topic required — and it must be this organization's own (a sink must not read another tenant's topic);
          NO generated output topic; source-only settings (`topic.prefix`, `tables`, ...) prohibited.

(tests/test_pipeline_direction.py covers the original derive-from-template and required-input rules.)
"""

from types import SimpleNamespace

import pytest

from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate
from app.services import connect_config
from app.services.lineage_service import lineage_service
from app.services.pipeline_service import pipeline_service
from app.services.pipeline_topics import data_topic, input_topics, is_sink_pipeline
from app.services.runtime_ownership import pipeline_by_topic_name
from app.services.sink_topics import (
    SinkTopicError,
    misplaced_config_keys,
    normalize_sink_topics,
    validate_sink_topics,
)
from app.services.topic_naming import build_connector_quarantine_topic_name

# ── the topic rules, on their own ──────────────────────────────────────────────────────────────────────────────────


def test_a_topic_in_the_organizations_namespace_is_accepted() -> None:
    assert validate_sink_topics("raw-sales-pg-orders-batch-v1") == ["raw-sales-pg-orders-batch-v1"]


def test_a_list_or_a_comma_separated_string_is_normalized_and_deduplicated() -> None:
    assert normalize_sink_topics(["raw-t-s-a-batch-v1", " raw-t-s-b-batch-v1 ", "raw-t-s-a-batch-v1"]) == "raw-t-s-a-batch-v1,raw-t-s-b-batch-v1"
    assert normalize_sink_topics("raw-t-s-a-batch-v1, raw-t-s-b-batch-v1,,") == "raw-t-s-a-batch-v1,raw-t-s-b-batch-v1"




def test_a_sink_input_topic_names_no_organization() -> None:
    """Since 2026-11-06 a topic name carries no organization: any well-formed six-part name is accepted, and which organization it belongs to
    is a matter of whose Kafka it is on, not of its name."""
    assert validate_sink_topics("raw-sales-pg-orders-batch-v1,prepared-t-s-x-batch-v1") == ["raw-sales-pg-orders-batch-v1", "prepared-t-s-x-batch-v1"]
    assert validate_sink_topics("org_1001-raw-sales-pg-orders-batch-v1") == ["org_1001-raw-sales-pg-orders-batch-v1"]   # an old name still reads


@pytest.mark.parametrize(
    "topic",
    [
        "_dataconnect.configs",  # platform-internal topic
        "raw.sales.orders",  # dots, not the six "-"-joined parts
        "raw-custom",  # not a six-part name
    ],
)
def test_a_malformed_topic_is_invalid_topic_name(topic) -> None:
    with pytest.raises(SinkTopicError, match="not a valid input topic name") as exc_info:
        validate_sink_topics(topic)
    assert exc_info.value.code == "INVALID_TOPIC_NAME"




@pytest.mark.parametrize("topic", ["org_1.*", "org_1\\..*", "raw-t-s-a-batch-v1 b", "org_1.a|org_1.b", "org_1.(a)", "org_1.a/../b", "org_1." + "x" * 300])
def test_a_wildcard_pattern_or_odd_name_is_rejected(topic) -> None:
    with pytest.raises(SinkTopicError, match="not a valid input topic name"):
        validate_sink_topics(topic)


@pytest.mark.parametrize("raw", [None, "", "  ,  ", [], 7, {"a": 1}, ["raw-t-s-a-batch-v1", 3]])
def test_a_missing_or_malformed_input_is_rejected(raw) -> None:
    with pytest.raises(SinkTopicError):
        validate_sink_topics(raw)


def test_each_direction_names_the_settings_that_belong_to_the_other() -> None:
    assert misplaced_config_keys("SOURCE", {"tables": ["t"], "topics": "raw-t-s-a-batch-v1", "topics.regex": ".*"}) == ["topics", "topics.regex"]
    assert misplaced_config_keys("SINK", {"topics": "raw-t-s-a-batch-v1", "topic.prefix": "x", "tables": ["t"]}) == ["tables", "topic.prefix"]
    assert misplaced_config_keys("SINK", {"topics": "raw-t-s-a-batch-v1", "table.name.format": "t"}) == []
    assert misplaced_config_keys("SOURCE", None) == []


# ── creation and update ────────────────────────────────────────────────────────────────────────────────────────────


def _seed(fake_postgres, org_id: str, connector_type: str) -> tuple[dict, dict]:
    klass = "io.confluent.connect.jdbc.JdbcSinkConnector" if connector_type == "sink" else "io.confluent.connect.jdbc.JdbcSourceConnector"
    template = fake_postgres.create(
        "connector_templates", {"connector_class": klass, "connector_type": connector_type, "default_config": {}},
    )
    connection = fake_postgres.create("connection_profiles", {"org_id": org_id, "name": "c", "connection_config": {}})
    return template, connection


def _payload(template: dict, connection: dict, **overrides) -> PipelineCreate:
    base = {
        "name": "P", "template_id": template["id"], "connection_profile_id": connection["id"], "zone": "prepared",
        "domain": "sales", "source_system": "pg", "entity": "orders_copy", "mode": "batch", "version": "v1",
    }
    base.update(overrides)
    return PipelineCreate(**base)








@pytest.mark.asyncio
async def test_several_input_topics_are_stored_in_normalized_form(fake_postgres) -> None:
    template, connection = _seed(fake_postgres, "org_a", "sink")

    created = await pipeline_service.create_pipeline(
        "org_a", _payload(template, connection, connector_config={"topics": ["raw-t-s-one-batch-v1", "raw-t-s-two-batch-v1", "raw-t-s-one-batch-v1"]}),
    )

    assert created["connector_config"]["topics"] == "raw-t-s-one-batch-v1,raw-t-s-two-batch-v1"


@pytest.mark.asyncio
async def test_a_source_cannot_carry_a_sink_input_setting(fake_postgres) -> None:
    template, connection = _seed(fake_postgres, "org_a", "source")

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline(
            "org_a", _payload(template, connection, zone="raw", connector_config={"topics": "raw-t-s-raw_x-batch-v1"}),
        )

    assert "Source pipelines cannot set 'topics'" in exc_info.value.message
    assert exc_info.value.field_errors[0].field == "connector_config.topics"


@pytest.mark.asyncio
async def test_a_sink_cannot_carry_source_only_settings(fake_postgres) -> None:
    template, connection = _seed(fake_postgres, "org_a", "sink")

    with pytest.raises(ValidationError) as exc_info:
        await pipeline_service.create_pipeline(
            "org_a", _payload(template, connection, connector_config={"topics": "raw-t-s-x-batch-v1", "topic.prefix": "p", "tables": ["t"]}),
        )

    assert "Sink pipelines cannot set" in exc_info.value.message
    assert {e.field for e in exc_info.value.field_errors} == {"connector_config.tables", "connector_config.topic.prefix"}


# ── a sink has no generated topic ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_sink_is_created_without_a_generated_topic_and_a_source_with_one(fake_postgres) -> None:
    sink_template, connection = _seed(fake_postgres, "org_a", "sink")
    source_template, _ = _seed(fake_postgres, "org_a", "source")

    sink = await pipeline_service.create_pipeline(
        "org_a", _payload(sink_template, connection, connector_config={"topics": "raw-sales-pg-orders-batch-v1"}),
    )
    source = await pipeline_service.create_pipeline(
        "org_a", _payload(source_template, connection, zone="raw", entity="orders", name="S"),
    )

    assert sink["direction"] == "SINK"
    assert sink["topic_name"] is None
    assert sink["catalog_qualified_name"] is None
    assert source["direction"] == "SOURCE"
    assert source["topic_name"] == "raw-sales-pg-orders-batch-v1"
    assert source["catalog_qualified_name"]
    # The sink's own generated config reads the topic it was given, and creates none.
    assert sink["generated_config"]["topics"] == "raw-sales-pg-orders-batch-v1"
    assert "topic.prefix" not in sink["generated_config"]


@pytest.mark.asyncio
async def test_a_sinks_readiness_has_no_generated_topic_to_collide_on(fake_postgres) -> None:
    template, connection = _seed(fake_postgres, "org_a", "sink")
    sink = await pipeline_service.create_pipeline(
        "org_a", _payload(template, connection, connector_config={"topics": "raw-sales-pg-orders-batch-v1"}),
    )

    readiness = await pipeline_service.check_deploy_readiness("org_a", sink["id"])

    topic_check = next(c for c in readiness["checks"] if c["key"] == "topic_name_available")
    assert topic_check["ok"] is True
    assert readiness["topic_name"] is None


# ── enforced again wherever the connector config is built ──────────────────────────────────────────────────────────




def test_building_a_sinks_config_returns_the_normalized_topics() -> None:
    row = {"org_id": "org_a", "connector_config": {"topics": " raw-t-s-one-batch-v1 , raw-t-s-two-batch-v1 "}}

    assert connect_config._sink_topic(row) == "raw-t-s-one-batch-v1,raw-t-s-two-batch-v1"


# ── a sink owns no topic but its own dead-letter ones ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_sink_is_not_attributed_the_topics_it_reads_but_keeps_its_dead_letter_topic(fake_postgres) -> None:
    source = fake_postgres.create("pipelines", {
        "org_id": "org_a", "direction": "SOURCE", "connector_name": "src", "topic_name": "raw-sales-pg-orders-batch-v1",
        "domain": "sales", "source_system": "pg", "entity": "orders", "version": "v1",
        "connector_config": {"tables": ["orders"]},
    })
    sink = fake_postgres.create("pipelines", {
        "org_id": "org_a", "direction": "SINK", "connector_name": "snk", "topic_name": None,
        "domain": "sales", "source_system": "pg", "entity": "orders_copy", "version": "v1",
        "connector_config": {"topics": "raw-sales-pg-orders-batch-v1"},
    })

    owned = await pipeline_by_topic_name("org_a")

    assert owned["raw-sales-pg-orders-batch-v1"]["id"] == source["id"]  # the producer, never the reader
    dead_letter = build_connector_quarantine_topic_name(
        domain="sales", source_system="pg", entity="orders_copy", version="v1",
    )
    assert owned[dead_letter]["id"] == sink["id"]


# ── which topic a pipeline's data is on ────────────────────────────────────────────────────────────────────────────


def test_the_data_topic_is_a_sources_generated_topic_or_a_sinks_first_input() -> None:
    source = {"direction": "SOURCE", "topic_name": "raw-t-s-raw_x-batch-v1", "connector_config": {"tables": ["x"]}}
    sink = {"direction": "SINK", "topic_name": None, "connector_config": {"topics": "raw-t-s-in1-batch-v1, raw-t-s-in2-batch-v1"}}

    assert data_topic(source) == "raw-t-s-raw_x-batch-v1"
    assert data_topic(sink) == "raw-t-s-in1-batch-v1"
    assert input_topics(sink) == ["raw-t-s-in1-batch-v1", "raw-t-s-in2-batch-v1"]
    assert input_topics(source) == []
    assert is_sink_pipeline(sink)
    assert data_topic({"direction": "SINK", "connector_config": {}}) is None


@pytest.mark.asyncio
async def test_a_skipped_quality_result_on_a_sink_names_the_topic_it_reads(fake_postgres, monkeypatch) -> None:
    import app.repositories.dataconnect as repositories
    import app.services.quality_service as quality_module

    stored = {}

    class _Results:
        def __init__(self, session):
            pass

        async def create(self, values):
            stored.update(values)
            return SimpleNamespace(**values, id="r1", created_at=None)

    monkeypatch.setattr(repositories, "QualityResultRepository", _Results)
    monkeypatch.setattr(quality_module, "_result_to_dict", lambda row: {"topic_name": row.topic_name})
    template, connection = _seed(fake_postgres, "org_a", "sink")
    sink = await pipeline_service.create_pipeline(
        "org_a", _payload(template, connection, connector_config={"topics": "raw-sales-pg-orders-batch-v1"}),
    )

    await pipeline_service._record_quality_skip("org_a", sink, {"id": "pol-1"}, "no rows")

    assert stored["topic_name"] == "raw-sales-pg-orders-batch-v1"


# ── lineage ────────────────────────────────────────────────────────────────────────────────────────────────────────


class _RecordingLineage:
    def __init__(self) -> None:
        self.assets: list[dict] = []
        self.edges: list[dict] = []

    async def asset(self, session, **kwargs):
        self.assets.append(kwargs)
        return SimpleNamespace(id=f"a{len(self.assets)}", **kwargs, metadata_json={}, created_at=None, updated_at=None)

    async def edge(self, session, **kwargs):
        self.edges.append(kwargs)
        return SimpleNamespace(
            id=f"e{len(self.edges)}", **{k: v for k, v in kwargs.items() if k != "metadata"}, metadata_json={}, created_at=None,
        )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_a_sinks_lineage_is_input_topic_to_connector_to_target_not_a_made_up_source_chain(monkeypatch) -> None:
    import app.services.lineage_service as lineage_module

    recorder = _RecordingLineage()
    monkeypatch.setattr(lineage_module, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(lineage_service, "_get_or_create_asset", recorder.asset)
    monkeypatch.setattr(lineage_service, "_get_or_create_edge", recorder.edge)
    monkeypatch.setattr(lineage_module, "_asset_to_dict", lambda a: {"asset_type": a.asset_type, "name": a.name})
    monkeypatch.setattr(lineage_module, "_edge_to_dict", lambda e: {"edge_type": e.edge_type})

    async def _no_prune(session, **kwargs):           # removing stale edges needs a real session; tested against PostgreSQL
        return None
    monkeypatch.setattr(lineage_service, "_prune_stale_edges", _no_prune)
    sink = {
        "id": "p1", "org_id": "org_a", "direction": "SINK", "topic_name": None, "connector_name": "sales--pg--orders_copy--batch--v1",
        "domain": "sales", "source_system": "pg", "entity": "orders_copy", "mode": "batch",
        "connector_config": {"topics": "raw-sales-pg-orders-batch-v1"},
    }

    graph = await lineage_service.materialize_pipeline_lineage(sink)

    assert {a["asset_type"] for a in recorder.assets} == {"connector", "sink", "kafka_topic"}
    assert [e["edge_type"] for e in graph["edges"]] == ["CONSUMED_BY", "WRITTEN_TO"]
    names = {a["qualified_name"] for a in recorder.assets}
    assert "org_a:topic:raw-sales-pg-orders-batch-v1" in names
    assert not any(":source:" in n or ":processor:" in n for n in names)  # nothing fabricated
    assert not any("prepared" in n for n in names)
