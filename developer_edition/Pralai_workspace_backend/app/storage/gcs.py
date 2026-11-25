"""Google Cloud Storage backend for the data plane.

When STORAGE_BACKEND=gcs the data plane writes workspace files into a GCS
bucket inside the customer's GCP project.
"""
from __future__ import annotations

import asyncio
import hashlib

from google.cloud import storage as gcs

from app.core.config import get_settings
from app.storage.base import BlobStore


class GCSStore(BlobStore):
    def __init__(self) -> None:
        settings = get_settings()
        self._client = gcs.Client()
        self._bucket = self._client.bucket(settings.storage_bucket)

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        def _put() -> None:
            blob = self._bucket.blob(key)
            blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
        await asyncio.to_thread(_put)
        return hashlib.sha256(data).hexdigest()

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            blob = self._bucket.blob(key)
            return blob.download_as_bytes()
        return await asyncio.to_thread(_get)

    async def delete(self, key: str) -> None:
        def _del() -> None:
            self._bucket.blob(key).delete()
        await asyncio.to_thread(_del)

    async def exists(self, key: str) -> bool:
        def _ex() -> bool:
            return self._bucket.blob(key).exists()
        return await asyncio.to_thread(_ex)

    async def list_prefix(self, prefix: str) -> list[str]:
        def _ls() -> list[str]:
            return [b.name for b in self._bucket.list_blobs(prefix=prefix)]
        return await asyncio.to_thread(_ls)

    async def delete_prefix(self, prefix: str) -> int:
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")

        def _delete_all() -> int:
            count = 0
            for blob in self._bucket.list_blobs(prefix=prefix):
                blob.delete()
                count += 1
            return count

        return await asyncio.to_thread(_delete_all)
