"""Durable artifact store (RUN-DBT-002c): local and S3 backends."""
from __future__ import annotations

import io
import json
import uuid

import pytest

from app.core.config import Settings
from app.services.artifact_store import (
    LocalArtifactStore, S3ArtifactStore, get_artifact_store, get_run_result, put_run_result, result_key,
)


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            err = Exception("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def _project_with_artifacts(tmp_path):
    target = tmp_path / "proj" / "target"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps({"nodes": {}}))
    (target / "run_results.json").write_text(json.dumps({"results": []}))
    return tmp_path / "proj"


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_put_and_get_run_result_roundtrip(tmp_path, backend):
    store = (LocalArtifactStore(tmp_path / "store") if backend == "local"
             else S3ArtifactStore(bucket="runs", prefix="dbtrunner", client=FakeS3()))
    run_id = uuid.uuid4()
    result = {"success": True, "errors": [], "nodes": [], "manifest": {"a": 1}}
    ref = put_run_result(store, run_id=run_id, attempt=2, result=result,
                         project_dir=_project_with_artifacts(tmp_path))
    assert ref["uri"].endswith(result_key(run_id, 2))
    got = get_run_result(store, run_id=run_id, attempt=2, ref=ref["uri"], sha256=ref["sha256"])
    assert got["manifest"] == {"a": 1}
    # deterministic key works without the reference (logs gone)
    assert get_run_result(store, run_id=run_id, attempt=2)["success"] is True
    # raw dbt artifacts were stored too
    assert store.get_bytes(f"runs/{run_id}/attempt-2/manifest.json") is not None
    assert get_run_result(store, run_id=run_id, attempt=3) is None


def test_checksum_mismatch_is_rejected(tmp_path):
    store = LocalArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    ref = put_run_result(store, run_id=run_id, attempt=1, result={"success": True})
    assert get_run_result(store, run_id=run_id, attempt=1, ref=ref["uri"], sha256="0" * 64) is None


def test_local_store_refuses_paths_outside_root(tmp_path):
    store = LocalArtifactStore(tmp_path / "root")
    with pytest.raises(ValueError):
        store.get_bytes("file:///etc/passwd")


def test_s3_store_refuses_other_buckets():
    store = S3ArtifactStore(bucket="runs", client=FakeS3())
    with pytest.raises(ValueError):
        store.get_bytes("s3://someone-else/x")


def test_factory():
    assert get_artifact_store(Settings(artifact_store_backend="none")) is None
    assert isinstance(get_artifact_store(Settings(artifact_store_backend="local")), LocalArtifactStore)
    with pytest.raises(ValueError):
        get_artifact_store(Settings(artifact_store_backend="s3"))
