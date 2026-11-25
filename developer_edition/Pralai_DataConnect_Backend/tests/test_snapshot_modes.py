"""
The CDC "Snapshot mode" a pipeline deploys with must be one its Debezium connector accepts. Found in the 2026-10-28 large-data test:
the form offered one list for every Debezium connector, PostgreSQL + "no_data" ("stream changes only") was sent as-is, and Kafka Connect
(Debezium 2.5.4) rejected it — the user only saw "Kafka Connect rejected the connector configuration." with no setting named.
"""

import pytest

from app.core.errors import KafkaConnectConfigError
from app.services.connect_config import (
    DBZ_MONGO,
    DBZ_MSSQL,
    DBZ_MYSQL,
    DBZ_ORACLE,
    DBZ_PG,
    SNAPSHOT_MODES_BY_CLASS,
    build_connector,
)
from app.services.safe_error import to_safe_error


def _pipeline(mode: str | None) -> dict:
    cc = {} if mode is None else {"snapshot_mode": mode}
    return {
        "id": "pipe-1", "org_id": "org_1", "domain": "sales", "source_system": "pg", "entity": "orders", "version": "v1",
        "topic_name": "raw-sales-pg-orders-cdc-v1", "connector_name": "sales--pg--orders--cdc--v1", "connector_config": cc,
    }


def _connection() -> dict:
    return {"connection_config": {"hostname": "db.internal", "port": 5432, "database": "salesdb", "username": "svc", "password": "pw"}}


async def _mode(klass: str, mode: str | None) -> str:
    config, _ = await build_connector(_pipeline(mode), _connection(), {"connector_class": klass}, schema=None)
    return config["snapshot.mode"]


@pytest.mark.asyncio
async def test_stream_only_no_data_deploys_as_what_debezium_2_5_calls_it(fake_postgres) -> None:
    assert await _mode(DBZ_PG, "no_data") == "never"
    assert await _mode(DBZ_MONGO, "no_data") == "never"
    assert await _mode(DBZ_MSSQL, "no_data") == "schema_only"
    assert await _mode(DBZ_ORACLE, "no_data") == "schema_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(("klass", "mode"), [(DBZ_PG, "when_needed"), (DBZ_PG, "configuration_based"), (DBZ_PG, "custom"),
                                        (DBZ_MONGO, "always"), (DBZ_MSSQL, "always"), (DBZ_ORACLE, "never")])
async def test_a_mode_the_connector_rejects_is_refused_with_the_modes_that_work(fake_postgres, klass, mode) -> None:
    with pytest.raises(ValueError, match="is not supported") as err:
        await _mode(klass, mode)
    assert f"'{mode}' is not supported" in str(err.value)
    assert ", ".join(SNAPSHOT_MODES_BY_CLASS[klass]) in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("klass", [DBZ_PG, DBZ_MYSQL, DBZ_MSSQL, DBZ_ORACLE, DBZ_MONGO])
async def test_every_listed_mode_deploys_unchanged_and_the_default_is_initial(fake_postgres, klass) -> None:
    for mode in SNAPSHOT_MODES_BY_CLASS[klass]:
        assert await _mode(klass, mode) == mode
    assert await _mode(klass, None) == "initial"


@pytest.mark.asyncio
async def test_a_value_that_is_no_snapshot_mode_at_all_still_falls_back_to_initial(fake_postgres) -> None:
    # As before: an old or mistyped row must not start failing deploys now.
    assert await _mode(DBZ_PG, "bogus") == "initial"


def test_lists_match_what_the_installed_debezium_accepts() -> None:
    # Taken from Kafka Connect's validator for the installed 2.5.4 plugins (custom left out: it needs a class the form can't set).
    assert set(SNAPSHOT_MODES_BY_CLASS[DBZ_PG]) == {"always", "never", "initial_only", "initial"}
    assert set(SNAPSHOT_MODES_BY_CLASS[DBZ_MSSQL]) == {"initial_only", "initial", "schema_only"}
    assert set(SNAPSHOT_MODES_BY_CLASS[DBZ_ORACLE]) == {"always", "initial_only", "initial", "schema_only", "schema_only_recovery"}
    assert set(SNAPSHOT_MODES_BY_CLASS[DBZ_MONGO]) == {"never", "initial"}


def test_connects_validator_wording_names_the_rejected_setting_but_never_its_value() -> None:
    exc = KafkaConnectConfigError(
        "Connector configuration is invalid and contains the following 1 error(s):\n"
        "The 'snapshot.mode' value is invalid: Value must be one of always, never, initial_only, initial, custom\n"
        "You can also find the above list of errors at the endpoint `/connector-plugins/{connectorType}/config/validate`"
    )
    safe = to_safe_error(exc, event="test.failed")
    assert safe.code == "KAFKA_CONNECT_CONFIG_INVALID"
    assert safe.message == "Kafka Connect rejected the connector configuration. Settings it objected to: snapshot.mode."
