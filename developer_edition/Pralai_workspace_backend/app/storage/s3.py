"""S3-backed BlobStore. Works for AWS S3 and S3-compatible stores (MinIO).

Uses aioboto3-style client via asyncio.to_thread for boto3 calls (boto3 is sync).
For higher throughput swap for aioboto3 directly.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings


class S3BlobStore:
    def __init__(self):
        settings = get_settings()
        config = Config(
            region_name=settings.storage_region,
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        )
        kwargs: dict[str, Any] = {"config": config}
        if settings.storage_endpoint_url:
            # MinIO / local dev
            kwargs["endpoint_url"] = settings.storage_endpoint_url
        self._client = boto3.client("s3", **kwargs)
        self._bucket = settings.storage_bucket

    async def get(self, key: str) -> bytes:
        try:
            resp = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from e
            raise
        return resp["Body"].read()

    async def put(self, key: str, content: bytes, content_type: str | None = None) -> str:
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": content,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        await asyncio.to_thread(self._client.put_object, **kwargs)
        return hashlib.sha256(content).hexdigest()

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key,
            )
        except ClientError:
            pass

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.head_object, Bucket=self._bucket, Key=key,
            )
            return True
        except ClientError:
            return False

    async def list_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")

        def _list():
            results = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    results.append(obj["Key"])
            return results

        keys = await asyncio.to_thread(_list)
        return keys

    async def delete_prefix(self, prefix: str) -> int:
        # A bare "" prefix would delete the entire bucket — never allow it,
        # regardless of caller. Every real caller passes a workspace/file
        # prefix that's always non-empty by construction (storage_key_for_file,
        # Workspace.storage_prefix), so this only trips on a genuine bug.
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")

        def _delete_all() -> int:
            count = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if not keys:
                    continue
                # delete_objects takes up to 1000 keys per call — exactly the
                # page size list_objects_v2 already returns.
                self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})
                count += len(keys)
            return count

        return await asyncio.to_thread(_delete_all)

    async def copy(self, src_key: str, dst_key: str) -> None:
        await asyncio.to_thread(
            self._client.copy_object,
            Bucket=self._bucket,
            CopySource={"Bucket": self._bucket, "Key": src_key},
            Key=dst_key,
        )
