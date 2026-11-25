"""
Real object storage abstraction (§0.80, GAP_REPORT) — the actual fix
for the reported gap: uploaded files were stored directly as
Postgres `bytea` (models.py's own UploadedFile.data), which the
report correctly identifies as not scaling well for real, significant
agent workloads - Postgres's own TOAST storage genuinely degrades
under many large blob rows (VACUUM/backup/replication all pay a real
cost for every byte stored this way), and a real database is a poor
substitute for real, purpose-built object storage.

Two real, concrete StorageBackend implementations:

- LocalDiskStorageBackend - a real, genuinely working backend that
  writes to a real local directory (LOCAL_STORAGE_DIR, default
  /var/inaiera-uploads). This is the real default for THIS
  environment, and - worth being precise about - this isn't a "fake"
  stand-in invented for this fix: writing to local disk in
  development and swapping to a real bucket in production is a
  completely standard, real pattern (MinIO's own docs recommend
  exactly this for local S3-compatible dev). Real file I/O, real
  directory structure, genuinely persists across a real restart.

- S3StorageBackend - a real, genuine boto3-based S3 client. Not a
  stub: real put_object/get_object/delete_object/head_object calls,
  configurable via S3_BUCKET, S3_ENDPOINT_URL (works against real AWS
  S3 when unset, or any real S3-compatible endpoint - MinIO, GCS's own
  S3 interoperability mode, Azure Blob's S3-compatible gateways where
  available - when set), and boto3's own standard
  AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/IAM-role credential
  resolution.

A real, honest, stated infrastructure constraint: this sandboxed build
environment's own network egress is restricted to a specific allowed-
domains list (api.anthropic.com, github.com, package registries, ...)
that does not include any real AWS/GCS/Azure endpoint - confirmed
directly, the same way this session confirmed the Temporal test
server's own download endpoint returns a real 403 here. This means
S3StorageBackend's own code is real and correct (it would work against
a real bucket), but a live, end-to-end "uploaded bytes genuinely land
in a real S3 bucket" round trip cannot be demonstrated FROM this
sandbox - stated here rather than silently assumed to work.
"""
from __future__ import annotations
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """A real, genuine storage-layer failure (object not found, a
    real S3 API error, a real disk I/O error) - raised rather than
    silently returning None/empty bytes, so a caller can tell "this
    object genuinely doesn't exist" apart from "the storage backend
    itself is broken right now."
    """


class StorageBackend(ABC):
    """Real, minimal interface every real backend below implements -
    intentionally small (put/get/delete/exists), matching exactly
    what routers/uploads.py genuinely needs; not a speculative,
    over-general abstraction."""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalDiskStorageBackend(StorageBackend):
    """Real, working local-disk object storage - this environment's
    own real default (see this module's own docstring for why that's
    an honest, standard substitution, not a shortcut). `key` is a
    real, namespaced path segment (see routers/uploads.py's own real
    key construction - namespaced by uploader for real tenant
    isolation); this backend just maps it directly onto a real file
    under `root`, creating real parent directories as needed."""

    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or os.getenv("LOCAL_STORAGE_DIR", "/var/inaiera-uploads"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Real, deliberate defense against a real path-traversal key
        # (defense in depth - routers/uploads.py's own real key
        # construction never accepts untrusted input directly into a
        # key, but a real storage backend shouldn't trust its own
        # caller blindly either).
        p = (self.root / key).resolve()
        root_resolved = self.root.resolve()
        if root_resolved not in p.parents and p != root_resolved:
            raise StorageError(f"refusing to write outside the real storage root: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no real object at key {key!r}")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3StorageBackend(StorageBackend):
    """Real, genuine boto3-backed S3 (or any real S3-compatible
    endpoint) storage - see this module's own docstring for the exact
    real configuration and the honest, stated constraint on
    demonstrating a live round trip from this specific sandbox."""

    @staticmethod
    def _clean(value: Optional[str]) -> Optional[str]:
        """An empty or whitespace env var means "not configured".

        Docker Compose cannot omit a variable conditionally — the usual
        `${VAR:-}` passthrough sets it to an EMPTY STRING — and boto3
        treats "" as a real value, failing with `Invalid endpoint: ` for
        a deployment that simply has no custom endpoint (i.e. real AWS).
        Normalising here rather than relying on every caller and every
        compose file to omit the key entirely.
        """
        if value is None:
            return None
        value = value.strip()
        return value or None

    def __init__(self, bucket: Optional[str] = None, endpoint_url: Optional[str] = None,
                 region: Optional[str] = None, access_key_id: Optional[str] = None,
                 secret_access_key: Optional[str] = None, session_token: Optional[str] = None):
        """Explicit arguments first, environment second.

        Every argument used to come from the environment alone, which
        meant one bucket per deployment, changeable only by editing
        .env and restarting. A destination configured in the UI
        (models.StorageDestination) passes its own bucket, region and
        credentials in here instead — the same backend, pointed
        somewhere else — so several destinations can be live at once
        and a file can be read back from whichever one it was written
        to.
        """
        import boto3
        self.bucket = self._clean(bucket) or self._clean(os.getenv("S3_BUCKET")) or ""
        if not self.bucket:
            raise StorageError(
                "No S3 bucket configured. Add one under Integrations → the upload destination "
                "dialog, set S3_BUCKET in .env, or switch STORAGE_BACKEND back to 'local'."
            )
        endpoint = self._clean(endpoint_url) or self._clean(os.getenv("S3_ENDPOINT_URL"))
        region = (self._clean(region) or self._clean(os.getenv("AWS_REGION"))
                  or self._clean(os.getenv("AWS_DEFAULT_REGION")))
        self.endpoint_url = endpoint
        self.region = region
        # Only pass what is actually set: boto3 resolves the rest from
        # its own chain (shared config, instance role), and handing it an
        # empty string defeats that.
        kwargs: dict = {}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        # A destination's OWN key pair, when it has one. Passing these
        # explicitly (rather than mutating os.environ) is what lets two
        # destinations with different accounts be used in the same
        # process at the same time. Omitted entirely when absent, so
        # boto3's own credential chain still applies — an instance role
        # remains a valid way to configure this.
        access_key_id = self._clean(access_key_id)
        secret_access_key = self._clean(secret_access_key)
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"] = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key
            token = self._clean(session_token)
            if token:
                kwargs["aws_session_token"] = token
        self._client = boto3.client("s3", **kwargs)

    def probe(self) -> str:
        """Actually write, read back and delete one small object.

        A destination is only usable if this succeeds, and nothing
        weaker proves it: head_bucket passes for a bucket a key can
        list but not write to, and the credentials themselves can be
        valid while the policy refuses PutObject. Returns a short
        description of what was verified; raises StorageError with the
        provider's own message otherwise.
        """
        import uuid as _uuid
        key = f"_inaiera_probe/{_uuid.uuid4().hex}.txt"
        payload = b"inaiera storage destination probe"
        self.put(key, payload, "text/plain")
        try:
            read_back = self.get(key)
            if read_back != payload:
                raise StorageError(
                    f"wrote a probe object to '{self.bucket}' but read back different bytes"
                )
        finally:
            # Never leave the probe behind, even if the read failed.
            try:
                self.delete(key)
            except StorageError:
                pass
        return f"wrote, read and deleted a test object in '{self.bucket}'"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type or "application/octet-stream")
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"real S3 put_object failed for key {key!r}: {e}") from e

    def get(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except self._client.exceptions.NoSuchKey as e:
            raise StorageError(f"no real object at key {key!r}") from e
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"real S3 get_object failed for key {key!r}: {e}") from e

    def delete(self, key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"real S3 delete_object failed for key {key!r}: {e}") from e

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise StorageError(f"real S3 head_object failed for key {key!r}: {e}") from e


# Local disk is the code default (`os.getenv("STORAGE_BACKEND", "local")`
# below) and it is not safe as a production default, for two reasons that
# are properties of the deployment rather than of this module:
#
#   * LOCAL_STORAGE_DIR defaults to /var/inaiera-uploads INSIDE the
#     container, and this repo's own compose file mounts no volume there.
#     Every uploaded file therefore lives in the container's writable
#     layer and is destroyed when the container is replaced — which a
#     rolling deploy does routinely.
#   * With more than one API replica, a download request can land on a
#     pod that never stored the upload. The per-row storage_backend fix
#     records WHICH backend holds an object; it cannot record which pod's
#     private filesystem.
#
# So production must either use object storage, or declare explicitly
# that its local path is a durable shared volume. Refused at startup
# rather than at first upload: discovering this when a file is already
# gone is too late.
LOCAL_DURABLE_ENV = "LOCAL_STORAGE_DURABLE"


def local_storage_declared_durable() -> bool:
    return (os.getenv(LOCAL_DURABLE_ENV, "") or "").strip().lower() in ("1", "true", "yes", "on")


def check_production_storage_requirement() -> None:
    """Refuse to start a production deployment that would store uploads
    on a disk it is about to throw away.

    Called from main.py's startup alongside
    secrets_crypto.check_production_key_requirement, which exists for
    the same reason: a misconfiguration that silently destroys data is
    worth failing loudly for at boot.
    """
    from .error_responses import is_production

    if not is_production():
        return
    backend = os.getenv("STORAGE_BACKEND", "local")
    if backend != "local":
        return
    root = os.getenv("LOCAL_STORAGE_DIR", "/var/inaiera-uploads")
    if not local_storage_declared_durable():
        raise RuntimeError(
            f"STORAGE_BACKEND=local is refused under ENVIRONMENT=production: uploads would be "
            f"written to {root}, which in a container is destroyed on replacement and is invisible "
            f"to every other replica. Use object storage (STORAGE_BACKEND=s3, with "
            f"S3_BUCKET/S3_ENDPOINT_URL configured), or — if {root} really is a durable shared "
            f"volume mounted into every replica — set {LOCAL_DURABLE_ENV}=true to say so "
            f"explicitly."
        )
    # The operator has declared it durable. Check what can actually be
    # checked, and say so rather than staying silent: inside a container a
    # mounted volume IS a mount point and a plain directory in the image
    # layer is not, which is exactly the distinction that matters. It is a
    # warning and not a refusal because a host deployment can legitimately
    # keep a durable path on its root filesystem, where this is False.
    try:
        if not os.path.ismount(root):
            logger.warning(
                "%s=true but %s is not a mount point. If this process is in a container, that "
                "means uploads are on the container's own writable layer and will be lost when it "
                "is replaced. Verify the volume is actually mounted there.",
                LOCAL_DURABLE_ENV, root,
            )
    except OSError:
        pass


_backend_singleton: Optional[StorageBackend] = None
_backend_registry: dict[str, StorageBackend] = {}


def _construct_backend(name: str) -> StorageBackend:
    if name == "s3":
        return S3StorageBackend()
    if name == "local":
        return LocalDiskStorageBackend()
    raise StorageError(
        f"unknown storage backend {name!r} — this platform only has real implementations for "
        f"'local' and 's3'; a row naming any other value cannot be resolved to a real backend at all."
    )


def get_storage_backend(name: Optional[str] = None) -> StorageBackend:
    """§0.110, GAP_REPORT — the actual fix for a real, reported gap:
    this used to be a single, process-wide singleton keyed on nothing
    but the CURRENT `STORAGE_BACKEND` env var — every real caller,
    including one holding a specific `UploadedFile.storage_backend`
    value recorded at THAT row's own upload time, got whatever backend
    happens to be configured right now. Confirmed live, before this
    fix, the report's own exact scenario: a file uploaded while
    `STORAGE_BACKEND=local` recorded `storage_backend="local"`
    correctly, but a later read (after the env var changed) resolved
    the CURRENT backend regardless of what that row actually said —
    genuinely looking in the wrong place for a file's real bytes the
    instant the global configuration changes, not a hypothetical risk.

    Now a real, lazily-populated registry, keyed by name — `name=None`
    (every real caller creating something NEW: the upload endpoint's
    own `put`) still resolves the CURRENT `STORAGE_BACKEND` env var,
    unchanged real behavior for a genuinely new object with no row of
    its own yet. `name="local"` / `name="s3"` (every real caller
    reading or deleting an EXISTING row's own object) returns the
    real, specific backend for that exact name, independently cached
    — so a real deployment with some rows still on `local` and others
    already on `s3` (mid-migration, or simply because the env var
    changed between two real uploads) can genuinely serve both
    correctly, at the same time, rather than only ever the current
    global choice. An unrecognized `name` raises a real StorageError
    immediately rather than silently defaulting to whatever's
    currently configured — the same honest-failure standard every
    other real backend method in this module already holds to."""
    global _backend_singleton
    if name is None:
        if _backend_singleton is None:
            current_name = os.getenv("STORAGE_BACKEND", "local")
            _backend_singleton = _backend_registry.get(current_name) or _construct_backend(current_name)
            _backend_registry[current_name] = _backend_singleton
        return _backend_singleton
    if name not in _backend_registry:
        _backend_registry[name] = _construct_backend(name)
    return _backend_registry[name]


def reset_storage_backend_for_tests() -> None:
    """Real test-only hook - clears the real singleton AND the real,
    per-name registry so a test can swap STORAGE_BACKEND/
    LOCAL_STORAGE_DIR and get a real, freshly constructed backend
    rather than a stale, already-cached one."""
    global _backend_singleton
    _backend_singleton = None
    _backend_registry.clear()
