"""Names for Dagster code locations (folders) and jobs, derived from runner projects.

A dbt Runner project is a repo plus (optionally) a folder inside it. In Dagster:

  * code location (the folder on the Jobs page) = ``<repo name>-<8 hex of a hash of the CANONICAL repository URL>``, e.g.
                                                  ``inaiera-dbt-test-3f9a1c2b`` (DG-012). The name is the location's IDENTITY; the
                                                  repo name in front is only there so a human can read it.
  * job inside it                                = the project's folder, e.g. ``customers``

Why not just the repo name: ``orgA/analytics.git`` and ``orgB/analytics.git`` are both ``analytics`` and used to be ONE location (their
projects mixed in one code server, reloaded and routed together). The hash is of the canonical URL — host and path, without user info,
``.git``, a ``#subdirectory`` suffix or the transport — so every spelling of one repository gives one location and two repositories never
share one.

Dagster job names may only contain letters, digits and ``_``, so anything else is
replaced by ``_`` and the names within one location are made unique.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

_JOB_SAFE = re.compile(r"[^A-Za-z0-9_]")


def repo_name(git_url: str) -> str:
    """'https://github.com/acme/inaiera-dbt-test.git#x' -> 'inaiera-dbt-test'."""
    base = (git_url or "").split("#", 1)[0].rstrip("/")
    last = re.split(r"[/:]", base)[-1] if base else ""
    if last.endswith(".git"):
        last = last[:-4]
    return last or "repo"


_DEFAULT_PORTS = {"http": 80, "https": 443, "ssh": 22, "git": 9418}
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")          # git@github.com:acme/repo.git


def canonical_repo_url(git_url: str) -> str:
    """The identity of a repository: ``host[:port]/path`` — lower-case host, no user info, no query/fragment (``#subdirectory=…``), no
    trailing ``/`` or ``.git``, and no transport (https, ssh and scp-style spellings of one repository are the same repository). A
    non-default port is kept. Never raises; an unparsable value canonicalises to itself, stripped.

    >>> canonical_repo_url("https://ci:tok@GitHub.com/acme/analytics.git#subdirectory=x")
    'github.com/acme/analytics'
    >>> canonical_repo_url("git@github.com:acme/analytics.git")
    'github.com/acme/analytics'
    """
    raw = (git_url or "").strip().split("#", 1)[0]
    host = path = ""
    if "://" in raw:
        try:
            parts = urlsplit(raw)
            port = parts.port
            host = (parts.hostname or "").lower()
            if port and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
                host = f"{host}:{port}"
            path = parts.path
        except ValueError:
            return raw.rstrip("/")
    else:
        m = _SCP_LIKE.match(raw)
        if m:
            host, path = m.group(1).lower(), m.group(2)
        else:
            return raw.rstrip("/")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}".strip("/")


def repo_hash(git_url: str) -> str:
    """Short, stable (the same on every restart and every machine) digest of the canonical repository URL."""
    return hashlib.sha256(canonical_repo_url(git_url).encode()).hexdigest()[:8]


_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def location_label(project: dict[str, Any]) -> str:
    """The human-friendly repo name, for display only (it is NOT unique)."""
    return _LABEL_SAFE.sub("-", repo_name(project.get("git_url", ""))).strip("-.")[:40] or "repo"


def location_name(project: dict[str, Any]) -> str:
    """The code location's identity: ``<label>-<hash of the canonical repository URL>`` (DG-012)."""
    git_url = project.get("git_url", "")
    return f"{location_label(project)}-{repo_hash(git_url)}"


def _safe(text: str) -> str:
    out = _JOB_SAFE.sub("_", text).strip("_") or "project"
    return out if not out[0].isdigit() else f"p_{out}"


def build_job_names(projects: list[dict[str, Any]]) -> dict[str, str]:
    """project id -> unique Dagster job name for the projects of ONE location."""
    ordered = sorted(projects, key=lambda p: (p.get("project_path") or "", p["id"]))

    def base(p: dict[str, Any]) -> str:
        path = (p.get("project_path") or "").strip("/")
        leaf = path.split("/")[-1]
        if p.get("kind", "dbt") != "dbt" and "." in leaf:      # a SQL / Python job's file: jobs/daily_totals.sql -> daily_totals
            leaf = leaf.rsplit(".", 1)[0]
        return _safe(leaf) if path else _safe(repo_name(p.get("git_url", "")))

    counts: dict[str, int] = {}
    for p in ordered:
        counts[base(p)] = counts.get(base(p), 0) + 1

    names: dict[str, str] = {}
    taken: set[str] = set()
    for p in ordered:
        name = base(p)
        if counts[name] > 1:  # same folder name in different parents: use the whole path
            name = _safe((p.get("project_path") or "").replace("/", "__"))
        while name in taken:
            name = f"{name}_{p['id'][:6]}"
        taken.add(name)
        names[p["id"]] = name
    return names


def location_fingerprint(projects: list[dict[str, Any]]) -> tuple:
    """What a location's jobs and assets depend on. When this changes the location is reloaded (a project's data upstream changes the
    data lineage, its Upstream Jobs change the job lineage); schedule changes do not (the sensors read schedules live)."""
    return tuple(sorted((p["id"], p.get("project_path") or "", p.get("git_url", ""),
                         tuple(sorted(p.get("data_upstream_project_ids") or [])),
                         tuple(sorted(p.get("upstream_project_ids") or []))) for p in projects))


def group_by_location(projects: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for p in projects:
        grouped.setdefault(location_name(p), []).append(p)
    return grouped
