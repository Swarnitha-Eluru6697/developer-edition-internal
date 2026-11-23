"""
Connection Profile Service — Postgres-authoritative.

CRUD is backed by Postgres (`dc_connection_profiles` via
ConnectionProfileRepository) — previously this went through the in-memory
store and never survived a restart. Every write opens its own session and
commits before returning, following the same pattern used throughout this
codebase (see app/services/schema_service.py's drift-approval methods and
app/services/catalog_service.py's template methods).

create_connection/update_connection/delete_connection also keep
app.services.secret_store in sync (GAP-01): a submitted connection_config's
credential fields are split out (app.services.secrets.split_secret_fields)
before anything is persisted, so ConnectionProfile.connection_config never
holds a raw secret — only secret_store does, in its own table, never
returned by any read here. _connection_to_dict re-synthesizes a MASK
placeholder for those fields in what it returns, purely so the read side
(mask_connection_profile, and the edit form) still looks the way it did
before this fix, without the real value ever having been in connection_config
to redact in the first place.

live_test_connection hydrates the real secret bundle back in (transiently,
never persisted) before probing — that's a genuine live connectivity check,
unlike test_connection below, which stays unchanged because it never reads
a credential field at all (see connection_adapters.py). Connection
discovery (GAP-10) moved off this service entirely — see
app/services/discovery_service.py.

probe_and_record (BUG-08) is the durable counterpart to live_test_connection:
a live probe used to be wrapped in a bare `except Exception: pass` at
creation time, so a probe that raised (a timeout, a DNS failure, ...) left
the connection saved with no diagnostic at all — not even a failed status.
This persists verification_status/last_tested_at/last_test_message
(and the coarser `status`) whether the probe returns cleanly or raises, and
is shared by both creation's own auto-probe and the explicit "Test
connection" action on an already-saved connection, so neither path can
silently lose the diagnostic again.
"""

import socket
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException, status

from app.core.errors import ConnectorConfigError
from app.db.session import AsyncSessionLocal
from app.models.connection import ConnectionProfile, ConnectionVerification
from app.repositories.dataconnect import (
    ConnectionProfileRepository,
    ConnectionVerificationRepository,
)
from app.schemas.errors import FieldError
from app.services.connection_adapters import adapter_for_source_type, live_probe
from app.services.connection_config_schema import validate_connection_config_fields
from app.services.secret_store import hydrate_connection_secrets, secret_store
from app.services.secrets import MASK, sanitize_probe_message, sensitive_keys, split_secret_fields

logger = structlog.get_logger(__name__)


STALE_MESSAGE = (
    "This connection's settings changed after it was last tested, so that result no longer applies. "
    "Test the connection again."
)


def _verification_view(row: ConnectionProfile, verification: ConnectionVerification | None) -> dict[str, Any]:
    """GAP-02: what the API reports about a connection's verification, DERIVED from its (separate)
    ConnectionVerification and the profile's current config_revision — never stored on the profile. A result
    recorded against an older revision is stale: the connection reads as unverified until re-tested."""
    if verification is None:
        return {
            "status": "draft", "verification_status": "unverified", "last_tested_at": None, "last_test_message": None,
            "latency_ms": None, "error_code": None, "verification_stale": False,
        }
    if verification.verified_revision != (row.config_revision or 1):
        return {
            "status": "draft", "verification_status": "unverified", "last_tested_at": verification.tested_at,
            "last_test_message": STALE_MESSAGE, "latency_ms": None, "error_code": None, "verification_stale": True,
        }
    ok = verification.status == "ok"
    return {
        "status": "active" if ok else "error", "verification_status": "ok" if ok else "error",
        "last_tested_at": verification.tested_at, "last_test_message": verification.message,
        "latency_ms": verification.latency_ms, "error_code": verification.error_code, "verification_stale": False,
    }


def _connection_to_dict(row: ConnectionProfile, verification: ConnectionVerification | None = None) -> dict[str, Any]:
    connection_config = dict(row.connection_config or {})
    if row.secret_ref:
        # The real values live in secret_store, never here (see this
        # module's docstring) — synthesize a masked placeholder for each
        # secret field this source_type's schema declares, so a caller
        # (mask_connection_profile, the edit form) can still tell "a value
        # is set here" without there ever having been a real one to redact.
        for key in sensitive_keys(row.source_type):
            connection_config.setdefault(key, MASK)
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "source_type": row.source_type,
        "connection_config": connection_config,
        "secret_ref": row.secret_ref,
        "network_profile": row.network_profile,
        "config_revision": row.config_revision or 1,
        "created_at": row.created_at,
        **_verification_view(row, verification),
    }


_CONNECTIVITY_FIELDS = ("source_type", "connection_config", "network_profile")


def _connectivity_changed(row: ConnectionProfile, payload: dict[str, Any], secret_changed: bool) -> bool:
    """True if applying `payload` would change how `row` connects: a new secret value, or a different
    source_type / non-secret connection_config / network_profile. `name` and everything else don't count."""
    if secret_changed:
        return True
    for field in _CONNECTIVITY_FIELDS:
        if field in payload and payload[field] != getattr(row, field):
            return True
    return False


class ConnectionProfileService:
    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def list_connections(self, org_id: str) -> list[dict]:
        async with AsyncSessionLocal() as session:
            rows = await ConnectionProfileRepository(session).list_for_org(org_id)
            verifications = await ConnectionVerificationRepository(session).get_many([row.id for row in rows])
            return [_connection_to_dict(row, verifications.get(row.id)) for row in rows]

    async def get_connection(self, org_id: str, connection_id: str) -> dict | None:
        async with AsyncSessionLocal() as session:
            row = await ConnectionProfileRepository(session).get_for_org(org_id, connection_id)
            if row is None:
                return None
            verifications = await ConnectionVerificationRepository(session).get_many([row.id])
            return _connection_to_dict(row, verifications.get(row.id))

    async def get_or_404(self, org_id: str, connection_id: str) -> dict:
        item = await self.get_connection(org_id, connection_id)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Connection '{connection_id}' not found.",
            )
        return item

    def _validate_config(self, payload: dict) -> None:
        errors = validate_connection_config_fields(payload.get("source_type"), payload.get("connection_config"))
        if errors:
            raise ConnectorConfigError(
                "; ".join(message for _key, message in errors),
                field_errors=[FieldError(field=f"connection_config.{key}", message=message) for key, message in errors],
                details={"source_type": payload.get("source_type")},
            )

    # ── Atomic creation and update (BUG-03) ──────────────────────────────────────
    #
    # A connection is a profile row plus its encrypted credentials plus the reference that ties them together. They are ONE
    # SQLAlchemy transaction: the profile insert, the secret upsert (on the same session — secret_store.put_in_session) and the
    # secret_ref update all commit together, once, or none of them does. A profile can never survive without its secret.
    # (A Vault-backed store would need a PROVISIONING -> ACTIVE flow with compensation and retry, since Vault cannot join a database
    # transaction; there is no such store — see secret_store.py — so that flow does not exist yet.)

    async def create_connection(self, org_id: str, payload: dict) -> dict:
        self._validate_config(payload)
        payload = dict(payload)
        payload["org_id"] = org_id
        non_secret, secret_fields = split_secret_fields(payload.get("connection_config"), payload.get("source_type"))
        payload["connection_config"] = non_secret
        if secret_fields:
            # Our own secret_store ref supersedes anything the client sent —
            # never trust a client-supplied secret_ref when real secret
            # material came with it. (No secret fields at all: leave
            # whatever secret_ref the client sent as-is — the manually
            # provisioned vault://... path case resolve_secret still
            # supports.)
            payload.pop("secret_ref", None)

        async with AsyncSessionLocal() as session:
            repo = ConnectionProfileRepository(session)
            row = await repo.create(payload)
            if secret_fields:
                secret_ref = await secret_store.put_in_session(
                    session, org_id=org_id, connection_id=row.id, fields=secret_fields
                )
                row = await repo.update(row, {"secret_ref": secret_ref})
            await session.commit()  # the one and only commit: profile + secret + reference, or nothing
            created = _connection_to_dict(row)

        logger.info("connection.created", org_id=org_id, connection_id=created["id"])
        return created

    async def update_connection(self, org_id: str, connection_id: str, payload: dict) -> dict:
        self._validate_config(payload)
        payload = dict(payload)
        payload["org_id"] = org_id
        non_secret, new_secret_fields = split_secret_fields(
            payload.get("connection_config"), payload.get("source_type")
        )
        payload["connection_config"] = non_secret
        if new_secret_fields:
            payload.pop("secret_ref", None)

        async with AsyncSessionLocal() as session:
            repo = ConnectionProfileRepository(session)
            row = await repo.get_for_org(org_id, connection_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Connection '{connection_id}' not found.",
                )
            existing_secret_ref = row.secret_ref
            # GAP-02: anything that changes HOW this connection connects invalidates its last test result
            # (config_revision moves on; ConnectionVerification keeps the revision it tested). A rename
            # or any other non-connectivity edit leaves the result valid.
            if _connectivity_changed(row, payload, bool(new_secret_fields)):
                payload["config_revision"] = (row.config_revision or 1) + 1
            updated_row = await repo.update(row, payload)
            if new_secret_fields:
                # Merge onto whatever's already stored — a field the client didn't touch (echoed MASK, or omitted the key) must
                # survive an update that only changes, say, the hostname. The secret is written on THIS session, so the profile
                # change, the new secret and the reference commit together or not at all.
                existing_secrets = await secret_store.get(existing_secret_ref) or {}
                secret_ref = await secret_store.put_in_session(
                    session, org_id=org_id, connection_id=connection_id, fields={**existing_secrets, **new_secret_fields}
                )
                if secret_ref != existing_secret_ref:
                    updated_row = await repo.update(updated_row, {"secret_ref": secret_ref})
            await session.commit()
            verifications = await ConnectionVerificationRepository(session).get_many([updated_row.id])
            updated = _connection_to_dict(updated_row, verifications.get(updated_row.id))

        logger.info("connection.updated", org_id=org_id, connection_id=connection_id)
        return updated

    async def delete_connection(self, org_id: str, connection_id: str) -> None:
        async with AsyncSessionLocal() as session:
            repo = ConnectionProfileRepository(session)
            row = await repo.get_for_org(org_id, connection_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Connection '{connection_id}' not found.",
                )
            # The secret goes in the SAME transaction as the profile (GAP-02): both are deleted or neither is, so a failure can
            # never leave a deleted connection's credentials behind, nor report an error for a delete that actually happened.
            await secret_store.delete_in_session(session, row.secret_ref)
            await repo.delete(row)
            await session.commit()
        logger.info("connection.deleted", org_id=org_id, connection_id=connection_id)

    # ── Async adapter operations ───────────────────────────────────────────────

    async def test_connection(self, connection: dict) -> dict[str, Any]:
        adapter = adapter_for_source_type(connection["source_type"])
        result = await adapter.test(connection)
        logger.info(
            "connection.tested",
            connection_id=connection.get("id"),
            status=result.status,
        )
        return {"status": result.status, "message": result.message, "details": result.details}

    async def live_test_connection(self, connection: dict, org_id: str | None = None) -> dict[str, Any]:
        """Actually reach the source (TCP/HTTP/endpoint/path), not just structural checks.
        `org_id` is passed explicitly (not just read off `connection`) because
        an unsaved payload probed via POST /connections/test-connection has
        no org_id of its own yet — it's needed for audit logging and the
        per-org rate limit in app.core.network_policy."""
        if org_id and not connection.get("org_id"):
            connection = {**connection, "org_id": org_id}
        # A saved connection's connection_config only ever has a MASK
        # placeholder for its secret fields (see _connection_to_dict) — the
        # real value is needed here for a genuine auth attempt against the
        # source. A no-op for an unsaved payload (POST .../test-connection),
        # which has no secret_ref and already carries its real values.
        connection = await hydrate_connection_secrets(connection)
        started = time.perf_counter()
        result = await live_probe(connection)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        # Not every adapter reports its own latency; record the wall-clock time of the probe when one doesn't.
        details = {**(result.details or {}), "latency_ms": (result.details or {}).get("latency_ms", elapsed_ms)}
        logger.info(
            "connection.live_tested",
            connection_id=connection.get("id"),
            source_type=connection.get("source_type"),
            status=result.status,
        )
        return {"status": result.status, "message": result.message, "details": details}

    async def probe_and_record(self, org_id: str, connection_id: str) -> dict[str, Any]:
        """Live-probes a saved connection and durably records the outcome as its ConnectionVerification —
        the ONLY place a verification is written (GAP-02), and it never touches the profile's own
        configuration. Never raises: a probe exception is caught here and turned into the same "error"
        outcome a clean-but-failed ConnectionCheck would produce, with a sanitized message and a stable
        error code — either way, something is always persisted (BUG-08).

        The result is recorded against the config_revision the connection had WHEN THE PROBE STARTED, so an
        edit that lands mid-probe correctly leaves this result stale rather than blessing the new settings."""
        connection = await self.get_or_404(org_id, connection_id)
        revision = connection["config_revision"]
        now = datetime.now(UTC)
        error_code: str | None = None
        try:
            result = await self.live_test_connection(connection, org_id)
        except Exception as exc:  # noqa: BLE001 - must persist a diagnostic no matter what the probe raises, not just what it deliberately returns
            # Same scrubbing as the persisted message: an exception's own text can embed a connection
            # URI with credentials, and a log line is no safer a place for those than the database.
            logger.warning(
                "connection.probe_failed", connection_id=connection_id, org_id=org_id,
                error=sanitize_probe_message(str(exc)), error_type=type(exc).__name__,
            )
            error_code = _error_code_for(exc)
            result = {"status": "error", "message": f"Connection check failed: {exc}", "details": {}}

        ok = result.get("status") == "ok"
        message = sanitize_probe_message(result.get("message"))
        details = result.get("details") or {}
        latency = details.get("latency_ms")

        async with AsyncSessionLocal() as session:
            await ConnectionVerificationRepository(session).record(
                connection_id=connection_id, org_id=org_id, status="ok" if ok else "error", tested_at=now,
                latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
                error_code=None if ok else (error_code or "PROBE_FAILED"),
                message=message, verified_revision=revision,
            )
            await session.commit()

        return {"status": result.get("status"), "message": message, "details": details}


def _error_code_for(exc: Exception) -> str:
    """A stable, machine-readable reason for a probe that RAISED (the message is for humans)."""
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, ConnectionRefusedError):
        return "CONNECTION_REFUSED"
    if isinstance(exc, socket.gaierror):
        return "DNS_FAILURE"
    if isinstance(exc, PermissionError):
        return "PERMISSION_DENIED"
    return "PROBE_ERROR"


connection_service = ConnectionProfileService()
