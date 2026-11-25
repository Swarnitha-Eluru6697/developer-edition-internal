import base64
import json

import httpx
import pytest

from app.core.config import settings
from app.services import ucs_lineage_sync


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


@pytest.fixture(autouse=True)
def _enable_sync(monkeypatch):
    """Every test in this module exercises the sync path explicitly, so the
    opt-in flag (default False in production) is turned on for the duration
    of each test and restored after."""
    monkeypatch.setattr(settings, "ucs_lineage_sync_enabled", True)


@pytest.fixture(autouse=True)
def _postgres_connections_and_no_pipeline_lookup(monkeypatch):
    """Every pipeline here lives on a Postgres connection unless a test says otherwise, and a runtime edge's pipeline is unknown (forwarded
    as it comes) unless a test provides one — neither touches a database."""
    async def _system(org_id, connection_profile_id):
        return "postgresql"

    async def _no_pipeline(org_id, pipeline_id):
        return None
    monkeypatch.setattr(ucs_lineage_sync, "_connection_system", _system)
    monkeypatch.setattr(ucs_lineage_sync, "_pipeline_for", _no_pipeline)


def _mock_get_client(monkeypatch, handler):
    """Same MockTransport pattern as test_runtime_orchestrator.py: patch the
    shared-client factory so no real network call is ever made, and capture
    every request the handler sees."""
    requests: list[httpx.Request] = []

    def _capturing_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    shared_client = httpx.AsyncClient(
        base_url="http://ucs:8000", transport=httpx.MockTransport(_capturing_handler)
    )

    async def _fake_get_client(base_url: str):
        assert base_url == settings.ucs_base_url
        return shared_client

    monkeypatch.setattr("app.services.ucs_lineage_sync.get_client", _fake_get_client)
    return requests, shared_client


ROUTING_POLICY = {"id": "dqp_1", "name": "checks", "on_failure": "route_to_quarantine", "quarantine_sink": "kafka"}


def _mock_quality_policy(monkeypatch, policy):
    from app.services.quality_service import quality_service

    async def _get_policy(org_id, policy_id):
        return policy
    monkeypatch.setattr(quality_service, "get_policy", _get_policy)


@pytest.mark.asyncio
async def test_emit_deploy_lineage_sends_source_and_its_own_topic_when_no_quality_policy(
    monkeypatch,
) -> None:
    """No quality_policy_id -> a single hop, exactly the physical Kafka
    Connect flow: the source connector writes straight to its own advertised
    topic. No "prepared" companion topic is emitted, since nothing ever
    produces to one in this case (see _pipeline_has_quality_routing)."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline = {
        "id": "pipe_1", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity",
        "topic_name": "raw__sales__opportunity",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert len(requests) == 1
        body = _body(requests[0])
        assert body["job"] == {"namespace": "dataconnect", "name": "sales_ingest"}
        assert body["run"]["runId"].startswith("deploy-pipe_1-")
        assert body["inputs"] == [
            {"namespace": "dataconnect:source:postgresql", "name": "opportunity"}
        ]
        assert body["outputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "raw__sales__opportunity"},
        ]
        assert requests[0].headers["X-Tenant-Id"] == "org_1"
        _hdr = requests[0].headers
        assert "x-user-roles" not in {k.lower() for k in _hdr.keys()}
        assert "x-user-role" not in {k.lower() for k in _hdr.keys()}
        assert json.loads(base64.b64decode(_hdr["X-User-Permissions"])) == ['ucs:lineage-events:create']
        assert _hdr["X-User-Name"] == settings.ucs_lineage_sync_user
        assert "X-Inaiera-Gateway-Secret" in _hdr
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_source_hop_is_unchanged_by_a_quality_policy_and_the_quality_hop_follows(
    monkeypatch,
) -> None:
    """A quality_policy_id never changes the CONNECTOR's data flow (see connect_config.py and quality_stream_worker.py's own docstrings:
    quality checking reads the topic, it is not a router) — the source still feeds its own topic directly. What a policy adds is the
    worker's own hop, sent as a separate job: the topic feeds the validation quarantine topic its failing records are copied to."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    _mock_quality_policy(monkeypatch, ROUTING_POLICY)
    pipeline = {
        "id": "pipe_1", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity", "domain": "crm",
        "version": "v1", "quality_policy_id": "dqp_1", "zone": "raw",
        "topic_name": "raw__crm__salesforce__opportunity__api__v1",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert len(requests) == 2
        body = _body(requests[0])
        assert body["job"] == {"namespace": "dataconnect", "name": "sales_ingest"}
        assert body["outputs"] == [
            {
                "namespace": "dataconnect:kafka_topic",
                "name": "raw__crm__salesforce__opportunity__api__v1",
            }
        ]
        quality = _body(requests[1])
        assert quality["job"] == {"namespace": "dataconnect", "name": "sales_ingest:quality"}
        assert quality["inputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "raw__crm__salesforce__opportunity__api__v1"}
        ]
        assert quality["outputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "quarantine-crm-salesforce-opportunity-validation-v1"}
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_sink_hop_is_unchanged_by_a_quality_policy_and_the_quality_hop_reads_its_input(
    monkeypatch,
) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    _mock_quality_policy(monkeypatch, ROUTING_POLICY)
    pipeline = {
        "id": "pipe_2", "org_id": "org_1", "name": "psql_sink",
        "source_system": "postgres", "entity": "products_copy", "domain": "db",
        "version": "v1", "quality_policy_id": "dqp_1", "zone": "raw",
        "topic_name": "raw__db__postgres__products_copy",
        "connector_config": {"topics": "raw__db__postgres__products", "tasks.max": "1"},
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert len(requests) == 2
        body = _body(requests[0])
        assert body["job"] == {"namespace": "dataconnect", "name": "psql_sink"}
        assert body["inputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "raw__db__postgres__products"}
        ]
        assert body["outputs"] == [
            {"namespace": "dataconnect:sink:postgresql", "name": "products_copy"}
        ]
        quality = _body(requests[1])
        assert quality["job"]["name"] == "psql_sink:quality"
        assert quality["inputs"] == body["inputs"]   # the worker checks what the sink reads
        assert quality["outputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "quarantine-db-postgres-products_copy-validation-v1"}
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_single_hop_on_a_quarantine_zone_pipeline(
    monkeypatch,
) -> None:
    """A pipeline whose own zone IS "quarantine" behaves identically to
    every other zone now (single hop) — this used to be the one exception
    the quality-routing feature special-cased; that special case is gone
    along with the routing itself."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline = {
        "id": "pipe_3", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity", "domain": "crm",
        "version": "v1", "quality_policy_id": "dqp_1", "zone": "quarantine",
        "topic_name": "quarantine__crm__salesforce__opportunity",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert len(requests) == 1
        assert _body(requests[0])["outputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "quarantine__crm__salesforce__opportunity"},
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_sends_input_topic_and_sink_for_a_sink_pipeline(
    monkeypatch,
) -> None:
    """A pipeline is a sink iff its connector_config carries "topics" (the
    same signal connect_config.py's _sink_topic() relies on) — it must link
    from the topic it reads to the destination it writes, not the
    source/topic shape a source pipeline emits."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline = {
        "id": "pipe_2", "org_id": "org_1", "name": "psql_sink",
        "source_system": "postgres", "entity": "products_copy",
        "topic_name": "raw__db__postgres__products_copy__batch__v1",
        "connector_config": {"topics": "raw__db__postgres__products__batch__v1", "tasks.max": "1"},
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert len(requests) == 1
        body = _body(requests[0])
        assert body["job"] == {"namespace": "dataconnect", "name": "psql_sink"}
        assert body["inputs"] == [
            {
                "namespace": "dataconnect:kafka_topic",
                "name": "raw__db__postgres__products__batch__v1",
            }
        ]
        assert body["outputs"] == [
            {"namespace": "dataconnect:sink:postgresql", "name": "products_copy"}
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_names_the_source_dataset_after_the_real_table(monkeypatch) -> None:
    """The dataset name is the pipeline's own entity — its real source table
    (connect_config.py's JDBC source reads app/schemas/pipeline.py's own
    `entity` field, never the connection's display name) — because UCS's
    admin-configured lineage namespace mapping resolves
    "dataconnect:source:{org}" under the real Postgres catalog + schema that
    table actually lives in (e.g. dataconnect_postgres.public.<entity>), the
    SAME FQN Gravitino itself uses for it. A synthetic, connection-prefixed
    name would point at a node nothing else in UCS recognizes, so the
    table's own Explore-browsed profile page could never show the lineage
    UCS had actually recorded for it."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline = {
        "id": "pipe_1", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity",
        "topic_name": "raw__sales__opportunity",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        body = _body(requests[0])
        assert body["inputs"] == [
            {"namespace": "dataconnect:source:postgresql", "name": "opportunity"}
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_distinguishes_two_sinks_by_their_own_entity(monkeypatch) -> None:
    """The bug this closes: two sink pipelines through the same connection
    profile but different destination tables used to both resolve to the
    identical "dataconnect:sink:{org}"/"<connection name>" dataset in UCS —
    its lineage graph walks by dataset id, so only one of several real sinks
    ever showed up as a topic's downstream consumer. Naming by entity (the
    actual destination table) keeps them distinct."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline_a = {
        "id": "pipe_a", "org_id": "org_1", "name": "sink_a",
        "source_system": "postgres", "entity": "products_copy",
        "topic_name": "prepared__db__postgres__products_copy__batch__v1",
        "connector_config": {"topics": "raw__db__postgres__products__batch__v1"},
    }
    pipeline_b = {
        "id": "pipe_b", "org_id": "org_1", "name": "sink_b",
        "source_system": "postgres", "entity": "products_second_sink",
        "topic_name": "prepared__db__postgres__products_second_sink__batch__v1",
        "connector_config": {"topics": "raw__db__postgres__products__batch__v1"},
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline_a)
        await ucs_lineage_sync.emit_deploy_lineage(pipeline_b)

        name_a = _body(requests[0])["outputs"][0]["name"]
        name_b = _body(requests[1])["outputs"][0]["name"]
        assert name_a == "products_copy"
        assert name_b == "products_second_sink"
        assert name_a != name_b
    finally:
        await shared_client.aclose()


def _mock_schema_and_transform_services(monkeypatch, *, schema=None, policy=None):
    """Patches the two service singletons ucs_lineage_sync reads schema/
    transform data through, scoped to this module's namespace only — the
    real singletons used elsewhere are untouched."""

    class _FakeSchemaService:
        async def get_schema(self, org_id: str, schema_id: str):
            return schema

    class _FakeTransformService:
        async def get_policy(self, org_id: str, policy_id: str):
            return policy

    monkeypatch.setattr("app.services.ucs_lineage_sync.schema_service", _FakeSchemaService())
    monkeypatch.setattr("app.services.ucs_lineage_sync.transform_service", _FakeTransformService())


@pytest.mark.asyncio
async def test_emit_deploy_lineage_attaches_schema_and_column_lineage_facets(monkeypatch) -> None:
    """A source pipeline with a registered SchemaVersion (autocreated or
    hand-registered — same dc_schema_versions row either way) carries
    `schema` + `columnLineage` facets on its raw-topic output, with every
    field IDENTITY from the same-named upstream field when no transform
    masks it."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    _mock_schema_and_transform_services(
        monkeypatch,
        schema={"schema_definition": {"fields": [
            {"name": "product_id", "type": "integer", "primary_key": True},
            {"name": "product_name", "type": "string"},
        ]}},
    )
    pipeline = {
        "id": "pipe_3", "org_id": "org_1", "name": "products_ingest",
        "source_system": "postgres", "entity": "products",
        "topic_name": "raw__db__postgres__products__batch__v1",
        "schema_version_id": "sv_1",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        body = _body(requests[0])
        raw_output = body["outputs"][0]
        assert raw_output["name"] == "raw__db__postgres__products__batch__v1"
        facets = raw_output["facets"]
        assert list(facets.keys()) == ["columnLineage"]
        col = facets["columnLineage"]["fields"]
        assert col["product_id"]["inputFields"] == [
            {
                "namespace": "dataconnect:source:postgresql",
                "name": "products",
                "field": "product_id",
                "transformations": [{"type": "IDENTITY", "masking": False}],
            }
        ]
        assert col["product_name"]["inputFields"][0]["transformations"] == [
            {"type": "IDENTITY", "masking": False}
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_source_with_quality_policy_still_reaches_column_lineage(
    monkeypatch,
) -> None:
    """Column lineage is unaffected by quality_policy_id — it's the same
    single-hop event either way (see the "no quality policy" version of this
    assertion above); this just confirms attaching a policy doesn't change
    that."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    _mock_schema_and_transform_services(
        monkeypatch,
        schema={"schema_definition": {"fields": [
            {"name": "product_id", "type": "integer", "primary_key": True},
            {"name": "stock_quantity", "type": "integer"},
        ]}},
    )
    pipeline = {
        "id": "pipe_5", "org_id": "org_1", "name": "products_ingest",
        "source_system": "postgres", "entity": "products", "domain": "db",
        "version": "v1", "quality_policy_id": "dqp_1", "zone": "raw",
        "topic_name": "raw__db__postgres__products__batch__v1",
        "schema_version_id": "sv_1",
    }
    _mock_quality_policy(monkeypatch, ROUTING_POLICY)

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert len(requests) == 2   # the connector hop (with its column lineage) and the quality hop
        body = _body(requests[0])
        raw_output = body["outputs"][0]
        assert raw_output["name"] == "raw__db__postgres__products__batch__v1"
        col = raw_output["facets"]["columnLineage"]["fields"]
        assert col["product_id"]["inputFields"][0]["field"] == "product_id"
        assert col["stock_quantity"]["inputFields"][0]["field"] == "stock_quantity"
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_marks_masked_fields_from_the_transform_policy(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    _mock_schema_and_transform_services(
        monkeypatch,
        schema={"schema_definition": {"fields": [
            {"name": "customer_id", "type": "integer"},
            {"name": "email", "type": "string"},
            {"name": "phone", "type": "string"},
        ]}},
        policy={"transforms": [
            {"order": 1, "name": "maskPII", "type": "MaskField", "scope": "value", "config": {"fields": "email,phone"}},
        ]},
    )
    pipeline = {
        "id": "pipe_4", "org_id": "org_1", "name": "customers_ingest",
        "source_system": "postgres", "entity": "customers",
        "topic_name": "raw__db__postgres__customers__batch__v1",
        "schema_version_id": "sv_2", "transform_policy_id": "tp_1",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        col = _body(requests[0])["outputs"][0]["facets"]["columnLineage"]["fields"]
        assert col["customer_id"]["inputFields"][0]["transformations"] == [
            {"type": "IDENTITY", "masking": False}
        ]
        assert col["email"]["inputFields"][0]["transformations"] == [{"type": "MASK", "masking": True}]
        assert col["phone"]["inputFields"][0]["transformations"] == [{"type": "MASK", "masking": True}]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_omits_facets_when_pipeline_has_no_schema_yet(monkeypatch) -> None:
    """autocreate_schema_from_source didn't run (source unreachable) or the
    pipeline was never linked to a schema — the raw output must still be
    sent, just without a facets key, exactly like before this feature."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline = {
        "id": "pipe_1", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity",
        "topic_name": "raw__sales__opportunity",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert "facets" not in _body(requests[0])["outputs"][0]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_is_a_noop_when_sync_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ucs_lineage_sync_enabled", False)
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    pipeline = {
        "id": "pipe_1", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity",
        "topic_name": "raw__sales__opportunity",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)

        assert requests == []
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_deploy_lineage_swallows_a_ucs_outage(monkeypatch) -> None:
    """A pipeline deploy must never fail because UCS happens to be down."""
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    requests, shared_client = _mock_get_client(monkeypatch, _raise)
    pipeline = {
        "id": "pipe_1", "org_id": "org_1", "name": "sales_ingest",
        "source_system": "salesforce", "entity": "opportunity",
        "topic_name": "raw__sales__opportunity",
    }

    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)  # must not raise
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_runtime_edge_lineage_forwards_a_sink_write(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    event = {
        "org_id": "org_1", "pipeline_id": "pipe_1", "edge_type": "WRITTEN_TO",
        "run_id": "run-42",
        "from_asset": {"asset_type": "kafka_topic", "name": "prepared__sales__opportunity"},
        "to_asset": {"asset_type": "sink", "name": "warehouse.public.opportunity"},
    }

    try:
        await ucs_lineage_sync.emit_runtime_edge_lineage(event)

        assert len(requests) == 1
        body = _body(requests[0])
        assert body["run"] == {"runId": "run-42"}
        assert body["inputs"] == [
            {"namespace": "dataconnect:kafka_topic", "name": "prepared__sales__opportunity"}
        ]
        assert body["outputs"] == [
            {"namespace": "dataconnect:sink", "name": "warehouse.public.opportunity"}
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_emit_runtime_edge_lineage_skips_edges_touching_a_connector_or_processor(
    monkeypatch,
) -> None:
    """CONNECTS_TO / INGESTS_TO / VALIDATED_BY / EMITS_TO edges involve a
    connector or processor node — pipeline plumbing UCS has no catalog
    object for — and must never be forwarded."""
    requests, shared_client = _mock_get_client(
        monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"})
    )
    event = {
        "org_id": "org_1", "pipeline_id": "pipe_1", "edge_type": "INGESTS_TO",
        "from_asset": {"asset_type": "connector", "name": "pg_sales_connector"},
        "to_asset": {"asset_type": "kafka_topic", "name": "raw__sales__opportunity"},
    }

    try:
        await ucs_lineage_sync.emit_runtime_edge_lineage(event)

        assert requests == []
    finally:
        await shared_client.aclose()


def test_ucs_service_headers_carry_one_permission_and_no_role_marker(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ucs_gateway_secret", "ucs-secret")
    monkeypatch.setattr(settings, "ucs_lineage_sync_user", "dataconnect-sync")
    for permission in (ucs_lineage_sync.LINEAGE_EVENTS_PERMISSION, ucs_lineage_sync.QUALITY_RESULTS_PERMISSION):
        headers = ucs_lineage_sync.ucs_service_headers("org_1", permission)
        assert {k.lower() for k in headers} == {"x-tenant-id", "x-user-name", "x-user-groups", "x-user-permissions", "x-inaiera-gateway-secret"}
        assert json.loads(base64.b64decode(headers["X-User-Permissions"])) == [permission]
        assert headers["X-Inaiera-Gateway-Secret"] == "ucs-secret"
        assert headers["X-User-Name"] == "dataconnect-sync"
    assert ucs_lineage_sync.LINEAGE_EVENTS_PERMISSION == "ucs:lineage-events:create"
    assert ucs_lineage_sync.QUALITY_RESULTS_PERMISSION == "ucs:quality-results:create"


@pytest.mark.asyncio
async def test_the_quality_hop_of_an_enforce_pipeline_also_feeds_its_prepared_topic(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    _mock_quality_policy(monkeypatch, ROUTING_POLICY)
    pipeline = {
        "id": "pipe_e", "org_id": "org_1", "name": "orders_src", "source_system": "pg", "entity": "orders", "domain": "shop",
        "mode": "batch", "version": "v1", "quality_policy_id": "dqp_1", "quality_mode": "ENFORCE", "zone": "raw",
        "topic_name": "raw-shop-pg-orders-batch-v1",
    }
    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)
        assert [o["name"] for o in _body(requests[1])["outputs"]] == [
            "quarantine-shop-pg-orders-validation-v1", "prepared-shop-pg-orders-batch-v1"]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_no_quality_hop_when_an_observe_policy_quarantines_nothing_to_kafka(monkeypatch) -> None:
    """OBSERVE with a policy that does not route to the Kafka quarantine: the worker writes no topic, so there is no hop to show."""
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    _mock_quality_policy(monkeypatch, {**ROUTING_POLICY, "on_failure": "log_only"})
    pipeline = {
        "id": "pipe_o", "org_id": "org_1", "name": "orders_src", "source_system": "pg", "entity": "orders", "domain": "shop",
        "mode": "batch", "version": "v1", "quality_policy_id": "dqp_1", "zone": "raw", "topic_name": "raw-shop-pg-orders-batch-v1",
    }
    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)
        assert len(requests) == 1
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_a_failing_policy_lookup_never_fails_the_deploy(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    from app.services.quality_service import quality_service

    async def _boom(org_id, policy_id):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(quality_service, "get_policy", _boom)
    pipeline = {
        "id": "pipe_f", "org_id": "org_1", "name": "orders_src", "source_system": "pg", "entity": "orders", "domain": "shop",
        "mode": "batch", "version": "v1", "quality_policy_id": "dqp_1", "zone": "raw", "topic_name": "raw-shop-pg-orders-batch-v1",
    }
    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)   # must not raise
        assert len(requests) == 1   # the connector hop still went out
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_every_run_id_fits_ucs_80_character_column(monkeypatch) -> None:
    """UCS stores runId in ol_run.run_id VARCHAR(80); a longer one makes its ingest worker drop the whole event (seen live: the quality hop's
    first run id was 88 characters and never reached the graph). Pipeline ids are UUIDs, so this is the realistic length."""
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    _mock_quality_policy(monkeypatch, ROUTING_POLICY)
    pipeline = {
        "id": "0bf42782-e221-45eb-b3d8-368434859327", "org_id": "org_1", "name": "orders_src", "source_system": "pg",
        "entity": "orders", "domain": "shop", "mode": "batch", "version": "v1", "quality_policy_id": "dqp_1", "quality_mode": "ENFORCE",
        "zone": "raw", "topic_name": "raw-shop-pg-orders-batch-v1",
    }
    try:
        await ucs_lineage_sync.emit_deploy_lineage(pipeline)
        assert len(requests) == 2
        assert all(len(_body(r)["run"]["runId"]) <= 80 for r in requests)
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_a_sink_names_its_own_system_so_ucs_can_map_each_system_on_its_own(monkeypatch) -> None:
    """One namespace per org, mapped to the Postgres catalog, made an Aerospike sink of "products" resolve to the Postgres table
    `products` and show up in UCS as writing into it. The namespace carries the connection's system now."""
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))

    async def _system(org_id, connection_profile_id):
        return {"c_pg": "postgresql", "c_as": "aerospike"}[connection_profile_id]
    monkeypatch.setattr(ucs_lineage_sync, "_connection_system", _system)
    base = {"org_id": "org_1", "source_system": "pg", "domain": "shop", "version": "v1", "zone": "raw", "entity": "products",
            "connector_config": {"topics": "raw-shop-pg-products-batch-v1"}}
    try:
        await ucs_lineage_sync.emit_deploy_lineage({**base, "id": "p1", "name": "to-postgres", "connection_profile_id": "c_pg"})
        await ucs_lineage_sync.emit_deploy_lineage({**base, "id": "p2", "name": "to-aerospike", "connection_profile_id": "c_as"})
        assert [_body(r)["outputs"] for r in requests] == [
            [{"namespace": "dataconnect:sink:postgresql", "name": "products"}],
            [{"namespace": "dataconnect:sink:aerospike", "name": "products"}],
        ]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_the_system_is_the_connections_source_type(monkeypatch) -> None:
    from app.services.connection_service import connection_service

    monkeypatch.undo()   # the real _connection_system, not this module's stand-in
    connections = {"c1": {"source_type": "PostgreSQL"}, "c2": {"source_type": "google bigquery"}, "c3": {}}

    async def _get(org_id, connection_id):
        return connections.get(connection_id)
    monkeypatch.setattr(connection_service, "get_connection", _get)
    assert await ucs_lineage_sync._connection_system("org_1", "c1") == "postgresql"
    assert await ucs_lineage_sync._connection_system("org_1", "c2") == "google_bigquery"
    assert await ucs_lineage_sync._connection_system("org_1", "c3") == "unknown"
    assert await ucs_lineage_sync._connection_system("org_1", "missing") == "unknown"
    assert await ucs_lineage_sync._connection_system("org_1", None) == "unknown"


@pytest.mark.asyncio
async def test_a_runtime_edge_uses_the_pipelines_job_table_and_system(monkeypatch) -> None:
    """A runtime source asset is named after the CONNECTION ("Postgres") and the edge's job was the pipeline's id: in UCS that was a
    dataset nobody else names and a job no delete would ever retire. With its pipeline known, the edge is the deploy lineage's own."""
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))

    async def _pipeline(org_id, pipeline_id):
        return {"id": pipeline_id, "name": "products-quality-src", "entity": "products", "connection_profile_id": "c_pg"}
    monkeypatch.setattr(ucs_lineage_sync, "_pipeline_for", _pipeline)
    event = {
        "org_id": "org_1", "pipeline_id": "pipe_7", "run_id": "run-7",
        "from_asset": {"asset_type": "source", "name": "Postgres"},
        "to_asset": {"asset_type": "kafka_topic", "name": "raw-shop-pg-products-batch-v1"},
    }
    try:
        await ucs_lineage_sync.emit_runtime_edge_lineage(event)
        body = _body(requests[0])
        assert body["job"] == {"namespace": "dataconnect", "name": "products-quality-src"}
        assert body["inputs"] == [{"namespace": "dataconnect:source:postgresql", "name": "products"}]
        assert body["outputs"] == [{"namespace": "dataconnect:kafka_topic", "name": "raw-shop-pg-products-batch-v1"}]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_retiring_a_pipelines_jobs_tells_ucs_with_the_producer_permission(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(200, json={"retired_edges": 2}))
    try:
        names = ucs_lineage_sync.lineage_job_names({"id": "pipe_7", "name": "products-sink"})
        await ucs_lineage_sync.retire_pipeline_lineage("org_1", names)
        assert requests[0].url.path == "/api/v1/lineage/jobs/retire"
        assert _body(requests[0]) == {"namespace": "dataconnect", "names": ["products-sink", "products-sink:quality", "pipe_7"]}
        assert json.loads(base64.b64decode(requests[0].headers["X-User-Permissions"])) == ["ucs:lineage-events:create"]
        assert requests[0].headers["X-Tenant-Id"] == "org_1"
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_retiring_never_raises_and_is_off_with_the_sync(monkeypatch) -> None:
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(503))
    try:
        await ucs_lineage_sync.retire_pipeline_lineage("org_1", ["a"])        # UCS down: logged, not raised
        monkeypatch.setattr(settings, "ucs_lineage_sync_enabled", False)
        await ucs_lineage_sync.retire_pipeline_lineage("org_1", ["a"])
        assert len(requests) == 1
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_a_sink_with_a_dead_letter_queue_also_writes_its_dead_letter_topic(monkeypatch) -> None:
    """Kafka Connect writes a record the sink cannot process to the sink's dead-letter topic, so that topic is one of the sink's outputs.
    Without it the topic stood alone in UCS: no upstream, no downstream."""
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    dlq = "quarantine-shop-pg-products_sink-connector-v1"
    base = {"id": "p1", "name": "products-sink", "org_id": "org_1", "source_system": "pg", "domain": "shop", "version": "v1", "zone": "raw",
            "entity": "products_sink", "connector_config": {"topics": "prepared-shop-pg-products-batch-v1"}}
    try:
        await ucs_lineage_sync.emit_deploy_lineage({**base, "generated_config": {"errors.deadletterqueue.topic.name": dlq}})
        await ucs_lineage_sync.emit_deploy_lineage({**base, "generated_config": {"errors.tolerance": "none"}})   # no DLQ (fail fast)
        assert _body(requests[0])["outputs"] == [
            {"namespace": "dataconnect:sink:postgresql", "name": "products_sink"},
            {"namespace": "dataconnect:kafka_topic", "name": dlq},
        ]
        assert _body(requests[1])["outputs"] == [{"namespace": "dataconnect:sink:postgresql", "name": "products_sink"}]
    finally:
        await shared_client.aclose()


@pytest.mark.asyncio
async def test_an_enforce_sink_is_drawn_input_to_table_with_its_failures_to_quarantine(monkeypatch) -> None:
    """An ENFORCE sink's gate hands passing records to its own connector through an internal topic that is not part of the data's
    lineage (quality_mode.SINK_HANDOFF_PREFIX): the sink reads its input and writes the table, and its failures go to its quarantine."""
    requests, shared_client = _mock_get_client(monkeypatch, lambda r: httpx.Response(202, json={"status": "accepted"}))
    _mock_quality_policy(monkeypatch, ROUTING_POLICY)
    sink = {"id": "p9", "name": "products-sink-enforce", "org_id": "org_1", "source_system": "pg", "domain": "shop", "mode": "stream",
            "version": "v1", "zone": "raw", "entity": "products_sink_enforce", "quality_policy_id": "dqp_1", "quality_mode": "ENFORCE",
            "direction": "SINK", "connector_config": {"topics": "prepared-shop-pg-products-batch-v1"}}
    try:
        await ucs_lineage_sync.emit_deploy_lineage(sink)
        connector, quality = _body(requests[0]), _body(requests[1])
        assert connector["inputs"] == [{"namespace": "dataconnect:kafka_topic", "name": "prepared-shop-pg-products-batch-v1"}]
        assert connector["outputs"][0] == {"namespace": "dataconnect:sink:postgresql", "name": "products_sink_enforce"}
        assert quality["inputs"] == connector["inputs"]
        assert [o["name"] for o in quality["outputs"]] == ["quarantine-shop-pg-products_sink_enforce-validation_sink-v1"]
        assert not any("_dataconnect.gate" in o["name"] for r in requests for o in _body(r)["outputs"])
    finally:
        await shared_client.aclose()
