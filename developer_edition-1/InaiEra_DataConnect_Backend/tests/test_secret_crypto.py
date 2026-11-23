"""GAP-01: connection credentials are encrypted at rest. Drives the REAL PostgresSecretStore (not the in-memory
fake the rest of the suite uses) against an in-memory table, with the marker from test_secret_regression.py, and
inspects what would actually sit in the database column."""
import base64
import json

import pytest

import app.services.secret_store as secret_store_module
from app.core.config import settings
from app.services import secret_crypto
from app.services.secret_crypto import (
    SecretEncryptionError,
    decrypt_fields,
    encrypt_fields,
    generate_key,
    is_encrypted,
)
from app.services.secret_store import PostgresSecretStore, secret_ref_for

MARKER = "TEST_SECRET_9f32d1e7c8b4a5069d2f1e3c4b5a6978"
CONN = "11111111-1111-1111-1111-111111111111"
OTHER_CONN = "22222222-2222-2222-2222-222222222222"
FIELDS = {"password": MARKER, "api_key": "k-" + MARKER}


@pytest.fixture
def key(monkeypatch):
    k = generate_key()
    monkeypatch.setattr(settings, "secret_encryption_key", k)
    monkeypatch.setattr(settings, "secret_encryption_key_id", "k1")
    monkeypatch.setattr(settings, "secret_encryption_previous_keys", "")
    return k


# ── the envelope ─────────────────────────────────────────────────────────────

def test_the_envelope_is_ciphertext_with_no_trace_of_the_secret(key) -> None:
    envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)

    assert is_encrypted(envelope)
    assert MARKER not in json.dumps(envelope)
    assert "password" not in json.dumps(envelope)  # not even the field NAMES are stored in clear
    assert decrypt_fields(envelope, org_id="org", connection_id=CONN) == FIELDS


def test_encrypting_twice_gives_different_ciphertext(key) -> None:
    first = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    second = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)

    assert first["ct"] != second["ct"]
    assert first["nonce"] != second["nonce"]


def test_a_ciphertext_copied_onto_another_connection_will_not_decrypt(key) -> None:
    envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)

    with pytest.raises(SecretEncryptionError, match="could not be decrypted"):
        decrypt_fields(envelope, org_id="org", connection_id=OTHER_CONN)
    with pytest.raises(SecretEncryptionError, match="could not be decrypted"):
        decrypt_fields(envelope, org_id="another-org", connection_id=CONN)


def test_a_tampered_ciphertext_is_rejected(key) -> None:
    envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    raw = bytearray(base64.b64decode(envelope["ct"]))
    raw[0] ^= 0x01
    envelope["ct"] = base64.b64encode(bytes(raw)).decode()

    with pytest.raises(SecretEncryptionError):
        decrypt_fields(envelope, org_id="org", connection_id=CONN)


def test_the_wrong_key_cannot_decrypt(key, monkeypatch) -> None:
    envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    monkeypatch.setattr(settings, "secret_encryption_key", generate_key())

    with pytest.raises(SecretEncryptionError):
        decrypt_fields(envelope, org_id="org", connection_id=CONN)


def test_errors_never_contain_the_secret_or_the_key(key, monkeypatch) -> None:
    envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    monkeypatch.setattr(settings, "secret_encryption_key", generate_key())

    with pytest.raises(SecretEncryptionError) as exc_info:
        decrypt_fields(envelope, org_id="org", connection_id=CONN)

    assert MARKER not in str(exc_info.value)
    assert key not in str(exc_info.value)


# ── configuration & rotation ─────────────────────────────────────────────────

def test_with_no_key_configured_nothing_is_encrypted_and_the_error_says_how_to_fix_it(monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_encryption_key", "")

    with pytest.raises(SecretEncryptionError, match="DATACONNECT_SECRET_ENCRYPTION_KEY"):
        encrypt_fields(FIELDS, org_id="org", connection_id=CONN)


@pytest.mark.parametrize("bad", ["not base64 at all!!", base64.urlsafe_b64encode(b"too-short").decode()])
def test_a_malformed_key_is_rejected_at_startup_validation(monkeypatch, bad) -> None:
    monkeypatch.setattr(settings, "secret_encryption_key", bad)

    with pytest.raises(SecretEncryptionError):
        secret_crypto.validate_configuration()


def test_rotation_old_rows_still_decrypt_and_new_writes_use_the_new_key(key, monkeypatch) -> None:
    old_envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    new_key = generate_key()
    monkeypatch.setattr(settings, "secret_encryption_key", new_key)
    monkeypatch.setattr(settings, "secret_encryption_key_id", "k2")
    monkeypatch.setattr(settings, "secret_encryption_previous_keys", f"k1:{key}")

    assert decrypt_fields(old_envelope, org_id="org", connection_id=CONN) == FIELDS  # old row, old key
    fresh = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    assert fresh["kid"] == "k2"
    assert decrypt_fields(fresh, org_id="org", connection_id=CONN) == FIELDS


def test_a_row_encrypted_with_a_key_that_is_no_longer_configured_says_which_one(key, monkeypatch) -> None:
    envelope = encrypt_fields(FIELDS, org_id="org", connection_id=CONN)
    monkeypatch.setattr(settings, "secret_encryption_key", generate_key())
    monkeypatch.setattr(settings, "secret_encryption_key_id", "k9")

    with pytest.raises(SecretEncryptionError, match="'k1'"):
        decrypt_fields(envelope, org_id="org", connection_id=CONN)


# ── the real store: what actually lands in the database column ───────────────

class _Row:
    def __init__(self, connection_id, org_id, values):
        self.connection_id, self.org_id, self.values = connection_id, org_id, values


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


@pytest.fixture
def table(monkeypatch):
    rows: dict[str, _Row] = {}

    class _Repo:
        def __init__(self, session):
            pass

        async def get(self, connection_id):
            return rows.get(connection_id)

        async def upsert(self, *, org_id, connection_id, values):
            rows[connection_id] = _Row(connection_id, org_id, values)
            return rows[connection_id]

        async def delete(self, connection_id):
            rows.pop(connection_id, None)

    monkeypatch.setattr(secret_store_module, "AsyncSessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(secret_store_module, "_ConnectionSecretRepo", _Repo)
    return rows


@pytest.mark.asyncio
async def test_the_marker_never_reaches_the_database_column(key, table) -> None:
    ref = await PostgresSecretStore().put(org_id="org", connection_id=CONN, fields=FIELDS)

    assert ref == secret_ref_for(CONN)
    stored = table[CONN].values
    assert is_encrypted(stored)
    assert MARKER not in json.dumps(stored)  # the raw JSONB value, exactly as Postgres would hold it
    assert await PostgresSecretStore().get(ref) == FIELDS  # and it round-trips for the live operations that need it


@pytest.mark.asyncio
async def test_a_legacy_plaintext_row_is_still_readable_but_the_next_write_encrypts_it(key, table) -> None:
    table[CONN] = _Row(CONN, "org", dict(FIELDS))  # a row written before encryption existed

    assert await PostgresSecretStore().get(secret_ref_for(CONN)) == FIELDS

    await PostgresSecretStore().put(org_id="org", connection_id=CONN, fields=FIELDS)
    assert is_encrypted(table[CONN].values)
    assert MARKER not in json.dumps(table[CONN].values)


@pytest.mark.asyncio
async def test_without_a_key_a_secret_is_refused_not_stored_in_clear(table, monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_encryption_key", "")

    with pytest.raises(SecretEncryptionError):
        await PostgresSecretStore().put(org_id="org", connection_id=CONN, fields=FIELDS)

    assert table == {}  # nothing was written at all


@pytest.mark.asyncio
async def test_a_reference_this_store_does_not_own_is_ignored(key, table) -> None:
    assert await PostgresSecretStore().get("vault://dataconnect/org/name") is None
    assert await PostgresSecretStore().get(None) is None
