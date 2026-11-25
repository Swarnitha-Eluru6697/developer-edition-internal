"""
Secret storage for connection profile credentials — GAP-01's fix.

A connection's credential fields (password, api_key, ...) are extracted out
of ConnectionProfile.connection_config at write time (see
app/services/secrets.py's split_secret_fields) and stored here instead, in
their own table, keyed by the connection's own `secret_ref`. No route ever
reads this table directly, and it's never included in an audit before/after
snapshot — both of those only ever see connection_config, which no longer
contains these values. The real value is read back only transiently, via
`hydrate_connection_secrets`, at the few points that genuinely need it for a
live operation: a Kafka Connect deploy (app/services/connect_config.py), a
live connectivity probe, or source introspection
(app/services/pipeline_service.py, app/services/connection_service.py).

The values are encrypted at rest (AES-256-GCM, key from the environment — see
app/services/secret_crypto.py), not stored as readable JSON.

PostgresSecretStore is the only implementation. `DATACONNECT_SECRET_BACKEND=vault` is rejected at startup (app/core/config.py)
rather than accepted and ignored: there is no Vault-backed store. It sits behind the SecretStore protocol so one can be added later
without any caller changing — connection_service.py and connect_config.py only ever talk to this protocol, never to Postgres
directly for secret material. (A hand-provisioned `vault://...` secret_ref is a different thing: Kafka Connect's own ConfigProvider
resolves it, see app/services/secrets.py; this store does not read it.)

Deleting a connection (GAP-02) removes the profile and its secret in ONE transaction, so there is no second, external step that
could fail and leave a secret behind. A store outside the database (Vault) cannot join that transaction; it would need
ACTIVE -> DELETING -> DELETED plus a durable cleanup record retried until the external delete succeeds. That is not built because
no such store exists.
"""

import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.connection import ConnectionSecret
from app.services.secret_crypto import decrypt_fields, encrypt_fields, is_encrypted

_REF_PREFIX = "secret://dataconnect/connections/"
_PROBE_ID = "00000000-0000-0000-0000-000000000000"   # the nil UUID: a valid value for the UUID key column, never a real connection id


def secret_ref_for(connection_id: str) -> str:
    return f"{_REF_PREFIX}{connection_id}"


def _connection_id_from_ref(secret_ref: str | None) -> str | None:
    """None for anything this store doesn't own — notably a manually
    provisioned `vault://...` ref (still supported — see
    app/services/secrets.py's resolve_secret), which this store was never
    asked to write and has nothing to fetch for."""
    if not secret_ref or not secret_ref.startswith(_REF_PREFIX):
        return None
    return secret_ref[len(_REF_PREFIX):]


class SecretStore(Protocol):
    async def put(self, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        """Persists `fields` (already just the secret ones — see
        split_secret_fields) for this connection and returns its secret_ref.
        Overwrites whatever was stored before for this connection."""
        ...

    async def get(self, secret_ref: str | None) -> dict[str, Any] | None:
        """None if `secret_ref` is absent, or isn't one this store owns."""
        ...

    async def put_in_session(self, session: AsyncSession, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        """Like `put`, but written on the caller's session and NOT committed (BUG-03: one transaction for the whole connection)."""
        ...

    async def delete(self, secret_ref: str | None) -> None:
        ...

    async def delete_in_session(self, session: AsyncSession, secret_ref: str | None) -> None:
        """Like `delete`, but on the caller's session and NOT committed (GAP-02: the profile and its secret go in one transaction)."""
        ...

    async def check_ready(self) -> dict[str, Any]:
        """GAP-05: can this store actually store and read secrets right now? {"ok": bool, "backend": str} and, on failure,
        {"error": <exception class name>} — never a message, a path or a credential, since readiness is unauthenticated and its
        detail is logged. A store outside the database (Vault) implements it as: authenticate, then prove access to the exact
        paths it uses — a failed login or a denied path is not ready."""
        ...


class _ConnectionSecretRepo:
    """connection_id IS this table's primary key (a plain one-to-one, not
    the generic id-keyed shape every other repository in
    app/repositories/dataconnect.py uses), so this stays local to its one
    caller (PostgresSecretStore) rather than joining that file's otherwise-
    uniform convention for a shape it doesn't fit."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, connection_id: str) -> ConnectionSecret | None:
        return await self.session.get(ConnectionSecret, connection_id)

    async def upsert(self, *, org_id: str, connection_id: str, values: dict[str, Any]) -> ConnectionSecret:
        row = await self.get(connection_id)
        if row is None:
            row = ConnectionSecret(connection_id=connection_id, org_id=org_id, values=values)
            self.session.add(row)
        else:
            row.values = values
        await self.session.flush()
        return row

    async def delete(self, connection_id: str) -> None:
        row = await self.get(connection_id)
        if row is not None:
            await self.session.delete(row)
            await self.session.flush()


class PostgresSecretStore:
    """Stores each connection's credential bundle AES-256-GCM encrypted (app/services/secret_crypto.py) in
    dc_connection_secrets. Writes are always encrypted; a legacy plaintext row is still readable until the
    0035 migration re-encrypts it."""

    async def put(self, *, org_id: str, connection_id: str, fields: dict[str, Any]) -> str:
        async with AsyncSessionLocal() as session:
            ref = await self.put_in_session(session, org_id=org_id, connection_id=connection_id, fields=fields)
            await session.commit()
        return ref

    async def put_in_session(
        self, session: AsyncSession, *, org_id: str, connection_id: str, fields: dict[str, Any]
    ) -> str:
        """Writes the encrypted bundle on the CALLER's session and does not commit — so a connection profile, its secret and
        its secret_ref can all be one transaction (BUG-03): they commit together or none of them does."""
        envelope = encrypt_fields(fields, org_id=org_id, connection_id=connection_id)
        await _ConnectionSecretRepo(session).upsert(org_id=org_id, connection_id=connection_id, values=envelope)
        return secret_ref_for(connection_id)

    async def get(self, secret_ref: str | None) -> dict[str, Any] | None:
        connection_id = _connection_id_from_ref(secret_ref)
        if connection_id is None:
            return None
        async with AsyncSessionLocal() as session:
            row = await _ConnectionSecretRepo(session).get(connection_id)
            if row is None:
                return None
            stored = dict(row.values)
            if is_encrypted(stored):
                return decrypt_fields(stored, org_id=row.org_id, connection_id=connection_id)
            return stored  # legacy plaintext bundle, until the 0035 migration re-encrypts it

    async def check_ready(self) -> dict[str, Any]:
        """Ready means: the encryption key works (a throwaway bundle encrypts and decrypts back) and the secret table is readable.
        Writes nothing, and reports the failure's class name only."""
        try:
            probe = {"probe": uuid.uuid4().hex}
            envelope = encrypt_fields(probe, org_id=_PROBE_ID, connection_id=_PROBE_ID)
            if decrypt_fields(envelope, org_id=_PROBE_ID, connection_id=_PROBE_ID) != probe:
                return {"ok": False, "backend": "postgres", "error": "RoundTripMismatch"}
            async with AsyncSessionLocal() as session:
                await _ConnectionSecretRepo(session).get(_PROBE_ID)   # a read of a row that does not exist: proves table access
            return {"ok": True, "backend": "postgres"}
        except Exception as exc:  # noqa: BLE001 - any failure means not ready; the class name is all that leaves this method
            return {"ok": False, "backend": "postgres", "error": type(exc).__name__}

    async def delete(self, secret_ref: str | None) -> None:
        async with AsyncSessionLocal() as session:
            await self.delete_in_session(session, secret_ref)
            await session.commit()

    async def delete_in_session(self, session: AsyncSession, secret_ref: str | None) -> None:
        connection_id = _connection_id_from_ref(secret_ref)
        if connection_id is None:
            return
        await _ConnectionSecretRepo(session).delete(connection_id)


secret_store: SecretStore = PostgresSecretStore()


async def hydrate_connection_secrets(connection: dict[str, Any]) -> dict[str, Any]:
    """Merges a connection's real secret values back into a TRANSIENT copy
    of its connection_config, for one-off live use — a deploy-time config
    build, a live connectivity probe, source introspection. Never persisted
    or returned; call this right before that one use, nowhere else.

    A no-op if the connection has no secret_ref: a brand-new, not-yet-saved
    payload (e.g. POST /connections/test-connection) already carries its
    real values directly in connection_config, since it was never written
    through split_secret_fields in the first place — nothing to hydrate."""
    secret_ref = connection.get("secret_ref")
    if not secret_ref:
        return connection
    secret_fields = await secret_store.get(secret_ref)
    if not secret_fields:
        return connection
    return {**connection, "connection_config": {**(connection.get("connection_config") or {}), **secret_fields}}
