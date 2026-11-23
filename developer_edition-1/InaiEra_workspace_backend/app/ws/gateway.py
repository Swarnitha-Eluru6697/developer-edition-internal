"""WebSocket gateway for live execution updates.

Each connected client subscribes to a Redis channel for their session.
Worker processes (or even the same process) publish execution events
to that channel; the gateway forwards them as JSON envelopes.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import datetime, timezone
from uuid import UUID

import redis.asyncio as redis
import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.core.authz import get_owned_execution
from app.core.config import get_settings
from app.core.errors import WorkspaceError
from app.db.session import AsyncSessionLocal
from app.models import Execution
from app.models import Session as SessionModel
from app.services.execution_router import (
    get_execution_router, get_redis, session_channel_for,
)

log = structlog.get_logger()
router = APIRouter(tags=["websocket"])

# Application close code (4000-4999 range) for "your session was ended".
SESSION_TERMINATED_CLOSE_CODE = 4001

# How stale a connection's last-seen entry must be before it's dropped from
# the per-user live-connection count (websocket_max_per_user, WS-LIFE-013) —
# a multiple of the 30s keepalive below so one missed tick never falsely
# frees up a slot a connection is still actually using.
_WS_CONN_STALE_SECONDS = 90
# Only write Session.last_activity_at this often per connection — every
# session.activity message would otherwise be a DB write on a 30s-or-tighter
# client heartbeat cadence, for a column whose only consumer (the idle-
# timeout reconciler) checks on the order of minutes.
_ACTIVITY_WRITE_DEBOUNCE_SECONDS = 60


async def _consume_ws_ticket(ticket: str) -> dict | None:
    """Look up and immediately delete a WS connect ticket (single-use).

    Returns the ticket's bound {org_id, user_id, external_user_id,
    session_id} dict, or None if it never existed, was already used, or
    expired (WS-AUTH-010).
    """
    r = get_redis()
    # GETDEL, not GET then DELETE: atomic, so two concurrent connects can't
    # both read the ticket before either deletes it (Redis >= 6.2).
    raw = await r.getdel(f"ws-ticket:{ticket}")
    if raw is None:
        return None
    return json.loads(raw)


@router.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str, ticket: str = Query(...)):
    """Single WebSocket per session.

    Auth: a single-use, short-lived ticket minted by POST /api/v1/auth/ws-ticket
    (never the raw session JWT — see WS-AUTH-010). Browsers can't set custom
    headers on a WS upgrade, so *something* still has to ride the query
    string, but a ticket is worthless after this one connection attempt or
    ~30s, unlike a long-lived bearer credential.
    Subprotocol: workspace.v1
    """
    settings = get_settings()

    try:
        session_uuid = UUID(session_id)
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    ticket_data = await _consume_ws_ticket(ticket)
    if ticket_data is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # The ticket's session_id/org_id binding IS the authorization decision —
    # it was only ever minted after get_owned_session confirmed ownership at
    # mint time (see mint_ws_ticket). A ticket presented for a different
    # session_id than the one it was bound to, or from a different org,
    # is rejected outright rather than re-trusted.
    if (
        ticket_data.get("session_id") != str(session_uuid)
        or ticket_data.get("org_id") != settings.org_id
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    owner_user_id = UUID(ticket_data["user_id"])
    external_user_id = ticket_data.get("external_user_id", ticket_data["user_id"])

    # The ticket was valid up to 30s ago; the session may have ended since.
    async with AsyncSessionLocal() as db:
        session_row = await db.get(SessionModel, session_uuid)
        if session_row is None or session_row.user_id != owner_user_id or session_row.status == "terminated":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    r = get_redis()
    # websocket_max_per_user (WS-LIFE-013): a Redis sorted set per user,
    # member=connection id, score=last-seen unix time. Prune anything past
    # the stale window before counting — a connection whose process died
    # without reaching the `finally` cleanup below (a hard crash, not a
    # clean disconnect) must not permanently occupy a slot.
    conns_key = f"ws-conns:{owner_user_id}"
    connection_id = uuid.uuid4().hex
    now_ts = time.time()
    await r.zremrangebyscore(conns_key, "-inf", now_ts - _WS_CONN_STALE_SECONDS)
    live_count = await r.zcard(conns_key)
    if live_count >= get_settings().websocket_max_per_user:
        log.info("ws_rejected_max_connections", user_id=external_user_id, live_count=live_count)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await r.zadd(conns_key, {connection_id: now_ts})

    await websocket.accept(subprotocol="workspace.v1")
    log.info(
        "ws_connected", session_id=session_id, user_id=external_user_id,
    )

    # Send session.ready
    await websocket.send_text(json.dumps({
        "v": 1,
        "type": "session.ready",
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "user_id": external_user_id,
    }))

    # A fresh connection is itself activity — reset the idle clock immediately
    # rather than waiting for the first debounced session.activity message
    # (WS-LIFE-013).
    last_activity_write = time.monotonic()
    await _touch_session_activity(session_uuid, owner_user_id)

    # Reconnect catch-up (WS-LIFE-013): Redis pub/sub has no history, so a
    # client that (re)connects mid-execution would otherwise see nothing
    # until the next chunk happens to be published. Replay what's still
    # in-flight for this session from the durable Execution rows so the
    # client can reattach instead of guessing.
    async with AsyncSessionLocal() as db:
        in_flight = (await db.execute(
            select(Execution).where(
                Execution.session_id == session_uuid,
                Execution.status.in_(("pending", "running")),
            )
        )).scalars().all()
        for execution in in_flight:
            await websocket.send_text(json.dumps({
                "v": 1, "type": "execution.resumed",
                "ts": datetime.now(timezone.utc).isoformat(),
                "execution_id": str(execution.id),
                "status": execution.status,
                "language": execution.language,
                "file_id": str(execution.file_id) if execution.file_id else None,
                "cell_id": execution.cell_id,
                "started_at": execution.started_at.isoformat() if execution.started_at else None,
            }))

    pubsub = r.pubsub()
    await pubsub.subscribe(session_channel_for(session_id))

    # Start two tasks: one forwarding pub/sub → ws, one reading ws → server
    async def pubsub_to_ws():
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    await websocket.send_text(message["data"])
                except Exception:
                    break
                # Ending the session revokes its open sockets too, not just
                # new tickets (DELETE /sessions/{id} publishes this).
                if '"session.terminated"' in message["data"]:
                    with contextlib.suppress(Exception):
                        await websocket.close(code=SESSION_TERMINATED_CLOSE_CODE)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(session_channel_for(session_id))
            await pubsub.close()

    async def ws_to_server():
        nonlocal last_activity_write
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type")
                if msg_type == "ping":
                    await websocket.send_text(json.dumps({
                        "v": 1, "type": "pong",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }))
                elif msg_type == "session.activity":
                    # Debounced (WS-LIFE-013): last_activity_at only feeds the
                    # idle-timeout reconciler, which checks on the order of
                    # minutes — no need to write on every single heartbeat.
                    now_mono = time.monotonic()
                    if now_mono - last_activity_write >= _ACTIVITY_WRITE_DEBOUNCE_SECONDS:
                        last_activity_write = now_mono
                        await _touch_session_activity(session_uuid, owner_user_id)
                elif msg_type == "execution.cancel":
                    raw_execution_id = msg.get("payload", {}).get("execution_id")
                    if raw_execution_id:
                        try:
                            execution_uuid = UUID(raw_execution_id)
                        except ValueError:
                            continue
                        async with AsyncSessionLocal() as db:
                            try:
                                execution = await get_owned_execution(db, owner_user_id, execution_uuid)
                            except WorkspaceError:
                                continue
                        # Same durable path as POST /executions/{id}/cancel —
                        # the old "cancel:execution:*" channel had no listener.
                        await get_execution_router().cancel(execution_uuid, execution.language)
        except WebSocketDisconnect:
            pass

    forwarder = asyncio.create_task(pubsub_to_ws())
    receiver = asyncio.create_task(ws_to_server())

    # Keepalive: server-side ping every 30s
    async def keepalive():
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await websocket.send_text(json.dumps({
                        "v": 1, "type": "ping",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }))
                    # Refresh this connection's liveness score so the
                    # websocket_max_per_user prune above never frees its slot
                    # while it's still genuinely connected.
                    await r.zadd(conns_key, {connection_id: time.time()})
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    keepalive_task = asyncio.create_task(keepalive())

    try:
        done, pending = await asyncio.wait(
            [forwarder, receiver, keepalive_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        await r.zrem(conns_key, connection_id)
        log.info("ws_disconnected", session_id=session_id, user_id=external_user_id)


async def _touch_session_activity(session_uuid: UUID, owner_user_id: UUID) -> None:
    """Best-effort last_activity_at bump — a failure here must never break
    the connection (WS-LIFE-013's idle reconciler is a backstop, not a
    correctness dependency for anything else)."""
    try:
        async with AsyncSessionLocal() as db:
            session = await db.get(SessionModel, session_uuid)
            if session and session.user_id == owner_user_id and session.status == "active":
                session.last_activity_at = datetime.now(timezone.utc)
                await db.commit()
    except Exception:
        log.warning("session_activity_touch_failed", session_id=str(session_uuid))
