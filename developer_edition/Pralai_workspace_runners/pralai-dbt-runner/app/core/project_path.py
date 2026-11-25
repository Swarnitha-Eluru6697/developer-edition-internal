"""Where a dbt project lives inside its Git repo.

One repo can hold several dbt projects (each folder has its own
dbt_project.yml). A registered project is therefore identified by
(git_url, git_ref, project_path); an empty project_path means "the repo
root, or the only dbt_project.yml in it" (the original behaviour).

Callers that can only supply a URL (e.g. a UI with no folder field) may append
a fragment instead:  https://github.com/acme/repo.git#subdirectory=dbt/sales
"""
from __future__ import annotations

from urllib.parse import parse_qs

_FRAGMENT_KEYS = ("subdirectory", "path")


def normalize_project_path(raw: str | None) -> str:
    """'' for the repo root, else a clean relative POSIX path ('a/b').
    Rejects anything that could point outside the clone."""
    if raw is None:
        return ""
    path = raw.strip().replace("\\", "/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("project_path must not contain '..'")
    if path.startswith("/") or (parts and parts[0].endswith(":")):
        raise ValueError("project_path must be relative to the repo root")
    if ".git" in parts:
        raise ValueError("project_path must not point inside .git")
    return "/".join(parts)


def split_git_url(git_url: str) -> tuple[str, str]:
    """(url without fragment, project_path from a #subdirectory=/#path= fragment)."""
    base, sep, fragment = git_url.partition("#")
    if not sep:
        return git_url, ""
    params = parse_qs(fragment)
    for key in _FRAGMENT_KEYS:
        if params.get(key):
            return base, normalize_project_path(params[key][0])
    return base, ""
