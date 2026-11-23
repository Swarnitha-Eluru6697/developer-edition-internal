"""BlobStore factory. Pick implementation per STORAGE_BACKEND."""
from __future__ import annotations

from app.core.config import get_settings
from app.storage.base import BlobStore
from app.storage.s3 import S3BlobStore
from app.storage.secrets import get_secrets_backend as get_secrets_backend

# Azure and GCS implementations follow the same shape as S3BlobStore;
# included as stubs here for brevity. In production, implement them
# parallel to s3.py using azure-storage-blob and google-cloud-storage.

_store: BlobStore | None = None


def get_blob_store() -> BlobStore:
    global _store
    if _store is None:
        backend = get_settings().storage_backend
        if backend in ("s3", "local"):
            _store = S3BlobStore()
        elif backend == "azure_blob":
            from app.storage.azure_blob import AzureBlobStore
            _store = AzureBlobStore()
        elif backend == "gcs":
            from app.storage.gcs import GCSStore
            _store = GCSStore()
        else:
            raise ValueError(f"Unknown storage backend: {backend}")
    return _store


def storage_key_for_file(
    org_id: str,
    user_id: str,
    workspace_id: str,
    branch: str,
    path: str,
) -> str:
    """Canonical storage key. Keep stable — used in audit logs."""
    # Normalize the file path: no leading slash, no '..'
    safe_path = path.lstrip("/").replace("..", "")
    return f"{org_id}/{user_id}/{workspace_id}/{branch}/{safe_path}"
