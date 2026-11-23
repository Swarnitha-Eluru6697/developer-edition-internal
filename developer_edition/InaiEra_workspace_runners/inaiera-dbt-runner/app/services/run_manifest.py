"""Immutable execution manifest of a run (DBT-006, DBT-007).

A run used to remember only its `select`. Everything else that decides WHAT it executes was read from the project
row when the run was finally dispatched, so a queued run could build a branch that had moved on since submission
(DBT-006) under DQ rules that had been replaced since submission (DBT-007).

Now submission resolves and freezes, in one document stored on the run row:

  * the Git commit SHA the project's ref points to (ls-remote; the clone is then forced to that exact commit),
  * the dbt project folder, engine and selector,
  * every DQ schema block the project holds at that moment: its YAML, our own sha256 of it, and the DQ
    artifact version / binding / content hash / renderer version it was applied with.

Every later step (dispatch, a requeue after a crash, adoption by another replica, a retry of an attempt) reads the
manifest, never the project row. Its sha256 is stored beside it and re-checked before use, and a database trigger
(migration 0009) refuses to change a manifest once it is set. A different commit or different DQ rules means a NEW
run, which takes its own snapshot.

The manifest carries no Git credential: the URL is stored without its user info, and the clone credential is still
taken from the project's URL at dispatch (so a rotated token keeps working), after checking that it names the same
repository.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog

from app.core.config import get_settings

log = structlog.get_logger()

MANIFEST_VERSION = "1"
_ACCEPTED_VERSIONS = {"1", 1}          # 1 (an int) is what the first runs frozen on 2026-10-14 carry
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitRefError(Exception):
    """A project's Git ref could not be turned into a commit. `code` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ManifestIntegrityError(Exception):
    """The stored manifest no longer matches the hash recorded when the run was submitted."""


def redact_url(url: str) -> str:
    """The URL without user info (`https://user:token@host/x` -> `https://host/x`). Never raises."""
    try:
        parts = urlsplit(url)
        if parts.username is None and parts.password is None:
            return url
        host = parts.hostname or ""
        if ":" in host:                      # IPv6 literal
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return "<unparsable url>"


def scrub(text: str, git_url: str) -> str:
    """Remove the raw URL (which may embed a credential) from git's own error text."""
    text = text.replace(git_url, redact_url(git_url))
    parts = urlsplit(git_url) if "://" in git_url else None
    for secret in filter(None, (parts.password if parts else None, parts.username if parts else None)):
        text = text.replace(secret, "***")
    return text.strip()[-500:]


def _ls_remote(git_url: str, patterns: list[str], timeout: int, token: str | None = None) -> dict[str, str]:
    from app.core.git_credentials import git_env
    env = {**os.environ, **git_env(git_url, token)}
    try:
        proc = subprocess.run(["git", "ls-remote", git_url, *patterns], capture_output=True, text=True,
                              timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise GitRefError("GIT_UNREACHABLE", f"timed out after {timeout}s reading {redact_url(git_url)}")
    if proc.returncode != 0:
        raise GitRefError("GIT_UNREACHABLE",
                          f"cannot read {redact_url(git_url)}: {scrub(proc.stderr, git_url)}")
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name and _FULL_SHA.match(sha):
            refs[name] = sha
    return refs


def resolve_commit_blocking(git_url: str, git_ref: str, token: str | None = None) -> str:
    """The commit `git_ref` points to right now: a branch, a tag (annotated tags are peeled to their commit) or a
    full 40-character SHA (already immutable, used as is). A short SHA cannot be resolved remotely and is refused."""
    ref = (git_ref or "").strip()
    if _FULL_SHA.match(ref.lower()):
        return ref.lower()
    if not ref:
        raise GitRefError("GIT_REF_NOT_FOUND", "the project has no Git ref")
    if ref.startswith("refs/"):
        patterns = [ref, f"{ref}^{{}}"]
    else:
        patterns = [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"]
    refs = _ls_remote(git_url, patterns, get_settings().git_resolve_timeout_seconds, token)
    for name in (f"refs/heads/{ref}", f"{ref}^{{}}", f"refs/tags/{ref}^{{}}", ref, f"refs/tags/{ref}"):
        if name in refs:
            return refs[name]
    raise GitRefError("GIT_REF_NOT_FOUND",
                      f"ref '{ref}' does not exist in {redact_url(git_url)} (not a branch, tag or full commit SHA)")


async def resolve_commit(git_url: str, git_ref: str, token: str | None = None) -> str:
    """Async wrapper (git is a blocking subprocess). Tests replace this name."""
    return await asyncio.to_thread(resolve_commit_blocking, git_url, git_ref, token)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _canonical(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def dq_snapshot(blocks: dict[str, Any] | None, metas: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{target model: the block and everything that says which DQ artifact it is}. A block pushed without artifact
    metadata (the legacy shape) is kept with its artifact fields empty, so an audit sees it is unattributed."""
    blocks, metas = blocks or {}, metas or {}
    out: dict[str, dict[str, Any]] = {}
    for model in sorted(blocks):
        yaml_text = blocks[model]
        meta = metas.get(model) or {}
        out[model] = {
            "yaml": yaml_text,
            "yaml_sha256": sha256_text(yaml_text),
            "artifact_version_id": meta.get("artifact_version_id"),
            "binding_id": meta.get("binding_id"),
            "content_hash": meta.get("content_hash"),
            "renderer_version": meta.get("renderer_version"),
            "idempotency_key": meta.get("idempotency_key"),
            "applied_at": meta.get("applied_at"),
        }
    return out


def runtime_versions() -> dict[str, str]:
    """The software that will execute the run: the dbt-core and runner versions installed here (the Job runs the
    same image)."""
    from importlib.metadata import PackageNotFoundError, version

    def _v(name: str) -> str:
        try:
            return version(name)
        except PackageNotFoundError:
            return "unknown"
    return {"dbt_core": _v("dbt-core"), "runner": _v("inaiera-dbt-runner")}


def request_fingerprint(select: str | None, git_sha: str | None) -> str:
    """What an Idempotency-Key is bound to: the same key with a different request is refused, not replayed."""
    return sha256_text(json.dumps({"select": select, "git_sha": (git_sha or "").lower() or None},
                                  sort_keys=True, separators=(",", ":")))


def dq_artifacts(blocks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The audit summary of the frozen DQ blocks: which artifact (binding, version, hashes) governs which model."""
    return [{"model": m, "id": b.get("binding_id"), "version": b.get("artifact_version_id"),
             "sha256": b["yaml_sha256"], "content_hash": b.get("content_hash"),
             "renderer_version": b.get("renderer_version")} for m, b in sorted(blocks.items())]


def build_manifest(*, git_url: str, git_ref: str, git_sha: str, project_path: str, engine: str,
                   select: str | None, dq_blocks: dict[str, Any] | None,
                   dq_meta: dict[str, Any] | None, git_source: str = "ref", org_id: str = "",
                   workspace_id: str | None = None, subject_id: str | None = None, trigger: str | None = None,
                   idempotency_key: str | None = None, target_resource_id: str | None = None,
                   kind: str = "dbt", connection_id: str | None = None) -> dict[str, Any]:
    """The run's manifest (X-004, version "1"; the shared shape is in docs/EXECUTION_MANIFEST.md).

    `git_source` records where the commit came from: "ref" (resolved from the project's ref at submission) or
    "request" (pinned by the caller, DG-007: Dagster's git sensor). `target_resource_id` is None for dbt: the datasets
    a build writes are known only after it ran (they are registered from its results)."""
    snapshot = dq_snapshot(dq_blocks, dq_meta)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "trigger": trigger,
        "git": {"url": redact_url(git_url), "ref": git_ref, "sha": git_sha, "source": git_source},
        "project_path": project_path or "",
        "engine": engine,
        "select": select,
        "runtime_version": runtime_versions(),
        "target_resource_id": target_resource_id,
        "idempotency_key": idempotency_key,
        "dq_artifacts": dq_artifacts(snapshot),
        "dq": {"blocks": snapshot},
    }
    if kind != "dbt":       # a dbt run's manifest keeps exactly the shape it always had; a SQL/Python job's says what it is
        manifest["kind"] = kind
    if connection_id:       # a SQL job on a Workspace connection: which one is part of what the run froze
        manifest["connection_id"] = connection_id
    return manifest


def job_kind(manifest: dict[str, Any]) -> str:
    """What a frozen run executes: "dbt" (also every manifest written before SQL jobs existed), "sql" or "python"."""
    return str(manifest.get("kind") or "dbt")


def job_connection(manifest: dict[str, Any]) -> str | None:
    """The Workspace connection a SQL job runs on (frozen at submission), or None for the runner's own engine."""
    return manifest.get("connection_id") or None


def verify(manifest: dict[str, Any] | None, expected_sha256: str | None) -> dict[str, Any]:
    """The manifest, after checking it still hashes to what was recorded at submission and that every DQ block still
    matches its own hash. Raises ManifestIntegrityError otherwise."""
    if not manifest or not expected_sha256:
        raise ManifestIntegrityError("the run has no recorded manifest")
    if manifest_sha256(manifest) != expected_sha256:
        raise ManifestIntegrityError("the run's manifest does not match the hash recorded at submission")
    found = manifest.get("manifest_version", manifest.get("version"))
    if found not in _ACCEPTED_VERSIONS:
        raise ManifestIntegrityError(f"unsupported manifest version {found!r}")
    for model, block in (manifest.get("dq") or {}).get("blocks", {}).items():
        if sha256_text(block["yaml"]) != block["yaml_sha256"]:
            raise ManifestIntegrityError(f"DQ block for '{model}' does not match its recorded hash")
    return manifest


def dq_blocks(manifest: dict[str, Any]) -> dict[str, str]:
    """{target model: yaml} in the shape `_apply_dq_schema_tests` takes."""
    return {m: b["yaml"] for m, b in (manifest.get("dq") or {}).get("blocks", {}).items()}
