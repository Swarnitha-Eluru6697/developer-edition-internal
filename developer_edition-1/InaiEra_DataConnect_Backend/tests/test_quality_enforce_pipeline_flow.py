"""NEW-01 through the real service paths: creating and editing a pipeline in ENFORCE mode, what a deploy provisions, what a sink connector is
given to read, and what lineage draws. (The worker itself is covered in test_quality_enforce_mode.py.)"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.errors import ValidationError
from app.schemas.pipeline import PipelineCreate, PipelineUpdate
from app.services.lineage_service import lineage_service
from app.services.pipeline_service import pipeline_service
from app.services.runtime_orchestrator import KafkaConnectClient

ORG = "org_enf"
SOURCE_TOPIC = "raw-sales-orders_app-orders-cdc-v1"
PREPARED = "prepared-sales-orders_app-orders-cdc-v1"
JDBC_SOURCE = "io.confluent.connect.jdbc.JdbcSourceConnector"
JDBC_SINK = "io.confluent.connect.jdbc.JdbcSinkConnector"


@pytest.fixture
def world(fake_postgres):
    """A source template, a sink template, a connection, and one quality policy (a second one that routes failures nowhere)."""
    return SimpleNamespace(
        source_template=fake_postgres.create(
            "connector_templates", {"connector_class": JDBC_SOURCE, "connector_type": "source", "default_config": {}}),
        sink_template=fake_postgres.create(
            "connector_templates", {"connector_class": JDBC_SINK, "connector_type": "sink", "default_config": {}}),
        connection=fake_postgres.create("connection_profiles", {"org_id": ORG, "name": "Conn", "connection_config": {}}),
        policy=fake_postgres.create("quality_policies", {
            "org_id": ORG, "name": "P", "rules": [], "on_failure": "route_to_quarantine", "quarantine_sink": "kafka"}),
        log_policy=fake_postgres.create("quality_policies", {
            "org_id": ORG, "name": "Log only", "rules": [], "on_failure": "log", "quarantine_sink": "kafka"}),
    )


def _source(world, **over) -> PipelineCreate:
    base = {
        "name": "Orders", "template_id": world.source_template["id"], "connection_profile_id": world.connection["id"],
        "zone": "raw", "domain": "sales", "source_system": "orders_app", "entity": "orders", "mode": "cdc", "version": "v1",
        "quality_policy_id": world.policy["id"], "quality_mode": "ENFORCE",
    }
    return PipelineCreate(**{**base, **over})


def _sink(world, topic, **over) -> PipelineCreate:
    base = {
        "name": "Orders copy", "template_id": world.sink_template["id"], "connection_profile_id": world.connection["id"],
        "zone": "raw", "domain": "sales", "source_system": "orders_app", "entity": "orders_copy", "mode": "batch", "version": "v1",
        "connector_config": {"topics": topic},
    }
    return PipelineCreate(**{**base, **over})


# ── Create and edit ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_enforce_pipeline_reports_its_mode_and_its_prepared_topic(world) -> None:
    created = await pipeline_service.create_pipeline(ORG, _source(world))

    assert created["quality_mode"] == "ENFORCE"
    assert created["topic_name"] == SOURCE_TOPIC
    assert created["prepared_topic"] == PREPARED


@pytest.mark.asyncio
async def test_an_observe_pipeline_has_no_prepared_topic(world) -> None:
    created = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))

    assert created["quality_mode"] == "OBSERVE"
    assert created["prepared_topic"] is None


@pytest.mark.asyncio
async def test_the_default_is_observe_so_existing_clients_are_unchanged(world) -> None:
    created = await pipeline_service.create_pipeline(ORG, PipelineCreate(
        name="Orders", template_id=world.source_template["id"], connection_profile_id=world.connection["id"],
        zone="raw", domain="sales", source_system="orders_app", entity="orders", mode="cdc", version="v1",
        quality_policy_id=world.policy["id"],
    ))

    assert created["quality_mode"] == "OBSERVE"


@pytest.mark.asyncio
async def test_enforce_without_a_policy_is_refused(world) -> None:
    with pytest.raises(ValidationError) as exc:
        await pipeline_service.create_pipeline(ORG, _source(world, quality_policy_id=None))

    assert exc.value.field_errors[0].field == "quality_mode"


@pytest.mark.asyncio
async def test_enforce_with_a_policy_that_does_not_quarantine_is_refused(world) -> None:
    with pytest.raises(ValidationError) as exc:
        await pipeline_service.create_pipeline(ORG, _source(world, quality_policy_id=world.log_policy["id"]))

    assert "route failures to quarantine" in str(exc.value)


@pytest.mark.asyncio
async def test_a_pipeline_can_be_switched_to_enforce_and_back(world) -> None:
    created = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))

    enforced = await pipeline_service.update_pipeline(ORG, created["id"], PipelineUpdate(quality_mode="ENFORCE"))
    assert enforced["quality_mode"] == "ENFORCE"
    assert enforced["prepared_topic"] == PREPARED
    assert enforced["revision"] == created["revision"] + 1

    observed = await pipeline_service.update_pipeline(ORG, created["id"], PipelineUpdate(quality_mode="OBSERVE"))
    assert observed["quality_mode"] == "OBSERVE"
    assert observed["prepared_topic"] is None


@pytest.mark.asyncio
async def test_an_unrelated_edit_keeps_the_mode(world) -> None:
    created = await pipeline_service.create_pipeline(ORG, _source(world))

    renamed = await pipeline_service.update_pipeline(ORG, created["id"], PipelineUpdate(name="Orders v2"))

    assert renamed["quality_mode"] == "ENFORCE"
    assert renamed["prepared_topic"] == PREPARED


@pytest.mark.asyncio
async def test_switching_to_enforce_is_refused_while_a_sink_still_reads_the_data_topic(world) -> None:
    source = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))
    await pipeline_service.create_pipeline(ORG, _sink(world, SOURCE_TOPIC))

    with pytest.raises(ValidationError) as exc:
        await pipeline_service.update_pipeline(ORG, source["id"], PipelineUpdate(quality_mode="ENFORCE"))

    assert "Orders copy" in str(exc.value)
    assert PREPARED in str(exc.value)


@pytest.mark.asyncio
async def test_after_the_sink_moves_to_the_prepared_topic_the_source_can_enforce(world) -> None:
    source = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))
    sink = await pipeline_service.create_pipeline(ORG, _sink(world, SOURCE_TOPIC))

    await pipeline_service.update_pipeline(ORG, sink["id"], PipelineUpdate(connector_config={"topics": PREPARED}))
    enforced = await pipeline_service.update_pipeline(ORG, source["id"], PipelineUpdate(quality_mode="ENFORCE"))

    assert enforced["quality_mode"] == "ENFORCE"


@pytest.mark.asyncio
async def test_a_new_sink_cannot_read_an_enforce_sources_data_topic_but_can_read_its_prepared_topic(world) -> None:
    await pipeline_service.create_pipeline(ORG, _source(world))

    with pytest.raises(ValidationError) as exc:
        await pipeline_service.create_pipeline(ORG, _sink(world, SOURCE_TOPIC))
    assert PREPARED in str(exc.value)

    created = await pipeline_service.create_pipeline(ORG, _sink(world, PREPARED))
    assert created["connector_config"]["topics"] == PREPARED


# ── Deploy ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _noop(*args, **kwargs):
    return None


async def _noop_lineage(*args, **kwargs) -> dict:
    return {"nodes": [], "edges": []}


def _deploy_harness(monkeypatch):
    """Everything outside the service stubbed; records the order of topic creation and connector deployment."""
    calls: list[tuple[str, object]] = []

    async def _ensure(topic, **kwargs):
        calls.append(("ensure", topic))

    async def _deploy_connector(self, connector_name, config):
        calls.append(("deploy", config))
        return {"config": config}

    async def _plugins(self):
        return [{"class": JDBC_SOURCE, "type": "source", "version": "10.7.4"}, {"class": JDBC_SINK, "type": "sink", "version": "10.7.4"}]

    monkeypatch.setattr(settings, "runtime_deploy_enabled", True)
    monkeypatch.setattr("app.services.pipeline_service.runtime_reconciler.record_desired", _noop)
    monkeypatch.setattr("app.services.pipeline_service.lineage_service.materialize_pipeline_lineage", _noop_lineage)
    monkeypatch.setattr("app.services.kafka_admin.ensure_simple_topic", _ensure)
    monkeypatch.setattr(KafkaConnectClient, "deploy_connector", _deploy_connector)
    monkeypatch.setattr(KafkaConnectClient, "list_connector_plugins", _plugins)
    return calls


@pytest.mark.asyncio
async def test_deploying_an_enforce_pipeline_creates_the_prepared_topic_before_the_connector(world, monkeypatch) -> None:
    created = await pipeline_service.create_pipeline(ORG, _source(world))
    calls = _deploy_harness(monkeypatch)

    await pipeline_service.deploy(ORG, created["id"])

    assert [kind for kind, _ in calls] == ["ensure", "deploy"]
    assert calls[0] == ("ensure", PREPARED)


@pytest.mark.asyncio
async def test_deploying_an_observe_pipeline_creates_no_prepared_topic(world, monkeypatch) -> None:
    created = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))
    calls = _deploy_harness(monkeypatch)

    await pipeline_service.deploy(ORG, created["id"])

    assert [kind for kind, _ in calls] == ["deploy"]


@pytest.mark.asyncio
async def test_an_enforce_sink_connector_is_deployed_reading_its_internal_hand_off_topic(world, monkeypatch) -> None:
    """The sink's quality worker hands passing records to the sink's own connector through an internal topic: hidden from the catalog
    (the "_" system-topic prefix) and trimmed as the connector writes, so passing records end up in the table, not stored in Kafka."""
    sink = await pipeline_service.create_pipeline(ORG, _sink(
        world, "raw-ext-feed-orders-batch-v1", quality_policy_id=world.policy["id"], quality_mode="ENFORCE"))
    calls = _deploy_harness(monkeypatch)

    await pipeline_service.deploy(ORG, sink["id"])

    sink_prepared = "_dataconnect.gate.sales-orders_app-orders_copy-batch-v1"
    assert sink["prepared_topic"] == sink_prepared
    assert calls[0] == ("ensure", sink_prepared)
    (_, config) = calls[1]
    assert config["topics"] == sink_prepared                        # not the topic the worker consumes


@pytest.mark.asyncio
async def test_an_observe_sink_connector_is_deployed_reading_exactly_its_configured_topic(world, monkeypatch) -> None:
    topic = "raw-ext-feed-orders-batch-v1"
    sink = await pipeline_service.create_pipeline(ORG, _sink(world, topic, quality_policy_id=world.policy["id"]))
    calls = _deploy_harness(monkeypatch)

    await pipeline_service.deploy(ORG, sink["id"])

    assert calls[0][0] == "deploy"
    assert calls[0][1]["topics"] == topic


# ── Lineage ────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def lineage(monkeypatch):
    """Records which assets and edges the lineage graph is built from, with no database."""
    made = SimpleNamespace(assets=[], edges=[])

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace(commit=_noop)

    async def _asset(session, *, org_id, asset_type, name, qualified_name, metadata):
        made.assets.append((asset_type, name))
        return SimpleNamespace(id=name, asset_type=asset_type, name=name, qualified_name=qualified_name, asset_metadata=metadata,
                               org_id=org_id, **{"metadata": metadata})

    async def _edge(session, *, org_id, from_asset_id, to_asset_id, edge_type, pipeline_id, metadata):
        made.edges.append((from_asset_id, edge_type, to_asset_id))
        return SimpleNamespace(id=f"{from_asset_id}>{to_asset_id}", edge_type=edge_type)

    monkeypatch.setattr("app.services.lineage_service.AsyncSessionLocal", _session)
    # On the INSTANCE, not the class: an earlier test may leave an instance-level replacement, which would win over a class-level one.
    monkeypatch.setattr(lineage_service, "_get_or_create_asset", _asset)
    monkeypatch.setattr(lineage_service, "_get_or_create_edge", _edge)

    async def _policy(pipeline):                      # the lineage draws the policy (and its quarantine topic) only for a pipeline that has one
        return {"name": "Orders checks", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka"} if pipeline.get("quality_policy_id") else None

    async def _no_prune(session, **kwargs):           # removing stale edges needs a real session; it is tested against PostgreSQL (test_lineage_quality_flow_real_db.py)
        return None

    monkeypatch.setattr(lineage_service, "_quality_policy_of", _policy)
    monkeypatch.setattr(lineage_service, "_prune_stale_edges", _no_prune)
    monkeypatch.setattr("app.services.lineage_service._asset_to_dict", lambda a: {"name": a.name})
    monkeypatch.setattr("app.services.lineage_service._edge_to_dict", lambda e: {"edge_type": e.edge_type})
    return made


def _pipeline_row(mode: str) -> dict:
    return {
        "id": "p1", "org_id": ORG, "name": "Orders", "zone": "raw", "domain": "sales", "source_system": "orders_app", "entity": "orders",
        "mode": "cdc", "version": "v1", "topic_name": SOURCE_TOPIC, "connector_name": "sales--orders_app--orders--cdc--v1",
        "direction": "SOURCE", "quality_policy_id": "dqp_1", "quality_mode": mode,
    }


@pytest.mark.asyncio
async def test_lineage_does_not_draw_a_prepared_topic_for_an_observe_pipeline(lineage) -> None:
    await lineage_service.materialize_pipeline_lineage(_pipeline_row("OBSERVE"), None)

    assert ("kafka_topic", PREPARED) not in lineage.assets
    assert not any(edge_type == "EMITS_TO" for _, edge_type, _ in lineage.edges)
    assert any(edge_type == "VALIDATED_BY" for _, edge_type, _ in lineage.edges)   # the observation itself is still shown
    assert any(edge_type == "QUARANTINES_TO" for _, edge_type, _ in lineage.edges)  # and where the failures go


@pytest.mark.asyncio
async def test_lineage_draws_raw_to_quality_to_prepared_for_an_enforce_pipeline(lineage) -> None:
    await lineage_service.materialize_pipeline_lineage(_pipeline_row("ENFORCE"), None)

    assert ("kafka_topic", PREPARED) in lineage.assets
    assert any(edge_type == "EMITS_TO" and to == PREPARED for _, edge_type, to in lineage.edges)


@pytest.mark.asyncio
async def test_changing_the_quality_mode_of_a_deployed_pipeline_also_redraws_its_lineage_in_ucs(world, fake_postgres, monkeypatch) -> None:
    """A deployed pipeline sends its lineage to UCS only on deploy; a quality policy or mode changed afterwards must redraw it there too,
    or UCS keeps showing the old flow (no quarantine / prepared topic)."""
    import app.services.pipeline_service as pipeline_module

    sent: list[dict] = []
    retired: list[list[str]] = []

    async def _emit(pipeline):
        sent.append(pipeline)

    async def _retire(org_id, names):
        retired.append(names)
    monkeypatch.setattr(pipeline_module, "emit_deploy_lineage", _emit)
    monkeypatch.setattr(pipeline_module, "retire_pipeline_lineage", _retire)
    created = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))
    fake_postgres.pipelines[created["id"]].status = "running"

    await pipeline_service.update_pipeline(ORG, created["id"], PipelineUpdate(quality_mode="ENFORCE"))
    assert [p["quality_mode"] for p in sent] == ["ENFORCE"]
    assert retired == []

    await pipeline_service.update_pipeline(ORG, created["id"], PipelineUpdate(description="unrelated"))
    assert len(sent) == 1   # an edit that touches neither quality nor the name leaves the lineage alone

    # The name is the pipeline's job in UCS: a rename retires the old job and draws the new one.
    await pipeline_service.update_pipeline(ORG, created["id"], PipelineUpdate(name="Orders v2"))
    assert retired == [["Orders", "Orders:quality"]]
    assert sent[-1]["name"] == "Orders v2"


@pytest.mark.asyncio
async def test_deleting_a_pipeline_retires_its_jobs_in_ucs(world, monkeypatch) -> None:
    """OpenLineage has no "job deleted" event, so a deleted pipeline kept its lineage in UCS forever (a deleted sink still showed as
    writing into its table). hard_delete is where both delete paths end."""
    import app.services.pipeline_service as pipeline_module

    retired: list[tuple[str, list[str]]] = []

    async def _retire(org_id, names):
        retired.append((org_id, names))
    monkeypatch.setattr(pipeline_module, "retire_pipeline_lineage", _retire)
    created = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))

    await pipeline_service.hard_delete(ORG, created["id"])
    await pipeline_service.hard_delete(ORG, created["id"])   # already gone: nothing to retire again

    assert retired == [(ORG, ["Orders", "Orders:quality", created["id"]])]


@pytest.mark.asyncio
async def test_pointing_a_deployed_sink_at_another_topic_redraws_its_lineage_in_ucs(world, fake_postgres, monkeypatch) -> None:
    """Moving a running sink to an ENFORCE source's prepared topic changed what it reads, but UCS kept showing it reading the old topic."""
    import app.services.pipeline_service as pipeline_module

    sent: list[dict] = []

    async def _emit(pipeline):
        sent.append(pipeline)
    monkeypatch.setattr(pipeline_module, "emit_deploy_lineage", _emit)
    sink = await pipeline_service.create_pipeline(ORG, _sink(world, SOURCE_TOPIC))
    fake_postgres.pipelines[sink["id"]].status = "running"

    await pipeline_service.update_pipeline(ORG, sink["id"], PipelineUpdate(connector_config={"topics": PREPARED}))

    assert [p["connector_config"]["topics"] for p in sent] == [PREPARED]


@pytest.mark.asyncio
async def test_an_edit_of_a_deployed_pipeline_stores_the_config_it_runs_not_the_preview(world, fake_postgres, monkeypatch) -> None:
    """Sinks read a producer's stored generated_config to decide how to read its topic (upsert by key, plain JSON or not). An edit used to
    overwrite a DEPLOYED pipeline's config with the creation-time preview, which has no key transform or converters: switching a running
    source to ENFORCE turned its sink's upsert into plain inserts."""
    import app.services.runtime_reconciler as reconciler

    async def _resolve(pipeline):
        return ({"connector.class": JDBC_SOURCE, "transforms": "pkkey,reroute", "transforms.pkkey.fields": "id",
                 "key.converter.schemas.enable": "true", "connection.password": "s3cret"}, "hash")
    monkeypatch.setattr(reconciler, "_resolve_config", _resolve)

    draft = await pipeline_service.create_pipeline(ORG, _source(world, quality_mode="OBSERVE"))
    edited_draft = await pipeline_service.update_pipeline(ORG, draft["id"], PipelineUpdate(description="still a draft"))
    assert "transforms.pkkey.fields" not in (edited_draft["generated_config"] or {})   # a draft keeps its preview

    fake_postgres.pipelines[draft["id"]].status = "running"
    edited = await pipeline_service.update_pipeline(ORG, draft["id"], PipelineUpdate(quality_mode="ENFORCE"))

    assert edited["generated_config"]["transforms.pkkey.fields"] == "id"
    assert edited["generated_config"]["key.converter.schemas.enable"] == "true"
    assert edited["generated_config"]["connection.password"] != "s3cret"                # redacted like a deploy's copy


@pytest.mark.asyncio
async def test_lineage_draws_no_hand_off_topic_for_an_enforce_sink(lineage) -> None:
    """An ENFORCE sink's hand-off to its own connector is internal plumbing: the graph shows the sink reading its input and writing its
    table, never the hand-off topic."""
    sink = {**_pipeline_row("ENFORCE"), "direction": "SINK", "entity": "orders_copy", "mode": "batch", "topic_name": None,
            "connector_name": "sales--orders_app--orders_copy--batch--v1", "connector_config": {"topics": PREPARED}}

    await lineage_service.materialize_pipeline_lineage(sink, None)

    assert not any(name.startswith("_dataconnect.gate.") for _, name in lineage.assets)
    assert not any(edge_type == "EMITS_TO" for _, edge_type, _ in lineage.edges)
    assert (PREPARED, "CONSUMED_BY", sink["connector_name"]) in lineage.edges



@pytest.mark.asyncio
async def test_a_sinks_lineage_shows_its_quality_check_and_quarantine_like_ucs(lineage) -> None:
    """The Lineage page matches UCS: a sink with a policy reads its input into its connector -> table, and its quality check reads the same
    input and sends failing records to the sink's quarantine topic. Never a hand-off topic."""
    quarantine = "quarantine-sales-orders_app-orders_copy-validation_sink-v1"
    sink = {**_pipeline_row("ENFORCE"), "id": "s1", "direction": "SINK", "entity": "orders_copy", "mode": "batch", "topic_name": None,
            "connector_name": "sales--orders_app--orders_copy--batch--v1", "connector_config": {"topics": PREPARED}}

    await lineage_service.materialize_pipeline_lineage(sink, None)

    quality = "Orders checks (quality policy)"            # this fixture's asset ids are their names
    assert ("processor", quality) in lineage.assets
    assert ("kafka_topic", quarantine) in lineage.assets
    assert (PREPARED, "VALIDATED_BY", quality) in lineage.edges
    assert (quality, "QUARANTINES_TO", quarantine) in lineage.edges
    assert (PREPARED, "CONSUMED_BY", sink["connector_name"]) in lineage.edges
    assert not any(name.startswith("_dataconnect.gate.") for _, name in lineage.assets)


@pytest.mark.asyncio
async def test_a_sink_without_a_policy_has_no_quality_nodes(lineage) -> None:
    sink = {**_pipeline_row("OBSERVE"), "id": "s2", "quality_policy_id": None, "direction": "SINK", "entity": "orders_copy", "mode": "batch",
            "topic_name": None, "connector_name": "sales--orders_app--orders_copy--batch--v1", "connector_config": {"topics": PREPARED}}

    await lineage_service.materialize_pipeline_lineage(sink, None)

    assert not any(kind in ("VALIDATED_BY", "QUARANTINES_TO") for _, kind, _ in lineage.edges)
    assert not any(asset_type == "processor" for asset_type, _ in lineage.assets)
