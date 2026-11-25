"""Azure Blob Storage backend for the data plane.

When STORAGE_BACKEND=azure_blob the data plane writes workspace files into
the customer's Azure Storage account. Keys are object paths under the
container; identical to the S3 implementation in semantics.
"""
from __future__ import annotations

import hashlib

from azure.storage.blob.aio import BlobServiceClient

from app.core.config import get_settings
from app.storage.base import BlobStore


class AzureBlobStore(BlobStore):
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.storage_endpoint_url:
            raise RuntimeError(
                "AZURE_STORAGE_CONNECTION_STRING / endpoint required for azure_blob backend"
            )
        self._svc = BlobServiceClient.from_connection_string(settings.storage_endpoint_url)
        self._container = self._svc.get_container_client(settings.storage_bucket)

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        blob = self._container.get_blob_client(key)
        from azure.storage.blob import ContentSettings
        await blob.upload_blob(
            data, overwrite=True,
            content_settings=ContentSettings(content_type=content_type) if content_type else None,
        )
        return hashlib.sha256(data).hexdigest()

    async def get(self, key: str) -> bytes:
        blob = self._container.get_blob_client(key)
        stream = await blob.download_blob()
        return await stream.readall()

    async def delete(self, key: str) -> None:
        blob = self._container.get_blob_client(key)
        await blob.delete_blob(delete_snapshots="include")

    async def exists(self, key: str) -> bool:
        blob = self._container.get_blob_client(key)
        return await blob.exists()

    async def list_prefix(self, prefix: str) -> list[str]:
        return [b.name async for b in self._container.list_blobs(name_starts_with=prefix)]

    async def delete_prefix(self, prefix: str) -> int:
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        count = 0
        async for blob in self._container.list_blobs(name_starts_with=prefix):
            await self._container.get_blob_client(blob.name).delete_blob(delete_snapshots="include")
            count += 1
        return count
