import pytest

from app.services.connect_config import DBZ_MONGO, DBZ_MYSQL, DBZ_PG, build_connector


def _pipeline(*, org_id, domain, source_system, entity, connector_name, topic_name) -> dict:
    return {
        "id": f"pipe-{entity}",
        "org_id": org_id,
        "domain": domain,
        "source_system": source_system,
        "entity": entity,
        "version": "v1",
        "topic_name": topic_name,
        "connector_name": connector_name,
        "connector_config": {},
    }


def _connection(**overrides) -> dict:
    config = {"hostname": "db.internal", "database": "salesdb", "username": "svc", "password": "hunter2"}
    config.update(overrides)
    return {"connection_config": config}


@pytest.mark.asyncio
async def test_two_pipelines_sharing_org_domain_source_get_distinct_pg_identifiers(fake_postgres) -> None:
    """The exact scenario the original bug allowed: two Postgres CDC
    pipelines under the same org/domain/source_system, differing only by
    entity, must never share a topic.prefix, slot.name, or publication.name."""
    template = {"connector_class": DBZ_PG}
    connection = _connection()

    orders_pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="pg", entity="orders",
        connector_name="raw--sales--pg--orders--cdc--v1",
        topic_name="raw-sales-pg-orders-cdc-v1",
    )
    accounts_pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="pg", entity="accounts",
        connector_name="raw--sales--pg--accounts--cdc--v1",
        topic_name="raw-sales-pg-accounts-cdc-v1",
    )

    orders_config, _ = await build_connector(orders_pipeline, connection, template, schema=None)
    accounts_config, _ = await build_connector(accounts_pipeline, connection, template, schema=None)

    assert orders_config["topic.prefix"] != accounts_config["topic.prefix"]
    assert orders_config["slot.name"] != accounts_config["slot.name"]
    assert orders_config["publication.name"] != accounts_config["publication.name"]


@pytest.mark.asyncio
async def test_two_orgs_sharing_an_entity_name_get_distinct_pg_slot_and_publication(fake_postgres) -> None:
    template = {"connector_class": DBZ_PG}
    connection = _connection()

    org_a_pipeline = _pipeline(
        org_id="org_a", domain="sales", source_system="pg", entity="customers",
        connector_name="raw--sales--pg--customers--cdc--v1",
        topic_name="raw-sales-pg-customers-cdc-v1",
    )
    org_b_pipeline = _pipeline(
        org_id="org_b", domain="support", source_system="pg", entity="customers",
        connector_name="raw--support--pg--customers--cdc--v1",
        topic_name="raw-support-pg-customers-cdc-v1",
    )

    org_a_config, _ = await build_connector(org_a_pipeline, connection, template, schema=None)
    org_b_config, _ = await build_connector(org_b_pipeline, connection, template, schema=None)

    assert org_a_config["slot.name"] != org_b_config["slot.name"]
    assert org_a_config["publication.name"] != org_b_config["publication.name"]


@pytest.mark.asyncio
async def test_pg_slot_and_publication_names_are_valid_postgres_identifiers(fake_postgres) -> None:
    template = {"connector_class": DBZ_PG}
    connection = _connection()
    pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="pg", entity="orders",
        connector_name="raw--sales--pg--orders--cdc--v1",
        topic_name="raw-sales-pg-orders-cdc-v1",
    )

    config, _ = await build_connector(pipeline, connection, template, schema=None)

    for identifier in (config["slot.name"], config["publication.name"]):
        assert len(identifier) <= 63
        assert re_all_valid_postgres_chars(identifier)


def re_all_valid_postgres_chars(identifier: str) -> bool:
    return all(c.islower() or c.isdigit() or c == "_" for c in identifier) and (
        identifier[0].isalpha() or identifier[0] == "_"
    )


@pytest.mark.asyncio
async def test_two_mysql_pipelines_get_distinct_server_ids(fake_postgres) -> None:
    """The bug this closes: every MySQL CDC pipeline previously got the
    identical hardcoded database.server.id."""
    template = {"connector_class": DBZ_MYSQL}
    connection = _connection()

    orders_pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="mysql", entity="orders",
        connector_name="raw--sales--mysql--orders--cdc--v1",
        topic_name="raw-sales-mysql-orders-cdc-v1",
    )
    accounts_pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="mysql", entity="accounts",
        connector_name="raw--sales--mysql--accounts--cdc--v1",
        topic_name="raw-sales-mysql-accounts-cdc-v1",
    )

    orders_config, _ = await build_connector(orders_pipeline, connection, template, schema=None)
    accounts_config, _ = await build_connector(accounts_pipeline, connection, template, schema=None)

    assert orders_config["database.server.id"] != accounts_config["database.server.id"]
    # A real MySQL server id, not the old literal string "184054".
    assert orders_config["database.server.id"].isdigit()
    assert 1 <= int(orders_config["database.server.id"]) <= 2**32 - 1


@pytest.mark.asyncio
async def test_identifiers_are_stable_across_repeated_builds(fake_postgres) -> None:
    """Persisted in effect: rebuilding the config for the same pipeline
    (e.g. on every redeploy) must always yield the same identifiers, or a
    PostgreSQL replication slot gets orphaned on every redeploy."""
    template = {"connector_class": DBZ_PG}
    connection = _connection()
    pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="pg", entity="orders",
        connector_name="raw--sales--pg--orders--cdc--v1",
        topic_name="raw-sales-pg-orders-cdc-v1",
    )

    first, _ = await build_connector(pipeline, connection, template, schema=None)
    second, _ = await build_connector(pipeline, connection, template, schema=None)

    assert first["slot.name"] == second["slot.name"]
    assert first["publication.name"] == second["publication.name"]
    assert first["topic.prefix"] == second["topic.prefix"]


@pytest.mark.asyncio
async def test_mongo_debezium_topic_prefix_differs_across_entities(fake_postgres) -> None:
    template = {"connector_class": DBZ_MONGO}
    connection = _connection(username=None, password=None)

    orders_pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="mongo", entity="orders",
        connector_name="raw--sales--mongo--orders--cdc--v1",
        topic_name="raw-sales-mongo-orders-cdc-v1",
    )
    accounts_pipeline = _pipeline(
        org_id="org_1", domain="sales", source_system="mongo", entity="accounts",
        connector_name="raw--sales--mongo--accounts--cdc--v1",
        topic_name="raw-sales-mongo-accounts-cdc-v1",
    )

    orders_config, _ = await build_connector(orders_pipeline, connection, template, schema=None)
    accounts_config, _ = await build_connector(accounts_pipeline, connection, template, schema=None)

    assert orders_config["topic.prefix"] != accounts_config["topic.prefix"]
