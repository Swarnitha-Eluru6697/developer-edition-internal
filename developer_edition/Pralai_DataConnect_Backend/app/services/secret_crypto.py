"""GAP-01: encryption at rest for connection credentials.

secret_store used to keep each connection's credential bundle in dc_connection_secrets as readable JSON, so
anyone with database or backup access could read every password. The bundle is now stored as an AES-256-GCM
envelope instead:

    {"v": 1, "alg": "AES-256-GCM", "kid": "k1", "nonce": "<b64>", "ct": "<b64>"}

- The key comes from the environment (DATACONNECT_SECRET_ENCRYPTION_KEY, 32 bytes, base64) — never from the
  database it protects. An HSM/KMS/Vault-backed SecretStore can replace this later behind the same protocol.
- `kid` names the key that encrypted the row, so the key can be rotated: set a new primary key and list the old
  one in DATACONNECT_SECRET_ENCRYPTION_PREVIOUS_KEYS ("k1:<b64>,k0:<b64>") — old rows keep decrypting, and new
  writes use the primary.
- The row's identity (org_id + connection_id) is bound in as associated data, so a ciphertext copied onto a
  different connection's row fails to decrypt instead of silently handing over the wrong credentials.
- A row that is not an envelope is a legacy plaintext bundle: still readable (so an existing deployment keeps
  working until alembic 0035 re-encrypts it) but never written again.

Nothing here logs or raises with a secret value; errors name the problem, not the data.
"""

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.errors import DataConnectError

ENVELOPE_VERSION = 1
ALGORITHM = "AES-256-GCM"
_ENVELOPE_KEYS = {"v", "alg", "kid", "nonce", "ct"}


class SecretEncryptionError(DataConnectError):
    """Credentials can't be encrypted or decrypted — misconfigured or wrong key. Deliberately generic."""

    code = "secret_store_unavailable"
    status_code = 500
    retryable = False


def _decode_key(raw: str, *, label: str) -> bytes:
    text = raw.strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SecretEncryptionError(f"{label} is not valid base64.") from exc
    if len(key) != 32:
        raise SecretEncryptionError(f"{label} must decode to exactly 32 bytes (AES-256); got {len(key)}.")
    return key


def generate_key() -> str:
    """A fresh key in the format DATACONNECT_SECRET_ENCRYPTION_KEY expects."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _keyring() -> tuple[str, dict[str, bytes]]:
    """(primary key id, {kid: key}) from settings. Raises if no primary key is configured."""
    if not settings.secret_encryption_key.strip():
        raise SecretEncryptionError(
            "Credential encryption is not configured: set DATACONNECT_SECRET_ENCRYPTION_KEY "
            "(a 32-byte base64 key; generate one with app.services.secret_crypto.generate_key())."
        )
    kid = settings.secret_encryption_key_id.strip() or "k1"
    keys = {kid: _decode_key(settings.secret_encryption_key, label="DATACONNECT_SECRET_ENCRYPTION_KEY")}
    for entry in filter(None, (e.strip() for e in settings.secret_encryption_previous_keys.split(","))):
        old_kid, _, old_key = entry.partition(":")
        if not old_key:
            raise SecretEncryptionError("DATACONNECT_SECRET_ENCRYPTION_PREVIOUS_KEYS entries must look like 'kid:key'.")
        keys.setdefault(old_kid.strip(), _decode_key(old_key, label=f"previous key '{old_kid.strip()}'"))
    return kid, keys


def validate_configuration() -> None:
    """Startup check: a configured key must be well-formed. (Whether one is REQUIRED is the caller's call.)"""
    if settings.secret_encryption_key.strip():
        _keyring()


def is_encrypted(values: dict[str, Any] | None) -> bool:
    return isinstance(values, dict) and _ENVELOPE_KEYS <= values.keys() and values.get("v") == ENVELOPE_VERSION


def _aad(org_id: str, connection_id: str) -> bytes:
    return f"{org_id}:{connection_id}".encode()


def encrypt_fields(fields: dict[str, Any], *, org_id: str, connection_id: str) -> dict[str, Any]:
    kid, keys = _keyring()
    nonce = os.urandom(12)
    ciphertext = AESGCM(keys[kid]).encrypt(nonce, json.dumps(fields).encode(), _aad(org_id, connection_id))
    return {
        "v": ENVELOPE_VERSION, "alg": ALGORITHM, "kid": kid,
        "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ciphertext).decode(),
    }


def decrypt_fields(envelope: dict[str, Any], *, org_id: str, connection_id: str) -> dict[str, Any]:
    _, keys = _keyring()
    key = keys.get(envelope["kid"])
    if key is None:
        raise SecretEncryptionError(
            f"The credential bundle was encrypted with key '{envelope['kid']}', which is not configured "
            "(add it to DATACONNECT_SECRET_ENCRYPTION_PREVIOUS_KEYS)."
        )
    try:
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ct"]), _aad(org_id, connection_id),
        )
    except (InvalidTag, binascii.Error, ValueError) as exc:
        raise SecretEncryptionError(
            "The credential bundle could not be decrypted (wrong key, corrupted, or not this connection's)."
        ) from exc
    return json.loads(plaintext)
