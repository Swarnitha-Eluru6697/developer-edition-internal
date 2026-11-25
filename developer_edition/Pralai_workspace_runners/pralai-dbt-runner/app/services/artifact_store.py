"""Durable run-artifact storage (RUN-DBT-002 / RUN-DBT-003).

The isolated Job pod has no database access, so it used to hand its whole
result (manifest included — easily many MB) back as ONE line in its pod
log. That is fragile (log size limits, line splitting, log rotation) and it
vanishes with the pod when the Job is garbage-collected after its TTL —
after which a reconciler could only guess, and used to re-run the build.

Now the Job writes the result plus dbt's raw manifest.json/run_results.json
to an artifact store under a key derived purely from (run_id, attempt), and
prints only a small reference marker. The API side reads the result back
from the store — via the marker, or directly by the deterministic key when
the pod (and its logs) are already gone.

Backends:
  * s3    — any S3-compatible store (AWS S3, MinIO, Ceph, GCS interop).
            Required for k8s_job mode outside dev (the Job's filesystem is
            gone when the pod is).
  * local — a directory; for dev/subprocess mode or a shared RWX volume.
  * none  — disabled (the Job falls back to a size-capped inline result).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger()


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/json") -> str: ...
    def get_bytes(self, ref: str) -> bytes | None: ...
    def uri_for(self, key: str) -> str: ...


class LocalArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def uri_for(self, key: str) -> str:
        return f"file://{(self.root / key).as_posix()}"

    def _path(self, ref: str) -> Path:
        if ref.startswith("file://"):
            path = Path(ref[len("file://"):])
        else:
            path = self.root / ref
        resolved = path.resolve()
        if self.root.resolve() not in resolved.parents and resolved != self.root.resolve():
            raise ValueError(f"artifact ref escapes the store root: {ref}")
        return resolved

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/json") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic: readers never see a half-written result
        return self.uri_for(key)

    def get_bytes(self, ref: str) -> bytes | None:
        try:
            return self._path(ref).read_bytes()
        except FileNotFoundError:
            return None


class S3ArtifactStore:
    def __init__(self, *, bucket: str, prefix: str = "", endpoint_url: str | None = None,
                 region: str | None = None, client: Any = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3  # lazy: only needed when the s3 backend is selected

            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
        self._client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def uri_for(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/json") -> str:
        self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data,
                                ContentType=content_type)
        return self.uri_for(key)

    def get_bytes(self, ref: str) -> bytes | None:
        if ref.startswith("s3://"):
            bucket, _, key = ref[len("s3://"):].partition("/")
            if bucket != self.bucket:
                raise ValueError(f"artifact ref is outside the configured bucket: {ref}")
        else:
            key = self._key(ref)
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001 — botocore ClientError without importing botocore
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        return obj["Body"].read()


def get_artifact_store(settings) -> ArtifactStore | None:
    backend = settings.artifact_store_backend
    if backend == "s3":
        if not settings.artifact_s3_bucket:
            raise ValueError("ARTIFACT_STORE_BACKEND=s3 requires ARTIFACT_S3_BUCKET")
        return S3ArtifactStore(bucket=settings.artifact_s3_bucket, prefix=settings.artifact_s3_prefix,
                               endpoint_url=settings.artifact_s3_endpoint_url,
                               region=settings.artifact_s3_region)
    if backend == "local":
        return LocalArtifactStore(settings.artifact_store_local_dir)
    return None


# ---------------------------------------------------------------------
# Run-result helpers
# ---------------------------------------------------------------------
def run_prefix(run_id: uuid.UUID, attempt: int) -> str:
    return f"runs/{run_id}/attempt-{attempt}"


def result_key(run_id: uuid.UUID, attempt: int) -> str:
    return f"{run_prefix(run_id, attempt)}/result.json"


def put_run_result(store: ArtifactStore, *, run_id: uuid.UUID, attempt: int, result: dict,
                   project_dir: Path | None = None) -> dict:
    """Write dbt's raw artifacts (best-effort) and then the result document
    LAST — so the existence of result.json means the run is fully recorded.
    Returns a small reference {"uri", "sha256", "size"}."""
    if project_dir is not None:
        for name in ("manifest.json", "run_results.json"):
            path = Path(project_dir) / "target" / name
            if path.exists():
                try:
                    store.put_bytes(f"{run_prefix(run_id, attempt)}/{name}", path.read_bytes())
                except Exception:  # noqa: BLE001 — the result doc below is what matters
                    log.warning("artifact_raw_upload_failed", name=name, exc_info=True)
    data = json.dumps(result, default=str).encode()
    uri = store.put_bytes(result_key(run_id, attempt), data)
    return {"uri": uri, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def get_run_result(store: ArtifactStore, *, run_id: uuid.UUID, attempt: int,
                   ref: str | None = None, sha256: str | None = None) -> dict | None:
    data = store.get_bytes(ref or result_key(run_id, attempt))
    if data is None:
        return None
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        log.error("artifact_checksum_mismatch", run_id=str(run_id), attempt=attempt)
        return None
    result = json.loads(data)
    result["_artifact_uri"] = ref or store.uri_for(result_key(run_id, attempt))
    return result
