"""Every connector the platform builds ran exactly one task, so the Monitoring page could only ever show "Task 1". A pipeline can now set
`tasks_max`, but only for the connectors that really divide their work across tasks (JDBC source: one per table, JDBC sink: one per topic
partition); every other connector refuses more than one."""
import pytest

from app.services.connect_config import DBZ_PG, JDBC_SINK, JDBC_SOURCE, MAX_TASKS, build_connector

CONNECTION = {"source_type": "postgresql", "connection_config": {"hostname": "db", "port": 5432, "database": "d", "username": "u", "password": "p"}}
SOURCE_TEMPLATE = {"connector_class": JDBC_SOURCE, "connector_type": "source"}
SINK_TEMPLATE = {"connector_class": JDBC_SINK, "connector_type": "sink"}
CDC_TEMPLATE = {"connector_class": DBZ_PG, "connector_type": "source"}


def _source(**cc) -> dict:
    return {
        "id": "p1", "org_id": "org_x", "domain": "d", "source_system": "pg", "entity": "products", "version": "v1", "mode": "batch", "zone": "raw",
        "topic_name": "raw-d-pg-products-batch-v1", "connector_name": "d--pg--products--batch--v1",
        "connector_config": {"tables": ["products", "orders", "items"], **cc},
    }


def _sink(**cc) -> dict:
    return {
        "id": "p2", "org_id": "org_x", "domain": "d", "source_system": "pg", "entity": "products_copy", "version": "v1", "mode": "batch",
        "zone": "prepared", "direction": "SINK", "connector_name": "d--pg--products_copy--batch--v1",
        "connector_config": {"topics": "raw-d-pg-products-batch-v1", **cc},
    }


async def _tasks(pipeline: dict, template: dict) -> str:
    config, _ = await build_connector(pipeline, CONNECTION, template, schema=None)
    return config["tasks.max"]


@pytest.mark.asyncio
async def test_without_the_option_every_connector_keeps_one_task(fake_postgres) -> None:
    assert await _tasks(_source(), SOURCE_TEMPLATE) == "1"
    assert await _tasks(_sink(), SINK_TEMPLATE) == "1"
    assert await _tasks(_source(tasks_max=""), SOURCE_TEMPLATE) == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", [3, "3", 3.0, "3.0"])
async def test_a_jdbc_source_and_a_jdbc_sink_accept_more_tasks(fake_postgres, asked) -> None:
    assert await _tasks(_source(tasks_max=asked), SOURCE_TEMPLATE) == "3"
    assert await _tasks(_sink(tasks_max=asked), SINK_TEMPLATE) == "3"


@pytest.mark.asyncio
async def test_the_limits_are_one_to_sixteen(fake_postgres) -> None:
    assert await _tasks(_sink(tasks_max=1), SINK_TEMPLATE) == "1"
    assert await _tasks(_sink(tasks_max=MAX_TASKS), SINK_TEMPLATE) == str(MAX_TASKS)
    for bad in (0, -2, MAX_TASKS + 1):
        with pytest.raises(ValueError, match="between 1 and 16"):
            await _tasks(_sink(tasks_max=bad), SINK_TEMPLATE)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["many", "2.5", [2], {"n": 2}, "inf", "nan"])
async def test_a_value_that_is_not_a_whole_number_is_refused(fake_postgres, bad) -> None:
    with pytest.raises(ValueError, match="whole number"):
        await _tasks(_source(tasks_max=bad), SOURCE_TEMPLATE)


@pytest.mark.asyncio
async def test_a_connector_that_cannot_split_its_work_refuses_more_than_one_task(fake_postgres) -> None:
    # Debezium reads one replication stream: more than one task would only fail at start-up.
    with pytest.raises(ValueError, match="only supported for the JDBC source and JDBC sink"):
        await _tasks(_source(tasks_max=2), CDC_TEMPLATE)
    assert await _tasks(_source(tasks_max=1), CDC_TEMPLATE) == "1"
