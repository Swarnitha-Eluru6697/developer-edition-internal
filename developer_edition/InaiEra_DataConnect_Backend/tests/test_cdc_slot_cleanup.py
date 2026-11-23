"""
Deleting a PostgreSQL CDC pipeline must release what Debezium left on the source: its replication slot (which otherwise pins WAL forever)
and its publication. Found in the 2026-10-28 large-data test: three deleted pipelines left three inactive slots behind.
"""

import asyncio

import pytest

from app.services import cdc_slot_cleanup
from app.services.cdc_slot_cleanup import DBZ_PG, release_slot_and_publication, schedule_release
from app.services.debezium_identifiers import debezium_publication_name, debezium_slot_name

CONNECTOR = "sales--pg--orders--cdc--v1"
SLOT, PUB = debezium_slot_name(CONNECTOR), debezium_publication_name(CONNECTOR)


class FakeConn:
    """Stands in for an asyncpg connection to the source: a slot that is active for the first `active_polls` checks, and a publication."""

    def __init__(self, slot_exists=True, active_polls=0, publication_exists=True):
        self.slot_exists, self.active_polls, self.publication_exists = slot_exists, active_polls, publication_exists
        self.executed: list[str] = []
        self.closed = False

    async def fetchval(self, sql, *args):
        if "pg_replication_slots" in sql:
            assert args == (SLOT,)
            if not self.slot_exists:
                return None
            if self.active_polls > 0:
                self.active_polls -= 1
                return True
            return False
        if "pg_publication" in sql:
            assert args == (PUB,)
            return 1 if self.publication_exists else None
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.executed.append(sql if not args else f"{sql} {args}")

    async def close(self):
        self.closed = True


@pytest.fixture
def source(monkeypatch):
    """Patches every outside call: the connection lookup, secret hydration, network policy and asyncpg.connect."""
    state = {"conn": FakeConn(), "connection": {"connection_config": {"hostname": "db.internal", "port": 5432, "database": "sales",
                                                                       "username": "svc"}}, "connect_error": None}

    async def get_connection(org_id, connection_id):
        return state["connection"]

    async def hydrate(connection):
        return {**connection, "connection_config": {**connection["connection_config"], "password": "pw"}}

    async def validate_destination(host, org_id=None, purpose=None):
        return None

    async def connect(**kwargs):
        if state["connect_error"]:
            raise state["connect_error"]
        state["connect_kwargs"] = kwargs
        return state["conn"]

    import asyncpg

    from app.core import network_policy
    from app.services import secret_store
    from app.services.connection_service import connection_service

    monkeypatch.setattr(connection_service, "get_connection", get_connection)
    monkeypatch.setattr(secret_store, "hydrate_connection_secrets", hydrate)
    monkeypatch.setattr(network_policy, "validate_destination", validate_destination)
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(cdc_slot_cleanup, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cdc_slot_cleanup, "_WAIT_FOR_INACTIVE_SECONDS", 0.05)
    return state


def _pipeline(klass=DBZ_PG):
    return {"id": "p1", "org_id": "org_1", "connection_profile_id": "c1", "connector_name": CONNECTOR,
            "generated_config": {"connector.class": klass}}


@pytest.mark.asyncio
async def test_an_inactive_slot_and_the_publication_are_dropped(source) -> None:
    outcome = await release_slot_and_publication(_pipeline())
    assert outcome["slot_dropped"]
    assert outcome["publication_dropped"]
    assert source["conn"].executed == [f"SELECT pg_drop_replication_slot($1) ('{SLOT}',)", f'DROP PUBLICATION "{PUB}"']
    assert source["conn"].closed
    assert source["connect_kwargs"]["password"] == "pw"          # signs in with the connection's real (hydrated) secret


@pytest.mark.asyncio
async def test_it_waits_for_the_stopping_task_to_let_go_of_the_slot(source) -> None:
    source["conn"] = FakeConn(active_polls=2)                     # still held for two checks, then released
    outcome = await release_slot_and_publication(_pipeline())
    assert outcome["slot_dropped"]


@pytest.mark.asyncio
async def test_a_slot_that_stays_in_use_is_left_alone(source) -> None:
    source["conn"] = FakeConn(active_polls=10_000)
    outcome = await release_slot_and_publication(_pipeline())
    assert not outcome["slot_dropped"]
    assert outcome["slot_still_active"]
    assert not any("pg_drop_replication_slot" in sql for sql in source["conn"].executed)


@pytest.mark.asyncio
async def test_nothing_to_drop_is_fine(source) -> None:
    source["conn"] = FakeConn(slot_exists=False, publication_exists=False)
    outcome = await release_slot_and_publication(_pipeline())
    assert outcome == {"slot": SLOT, "publication": PUB, "slot_dropped": False, "publication_dropped": False}


@pytest.mark.asyncio
async def test_an_unreachable_source_never_raises(source) -> None:
    source["connect_error"] = ConnectionRefusedError("connection refused")
    outcome = await release_slot_and_publication(_pipeline())   # must not raise: deleting the pipeline goes ahead regardless
    assert outcome["error"] == "ConnectionRefusedError"
    assert not outcome["slot_dropped"]


@pytest.mark.asyncio
async def test_a_deleted_connection_is_skipped(source) -> None:
    source["connection"] = None
    outcome = await release_slot_and_publication(_pipeline())
    assert outcome["skipped"] == "connection deleted"


@pytest.mark.asyncio
async def test_only_postgres_cdc_pipelines_are_cleaned_up(source) -> None:
    schedule_release(_pipeline(klass="io.confluent.connect.jdbc.JdbcSourceConnector"))
    schedule_release(_pipeline(klass="io.debezium.connector.mysql.MySqlConnector"))
    assert not cdc_slot_cleanup._pending
    schedule_release(_pipeline())
    assert len(cdc_slot_cleanup._pending) == 1
    await asyncio.gather(*cdc_slot_cleanup._pending)
    assert source["conn"].executed                                # the scheduled task really dropped them
