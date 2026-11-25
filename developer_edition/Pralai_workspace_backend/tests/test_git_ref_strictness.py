"""W-014: an explicitly requested Git ref is a requirement; only "no ref asked for" uses the repository default.

Before, `_do_clone` retried WITHOUT the requested branch after any failure (typo, deleted branch, auth error, transient error), so the
import/deploy silently used different code while the binding still recorded the branch the user asked for. Now a requested ref that
cannot be cloned fails the operation, an omitted ref clones the default branch, and both the requested ref and the actual immutable
commit are recorded.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _headers
from test_git_import_state import BODY, api  # noqa: F401  (fixture)

from app.api import git as git_api


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(cwd)}).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real repository: default branch `trunk` (commit A), branch `feature` (A + B), tag `v1` (A)."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "trunk")
    (src / "a.sql").write_text("select 'trunk'")
    _git(src, "add", "."); _git(src, "commit", "-qm", "A")
    a = _git(src, "rev-parse", "HEAD")
    _git(src, "tag", "v1")
    _git(src, "checkout", "-qb", "feature")
    (src / "a.sql").write_text("select 'feature'")
    _git(src, "commit", "-qam", "B")
    b = _git(src, "rev-parse", "HEAD")
    _git(src, "checkout", "-q", "trunk")
    return SimpleNamespace(url=f"file://{src}", a=a, b=b, tmp=tmp_path)


def _clone(repo, ref):
    dest = repo.tmp / f"dest-{ref or 'default'}"
    return dest, git_api._do_clone(repo.url, str(dest), ref)


def test_a_requested_branch_is_cloned_and_its_commit_reported(repo):
    dest, result = _clone(repo, "feature")
    assert (dest / "a.sql").read_text() == "select 'feature'"
    assert result.commit_sha == repo.b and result.branch == "feature"


def test_no_ref_asked_for_clones_the_default_branch(repo):
    dest, result = _clone(repo, None)
    assert (dest / "a.sql").read_text() == "select 'trunk'"
    assert result.commit_sha == repo.a and result.branch == "trunk"            # the real default's name, not a guess like "main"


def test_a_tag_is_cloned_detached_with_its_commit(repo):
    _, result = _clone(repo, "v1")
    assert result.commit_sha == repo.a and result.branch is None


@pytest.mark.parametrize("ref", ["featur", "does-not-exist", "main", "release/1.0"])
def test_a_requested_ref_that_cannot_be_cloned_fails_and_is_never_replaced_by_the_default(repo, ref):
    dest = repo.tmp / "dest-missing"
    with pytest.raises(git_api.GitRefUnavailable) as exc:
        git_api._do_clone(repo.url, str(dest), ref)
    assert exc.value.ref == ref and ref in str(exc.value)
    assert not dest.exists(), "a half clone (or the default branch) was left behind"


def test_a_clone_that_fails_for_another_reason_does_not_fall_back_either(repo):
    """An unreachable repository with a requested ref is a GitRefUnavailable too — the old code retried without the ref."""
    with pytest.raises(git_api.GitRefUnavailable):
        git_api._do_clone(f"file://{repo.tmp}/no-such-repo", str(repo.tmp / "d"), "feature")


def test_no_fallback_clone_exists_in_the_source():
    import inspect
    src = inspect.getsource(git_api._do_clone)
    assert src.count('"clone"') == 1, "_do_clone must issue exactly one clone: a second attempt is the silent fallback"


# ── the routes ──────────────────────────────────────────────────────────
def test_an_explicit_missing_branch_fails_the_import_and_records_what_was_asked(api, monkeypatch):
    client, state = api

    def _clone_fails(url, dest, branch, **k):
        raise git_api.GitRefUnavailable(branch, "Remote branch typo not found in upstream origin")

    monkeypatch.setattr(git_api, "_do_clone", _clone_fails)
    r = client.post("/api/v1/git/workspaces/from-git", json={**BODY, "default_branch": "typo"}, headers=_headers())
    assert r.status_code == 201 and r.json()["status"] == "failed"
    body = r.json()
    assert "typo" in body["import_error"] and body["files_imported"] == 0
    assert body["requested_ref"] == "typo" and body["commit_sha"] is None
    b = state.binding
    assert b.requested_ref == "typo" and b.last_synced_sha is None            # nothing was imported, and the row says so


def test_no_branch_given_imports_the_default_and_records_the_real_branch_and_commit(api, monkeypatch):
    client, state = api
    seen = {}

    def _clone_ok(url, dest, branch, **k):
        seen["branch"] = branch
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "a.sql").write_text("select 1")
        return git_api.CloneResult(commit_sha="c" * 40, branch="trunk")

    import app.services.file_service as fs_mod

    class _FS:
        def __init__(self, *a): ...
        async def create(self, ws, req): return SimpleNamespace(is_dirty=True), req.content

    monkeypatch.setattr(git_api, "_do_clone", _clone_ok)
    monkeypatch.setattr(fs_mod, "FileService", _FS)
    r = client.post("/api/v1/git/workspaces/from-git", json=BODY, headers=_headers())
    assert r.status_code == 201 and r.json()["status"] == "ready", r.text
    assert seen["branch"] is None                                               # nothing was asked: no ref is forced
    b = state.binding
    assert (b.requested_ref, b.default_branch, b.current_branch, b.last_synced_sha) == (None, "trunk", "trunk", "c" * 40)
    assert r.json()["branch"] == "trunk" and r.json()["commit_sha"] == "c" * 40


def test_an_explicit_branch_is_passed_through_unchanged_and_recorded(api, monkeypatch):
    client, state = api
    seen = {}

    def _clone_ok(url, dest, branch, **k):
        seen["branch"] = branch
        Path(dest).mkdir(parents=True, exist_ok=True)
        return git_api.CloneResult(commit_sha="d" * 40, branch=branch)

    monkeypatch.setattr(git_api, "_do_clone", _clone_ok)
    import app.services.file_service as fs_mod
    monkeypatch.setattr(fs_mod, "FileService", type("FS", (), {"__init__": lambda self, *a: None}))
    r = client.post("/api/v1/git/workspaces/from-git", json={**BODY, "default_branch": "feature"}, headers=_headers())
    assert r.status_code == 201, r.text
    assert seen["branch"] == "feature"
    assert (state.binding.requested_ref, state.binding.current_branch, state.binding.last_synced_sha) == ("feature", "feature", "d" * 40)


def test_the_flink_import_fails_on_a_missing_ref_with_a_clear_422(monkeypatch):
    import asyncio

    import app.core.git_egress_policy as gep
    from app.api import flink_proxy
    from app.core.errors import WorkspaceError

    async def _token(_u, _p): return None

    def _clone_fails(url, dest, branch, **k):
        raise git_api.GitRefUnavailable(branch, "Remote branch nope not found")

    monkeypatch.setattr(git_api, "load_git_token", _token)
    monkeypatch.setattr(git_api, "_do_clone", _clone_fails)
    monkeypatch.setattr(gep, "assert_binding_url_allowed", lambda *_a: None)
    with pytest.raises(WorkspaceError) as exc:
        asyncio.run(flink_proxy._fetch_sql_from_git(git_url="https://github.com/a/b", git_ref="nope", sql_path=None,
                                                    provider="github", user_id="u"))
    assert exc.value.status_code == 400 and exc.value.detail["details"]["field"] == "git_ref"
    assert "nope" in exc.value.detail["message"] and "not replaced" in exc.value.detail["message"]
