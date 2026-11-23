"""Upload destinations that can be configured while the platform runs.

Where an uploaded file goes was decided by three environment variables
read once at import: STORAGE_BACKEND, S3_BUCKET, AWS_*. That meant one
destination per deployment, changeable only by editing .env on the
server and restarting — and it was the reason the dev server and the
containers disagreed about where files lived for an entire afternoon.

A StorageDestination row is the same thing made dynamic: a bucket with
its own credentials, added and tested from the upload dialog, usable
immediately. The environment values remain a destination in their own
right — the one every existing file is already stored in — so nothing
has to be migrated and nothing stops working when the first row is
added.

Three rules this module exists to keep:

  * Credentials are encrypted at rest and never returned. Reads mask
    them, the same as a model provider key.
  * A file is read back from the destination it was WRITTEN to, never
    from whatever is currently default. An UploadedFile row carries
    its destination id for exactly this reason.
  * "This destination works" is a fact this platform has checked by
    actually writing, reading and deleting an object — not an
    assumption from a well-formed bucket name.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlmodel import Session, select

from .models import StorageDestination
from .secrets_crypto import decrypt_secret, encrypt_secret
from .storage import S3StorageBackend, StorageBackend, StorageError, get_storage_backend

MASK = "••••••••"

# The id used in the API for "wherever .env points". Not a row: it has no
# credentials of its own to store, and inventing a row for it would make
# the environment look editable from the UI when it is not.
ENV_DESTINATION_ID = "env"


class DestinationError(RuntimeError):
    """A destination is missing, misconfigured, or its secrets cannot be
    read. Carries a message meant for whoever is looking at the dialog."""


# Backends are cached per destination because constructing one builds a
# boto3 client, which is not free, and an upload of forty files would
# otherwise build forty. Keyed by id AND by a fingerprint of the row's
# own settings, so editing a destination's bucket or key takes effect
# immediately instead of being served by a stale client.
_cache: Dict[str, Tuple[str, StorageBackend]] = {}


def _fingerprint(dest: StorageDestination) -> str:
    return "|".join([
        dest.bucket or "", dest.region or "", dest.endpoint_url or "",
        (dest.access_key_id or "")[-12:], (dest.secret_access_key or "")[-12:],
        (dest.session_token or "")[-12:],
    ])


def backend_for(dest: StorageDestination) -> StorageBackend:
    """A live backend for one destination, decrypting its credentials."""
    if dest.provider != "s3":
        raise DestinationError(
            f"Destination '{dest.name}' names provider '{dest.provider}', which this platform "
            f"has no implementation for — only 's3' is wired."
        )
    fingerprint = _fingerprint(dest)
    cached = _cache.get(dest.id)
    if cached and cached[0] == fingerprint:
        return cached[1]
    try:
        access_key = decrypt_secret(dest.access_key_id)
        secret_key = decrypt_secret(dest.secret_access_key)
        token = decrypt_secret(dest.session_token)
    except Exception as e:
        # The same failure mode a KB password has when the encryption
        # key changed underneath it, and it needs the same honest
        # message: the remedy is to re-enter the credentials.
        raise DestinationError(
            f"Could not decrypt the credentials stored for '{dest.name}': {e}. "
            f"Re-enter them in the upload destination dialog."
        ) from e
    backend = S3StorageBackend(
        bucket=dest.bucket, endpoint_url=dest.endpoint_url, region=dest.region,
        access_key_id=access_key, secret_access_key=secret_key, session_token=token,
    )
    _cache[dest.id] = (fingerprint, backend)
    return backend


def env_backend() -> StorageBackend:
    """The destination .env describes. Resolved through storage.py's own
    registry so it stays the same object every other caller already
    uses."""
    return get_storage_backend()


def env_summary() -> Dict[str, Any]:
    """What .env configures, in the same shape a stored destination
    reports, so the dialog can list them together."""
    backend = (os.getenv("STORAGE_BACKEND") or "local").strip().lower()
    bucket = (os.getenv("S3_BUCKET") or "").strip()
    endpoint = (os.getenv("S3_ENDPOINT_URL") or "").strip()
    region = ((os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip())
    if backend == "s3":
        return {
            "id": ENV_DESTINATION_ID, "name": "Server configuration (.env)",
            "provider": "s3", "bucket": bucket or None, "region": region or None,
            "endpoint_url": endpoint or None,
            "configured": bool(bucket),
            "label": f"S3 · {bucket}" if bucket else "S3 (no bucket configured)",
            "editable": False, "is_default": True,
            "credentials": "from the server environment",
        }
    return {
        "id": ENV_DESTINATION_ID, "name": "Server configuration (.env)",
        "provider": "local", "bucket": None, "region": None, "endpoint_url": None,
        "configured": True, "label": "this server's disk",
        "editable": False, "is_default": True,
        "credentials": "not applicable",
    }


def describe(dest: StorageDestination) -> Dict[str, Any]:
    """One destination as the API reports it. Credentials are masked,
    never returned — the dialog shows that a key IS set, which is all it
    needs to decide whether to ask for a new one."""
    return {
        "id": dest.id, "name": dest.name, "provider": dest.provider,
        "bucket": dest.bucket, "region": dest.region, "endpoint_url": dest.endpoint_url,
        "configured": bool(dest.bucket),
        "label": f"S3 · {dest.bucket}",
        "editable": True,
        "is_default": dest.is_default,
        "credentials": MASK if dest.access_key_id else "this server's credential chain",
        "access_key_id": MASK if dest.access_key_id else None,
        "created_at": dest.created_at,
        "last_checked_at": dest.last_checked_at,
        "last_error": dest.last_error,
    }


def default_destination(session: Session) -> Optional[StorageDestination]:
    return session.exec(
        select(StorageDestination).where(StorageDestination.is_default == True)  # noqa: E712
    ).first()


def resolve(session: Session, destination_id: Optional[str]) -> Tuple[StorageBackend, Optional[str], str]:
    """The backend to write to, its destination id, and a label for it.

    `None` means "whatever is configured to be used now" — the default
    stored destination if one exists, else .env. An explicit id always
    wins, including the literal "env", so a caller can send an upload to
    the environment's bucket even when a stored destination is default.
    """
    if destination_id == ENV_DESTINATION_ID:
        return env_backend(), None, env_summary()["label"]
    if destination_id:
        dest = session.get(StorageDestination, destination_id)
        if not dest:
            raise DestinationError(
                f"No upload destination with id '{destination_id}'. It may have been deleted — "
                f"pick one in the upload destination dialog."
            )
        return backend_for(dest), dest.id, describe(dest)["label"]
    dest = default_destination(session)
    if dest:
        return backend_for(dest), dest.id, describe(dest)["label"]
    return env_backend(), None, env_summary()["label"]


def backend_for_stored_object(session: Session, destination_id: Optional[str],
                              storage_backend_name: Optional[str]) -> StorageBackend:
    """The backend that can reach an object already written.

    Reads and deletes must follow the row, not the current default: a
    file written to one bucket is not in another, and after a
    destination change every older download link would otherwise 404
    against the wrong bucket.
    """
    if destination_id:
        dest = session.get(StorageDestination, destination_id)
        if not dest:
            raise DestinationError(
                f"This file was stored in upload destination '{destination_id}', which no longer "
                f"exists, so its bytes cannot be reached. The object itself was not deleted."
            )
        return backend_for(dest)
    return get_storage_backend(storage_backend_name)


def build_unsaved(bucket: str, region: Optional[str], endpoint_url: Optional[str],
                  access_key_id: Optional[str], secret_access_key: Optional[str],
                  session_token: Optional[str] = None) -> S3StorageBackend:
    """A backend from credentials that have not been saved — used to
    TEST a destination before storing it, so a person is never asked to
    save something that does not work."""
    if not (bucket or "").strip():
        raise DestinationError("A bucket name is required.")
    return S3StorageBackend(
        bucket=bucket, endpoint_url=endpoint_url, region=region,
        access_key_id=access_key_id, secret_access_key=secret_access_key,
        session_token=session_token,
    )


def probe(backend: StorageBackend) -> Tuple[bool, str]:
    """Really write, read back and delete one object. Returns
    (ok, message) rather than raising: both outcomes are things the
    dialog displays."""
    try:
        if not hasattr(backend, "probe"):
            return False, "This backend cannot be tested."
        return True, backend.probe()
    except StorageError as e:
        return False, str(e)
    except Exception as e:  # boto3 raises plenty that is not StorageError
        return False, f"{type(e).__name__}: {e}"


def create(session: Session, *, name: str, bucket: str, region: Optional[str],
           endpoint_url: Optional[str], access_key_id: Optional[str],
           secret_access_key: Optional[str], session_token: Optional[str] = None,
           make_default: bool = True, verified: Optional[str] = None) -> StorageDestination:
    """Store a destination, encrypting its credentials on the way in."""
    from .secrets_crypto import require_durable_key
    # Refuses when the process is running on a throwaway encryption key:
    # credentials saved under one are unreadable after the next restart,
    # which is how this platform lost its provider keys twice before.
    require_durable_key("an upload destination's credentials")

    dest = StorageDestination(
        id=f"sd-{uuid.uuid4().hex[:12]}",
        name=(name or "").strip() or f"S3 · {bucket}",
        provider="s3",
        bucket=bucket.strip(),
        region=(region or "").strip() or None,
        endpoint_url=(endpoint_url or "").strip() or None,
        access_key_id=encrypt_secret((access_key_id or "").strip() or None),
        secret_access_key=encrypt_secret((secret_access_key or "").strip() or None),
        session_token=encrypt_secret((session_token or "").strip() or None),
        is_default=make_default,
        last_checked_at=datetime.utcnow() if verified else None,
        last_error=None,
    )
    if make_default:
        _clear_default(session)
    session.add(dest)
    session.commit()
    session.refresh(dest)
    return dest


def _clear_default(session: Session) -> None:
    for row in session.exec(
        select(StorageDestination).where(StorageDestination.is_default == True)  # noqa: E712
    ).all():
        row.is_default = False
        session.add(row)


def set_default(session: Session, destination_id: str) -> None:
    """Exactly one destination is default. `env` is expressed as no
    stored default at all, rather than a row standing in for the
    environment."""
    _clear_default(session)
    if destination_id != ENV_DESTINATION_ID:
        dest = session.get(StorageDestination, destination_id)
        if not dest:
            raise DestinationError(f"No upload destination with id '{destination_id}'.")
        dest.is_default = True
        session.add(dest)
    session.commit()


def forget(destination_id: str) -> None:
    """Drop a cached backend — after an edit or a delete."""
    _cache.pop(destination_id, None)
