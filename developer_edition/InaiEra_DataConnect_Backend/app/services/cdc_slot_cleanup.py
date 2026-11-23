"""
Releases a deleted PostgreSQL CDC pipeline's replication slot and publication on the SOURCE database.

Debezium creates both when its connector first starts (slot.name / publication.name, see debezium_identifiers.py) and never removes them:
deleting the connector leaves them behind. A slot nobody reads makes PostgreSQL keep every WAL segment from its position onwards, so the
source's disk fills up for as long as it exists, and each one also uses one of max_replication_slots. Found in the 2026-10-28 large-data
test: three deleted pipelines left three inactive slots (31 MB of WAL each and growing), next to one left by an earlier test (108 MB).

Called once the reconciler has confirmed the connector is gone from Kafka Connect, just before the pipeline row is removed. Best effort by
design: the source may be unreachable or the user may lack the rights, and none of that may block deleting the pipeline — a failure is
logged (with what to drop by hand) instead.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.core.redaction import redact_secrets
from app.services.debezium_identifiers import debezium_publication_name, debezium_slot_name

logger = structlog.get_logger(__name__)

DBZ_PG = "io.debezium.connector.postgresql.PostgresConnector"
# After Kafka Connect drops the connector its task stops and closes the replication connection; until then the slot is "active" and
# PostgreSQL refuses to drop it. A few seconds is normal.
_WAIT_FOR_INACTIVE_SECONDS = 15.0
_POLL_SECONDS = 1.0
_CONNECT_TIMEOUT = 8.0

_pending: set[asyncio.Task] = set()   # keeps the background tasks referenced until they finish


def is_postgres_cdc(pipeline: dict[str, Any]) -> bool:
    return ((pipeline.get("generated_config") or {}).get("connector.class") == DBZ_PG
            or (pipeline.get("template_snapshot") or {}).get("connector_class") == DBZ_PG)


def schedule_release(pipeline: dict[str, Any]) -> None:
    """Fire-and-forget: the reconciler tick must not wait on a customer's database."""
    if not is_postgres_cdc(pipeline) or not pipeline.get("connector_name"):
        return
    task = asyncio.create_task(release_slot_and_publication(dict(pipeline)))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def release_slot_and_publication(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Drops the pipeline's slot (once inactive) and publication. Returns what happened, for logs and tests."""
    from app.core.network_policy import validate_destination
    from app.services.connection_service import connection_service
    from app.services.secret_store import hydrate_connection_secrets

    connector = pipeline["connector_name"]
    slot, publication = debezium_slot_name(connector), debezium_publication_name(connector)
    outcome: dict[str, Any] = {"slot": slot, "publication": publication, "slot_dropped": False, "publication_dropped": False}
    try:
        connection = await connection_service.get_connection(pipeline["org_id"], pipeline["connection_profile_id"])
        if connection is None:
            outcome["skipped"] = "connection deleted"
            logger.warning("cdc_cleanup.skipped", reason="connection no longer exists", slot=slot, publication=publication)
            return outcome
        cfg = (await hydrate_connection_secrets(connection)).get("connection_config") or {}
        host = cfg.get("hostname") or cfg.get("host")
        await validate_destination(host, org_id=pipeline["org_id"], purpose="cdc_cleanup")

        import asyncpg

        conn = await asyncio.wait_for(
            asyncpg.connect(host=host, port=int(cfg.get("port") or 5432), user=cfg.get("username"), password=cfg.get("password"),
                            database=cfg.get("database"), timeout=_CONNECT_TIMEOUT),
            timeout=_CONNECT_TIMEOUT + 1,
        )
        try:
            waited = 0.0
            while True:
                active = await conn.fetchval("SELECT active FROM pg_replication_slots WHERE slot_name = $1", slot)
                if active is None:
                    break                                   # no slot (never started, or already dropped)
                if not active:
                    await conn.execute("SELECT pg_drop_replication_slot($1)", slot)
                    outcome["slot_dropped"] = True
                    break
                if waited >= _WAIT_FOR_INACTIVE_SECONDS:
                    outcome["slot_still_active"] = True     # something else is still reading it: leave it alone
                    break
                await asyncio.sleep(_POLL_SECONDS)
                waited += _POLL_SECONDS
            exists = await conn.fetchval("SELECT 1 FROM pg_publication WHERE pubname = $1", publication)
            if exists:
                await conn.execute(f'DROP PUBLICATION "{publication}"')   # the name is our own identifier (letters, digits, _)
                outcome["publication_dropped"] = True
        finally:
            await conn.close()
        logger.info("cdc_cleanup.done", **outcome)
    except Exception as exc:   # noqa: BLE001 — best effort, see the module docstring
        outcome["error"] = type(exc).__name__
        logger.warning("cdc_cleanup.failed", slot=slot, publication=publication, error=redact_secrets(str(exc))[:300],
                       hint="drop them on the source database by hand: SELECT pg_drop_replication_slot('<slot>'); DROP PUBLICATION <publication>;")
    return outcome
