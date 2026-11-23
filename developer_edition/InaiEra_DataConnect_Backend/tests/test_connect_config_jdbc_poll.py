"""A bulk JDBC source re-reads the whole table on every poll and publishes every row again, so polling every 5 s filled the topic with copies of the
same rows. Bulk now polls every 5 minutes by default; incrementing (new rows only) keeps the short interval; both can be set per pipeline."""
import pytest

from app.services.connect_config import (
    JDBC_BULK_POLL_MS,
    JDBC_POLL_MS,
    JDBC_SOURCE,
    build_connector,
)

TEMPLATE = {"connector_class": JDBC_SOURCE, "connector_type": "source"}
CONNECTION = {"source_type": "postgresql", "connection_config": {"hostname": "db", "port": 5432, "database": "d", "username": "u", "password": "p"}}
SCHEMA_WITH_PK = {"schema_definition": {"fields": [{"name": "id", "type": "integer", "primary_key": True}]}}


def _pipeline(**cc) -> dict:
    return {
        "id": "p1", "org_id": "org_x", "domain": "d", "source_system": "pg", "entity": "products", "version": "v1", "mode": "batch", "zone": "raw",
        "topic_name": "raw-d-pg-products-batch-v1", "connector_name": "d--pg--products--batch--v1",
        "connector_config": {"tables": ["products"], **cc},
    }


async def _poll(cc: dict, schema=None) -> tuple[str, str]:
    config, _ = await build_connector(_pipeline(**cc), CONNECTION, TEMPLATE, schema=schema)
    return config["mode"], config["poll.interval.ms"]


def test_the_defaults_are_five_minutes_for_bulk_and_five_seconds_otherwise() -> None:
    assert JDBC_BULK_POLL_MS == 300_000
    assert JDBC_POLL_MS == 5_000


@pytest.mark.asyncio
async def test_a_bulk_pipeline_polls_every_five_minutes(fake_postgres) -> None:
    assert await _poll({"load_mode": "bulk"}) == ("bulk", "300000")


@pytest.mark.asyncio
async def test_an_auto_pipeline_that_falls_back_to_bulk_polls_every_five_minutes_too(fake_postgres) -> None:
    """No primary key is known, so 'auto' reads the whole table each time — the same case as an explicit bulk."""
    assert await _poll({}) == ("bulk", "300000")


@pytest.mark.asyncio
async def test_an_incrementing_pipeline_keeps_the_short_interval(fake_postgres) -> None:
    assert await _poll({"load_mode": "incrementing"}, SCHEMA_WITH_PK) == ("incrementing", "5000")


@pytest.mark.asyncio
async def test_auto_with_a_primary_key_is_incrementing_and_keeps_the_short_interval(fake_postgres) -> None:
    assert await _poll({}, SCHEMA_WITH_PK) == ("incrementing", "5000")


@pytest.mark.asyncio
@pytest.mark.parametrize("load_mode", ["bulk", "incrementing"])
async def test_the_interval_can_be_chosen_per_pipeline_and_wins_over_the_default(fake_postgres, load_mode) -> None:
    mode, poll = await _poll({"load_mode": load_mode, "poll_interval_seconds": 30}, SCHEMA_WITH_PK)
    assert mode == load_mode
    assert poll == "30000"


@pytest.mark.asyncio
async def test_a_fractional_interval_is_accepted(fake_postgres) -> None:
    assert (await _poll({"load_mode": "bulk", "poll_interval_seconds": 0.5}))[1] == "500"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "soon"])
async def test_a_bad_interval_is_refused_with_the_option_named(fake_postgres, bad) -> None:
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        await _poll({"load_mode": "bulk", "poll_interval_seconds": bad})


@pytest.mark.asyncio
async def test_a_blank_interval_means_use_the_default(fake_postgres) -> None:
    assert await _poll({"load_mode": "bulk", "poll_interval_seconds": ""}) == ("bulk", "300000")
