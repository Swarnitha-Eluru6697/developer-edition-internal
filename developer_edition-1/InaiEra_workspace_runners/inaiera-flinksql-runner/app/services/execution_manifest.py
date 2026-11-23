"""Immutable execution manifest of a Flink deployment (X-004).

A deployment used to remember only `sql_hash`. The SQL it submitted, the job's name and engine, and where the SQL came
from (a Git import) were read from the mutable `flink_jobs` row each time the deployment was (re)driven, so a retry or
a restart-resume could in principle run something other than what was requested, and an audit could only prove a hash.

Now `JobService.deploy` freezes, in the same transaction that claims the generation:

  * the SQL text itself and its sha256 (`sql_hash`),
  * the job's name, kind, engine (`sink_engine`), parallelism, restore savepoint and target resource (`ucs_fqn`),
  * who asked (`subject_id`), the org and workspace, the idempotency key,
  * the Git source the SQL came from (repository without credentials, requested ref, the exact commit, the path),
  * the runtime versions.

The deployment's execution (`_run_deploy`, including a reconciler resume after a crash) reads ONLY this manifest. Its
sha256 is stored next to it and re-checked before use, and a database trigger (migration 0008) refuses to change either
once set. Newer SQL or a new commit means a NEW deployment, which freezes its own manifest.

The shared shape across the platform is documented in InaiEra_workspace_backend/docs/EXECUTION_MANIFEST.md.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MANIFEST_VERSION = "1"


class ManifestIntegrityError(Exception):
    """The stored manifest no longer matches the hash recorded when the deployment was created."""


def redact_url(url: str) -> str:
    """The URL without user info. Never raises."""
    try:
        parts = urlsplit(url)
        if parts.username is None and parts.password is None:
            return url
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return "<unparsable url>"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def runtime_versions(flink_version: str) -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version
    try:
        runner = version("inaiera-flinksql-runner")
    except PackageNotFoundError:
        runner = "unknown"
    return {"flink": flink_version, "runner": runner}


def build_manifest(*, org_id: str, workspace_id: str | None, subject_id: str, job_id: str, job_name: str,
                   job_kind: str, generation: int, sql: str, engine: str, parallelism: int,
                   from_savepoint: str | None, source: dict[str, Any] | None, target_resource_id: str | None,
                   idempotency_key: str | None, flink_version: str,
                   dq_artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
    """`source` is the job's recorded Git provenance (already free of credentials) or None for SQL typed in by hand.
    `dq_artifacts` is the DQ artifact a QUALITY job's SQL was generated from, as the Data Quality service recorded it
    (X-004), or None: "not recorded" (every job that is not a DQ quality job, and a quality job created before DQ sent it)
    rather than a claim that there are none."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "job": {"id": job_id, "name": job_name, "kind": job_kind, "generation": generation},
        "source": source,
        "sql": {"text": sql, "sha256": sha256_text(sql)},
        "sql_hash": sha256_text(sql),
        "engine": engine,
        "parallelism": parallelism,
        "from_savepoint": from_savepoint,
        "runtime_version": runtime_versions(flink_version),
        "target_resource_id": target_resource_id,
        "dq_artifacts": dq_artifacts,
        "idempotency_key": idempotency_key,
    }


def verify(manifest: dict[str, Any] | None, expected_sha256: str | None) -> dict[str, Any]:
    """The manifest, after checking it still hashes to what was recorded and that its SQL still matches its own hash."""
    if not manifest or not expected_sha256:
        raise ManifestIntegrityError("the deployment has no recorded manifest")
    if manifest_sha256(manifest) != expected_sha256:
        raise ManifestIntegrityError("the deployment's manifest does not match the hash recorded when it was created")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestIntegrityError(f"unsupported manifest version {manifest.get('manifest_version')!r}")
    if sha256_text(manifest["sql"]["text"]) != manifest["sql"]["sha256"]:
        raise ManifestIntegrityError("the manifest's SQL does not match its recorded hash")
    return manifest
