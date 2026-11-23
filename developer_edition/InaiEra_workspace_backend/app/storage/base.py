"""BlobStore protocol.

Files in the workspace are stored as blobs in the customer-cloud object store:
  {bucket}/{org_id}/{user_id}/{workspace_id}/{branch}/{path}

Concrete implementations: s3.py, azure_blob.py, gcs.py.
The right backend is chosen at startup via STORAGE_BACKEND env var.
"""
from __future__ import annotations

from typing import Protocol


class BlobStore(Protocol):
    async def get(self, key: str) -> bytes:
        """Read a blob. Raises FileNotFoundError if absent."""
        ...

    async def put(self, key: str, content: bytes, content_type: str | None = None) -> str:
        """Write a blob, returning its sha256."""
        ...

    async def delete(self, key: str) -> None:
        """Delete a blob. No-op if it doesn't exist."""
        ...

    async def exists(self, key: str) -> bool:
        """Cheap existence check."""
        ...

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return all keys under a prefix."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every blob under a prefix (e.g. a whole workspace's files
        on workspace deletion — WS-LIFE-013). Returns the number deleted.
        No-op (returns 0) if nothing matches."""
        ...

    async def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy."""
        ...
