"""W-013: Workspace's two repository importers (the workspace Git import and Flink "create job from Git") apply
ONE set of rules, implemented once in app/git/safe_read.py.

  * one walker (`walk_repo_files`) decides what is a candidate file and what is rejected (symlinks, FIFOs...);
  * one path check (`ensure_safe_path`) for an explicit path a caller supplies;
  * one reader (`read_repo_file`);
  * a guard test: no other module may read files, or walk directories, on its own.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.api import flink_proxy
from app.api import git as git_api
from app.core.config import get_settings
from app.core.errors import WorkspaceError
from app.git.safe_read import UnsafeRepoPath, ensure_safe_path, walk_repo_files

SECRET = "TOP-SECRET-MOUNTED-TOKEN-4f9c"
APP = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture
def secrets_dir(tmp_path):
    d = tmp_path / "mounted-secrets"
    d.mkdir()
    (d / "token.sql").write_text(f"-- {SECRET}\nSELECT 1;")
    return d


@pytest.fixture
def bad_repo(tmp_path, secrets_dir):
    """A repository working tree full of things that must never be read."""
    r = tmp_path / "repo"
    (r / "sub").mkdir(parents=True)
    (r / ".git").mkdir()
    (r / ".git" / "config.sql").write_text("-- git internals")
    (r / "ok.sql").write_text("SELECT 'fine';")
    (r / "sub" / "also_ok.sql").write_text("SELECT 2;")
    os.symlink("/etc/passwd", r / "secret.sql")
    os.symlink(secrets_dir / "token.sql", r / "token_link.sql")
    os.symlink(secrets_dir, r / "creds")                      # a symlinked DIRECTORY
    os.symlink("ok.sql", r / "inrepo_link.sql")
    os.symlink("/does/not/exist", r / "dangling.sql")
    os.mkfifo(r / "pipe.sql")
    return r


# ── the shared walker ────────────────────────────────────────────────────
def test_the_walker_yields_candidates_and_rejections_in_a_stable_order(bad_repo):
    got = {e.rel: e.rejected for e in walk_repo_files(bad_repo)}
    assert got == {
        "ok.sql": None, "sub/also_ok.sql": None,
        "secret.sql": "symbolic link (not followed)", "token_link.sql": "symbolic link (not followed)",
        "creds": "symbolic link (not followed)", "inrepo_link.sql": "symbolic link (not followed)",
        "dangling.sql": "symbolic link (not followed)", "pipe.sql": "not a regular file",
    }


def test_the_walker_never_lists_git_internals_or_descends_into_a_symlinked_directory(bad_repo):
    rels = [e.rel for e in walk_repo_files(bad_repo)]
    assert not any(r.startswith(".git") for r in rels)
    assert not any(r.startswith("creds/") for r in rels)       # the secret directory's contents never appear


def test_the_walker_is_deterministic_and_sorted_within_each_directory(bad_repo):
    first = [e.rel for e in walk_repo_files(bad_repo)]
    assert first == [e.rel for e in walk_repo_files(bad_repo)]
    top = [r for r in first if "/" not in r]
    assert top == sorted(top)


def test_the_walker_on_an_empty_or_missing_tree_yields_nothing(tmp_path):
    (tmp_path / "empty").mkdir()
    assert list(walk_repo_files(tmp_path / "empty")) == []
    assert list(walk_repo_files(tmp_path / "absent")) == []


def test_the_walker_can_start_below_the_root(bad_repo):
    assert [e.rel for e in walk_repo_files(bad_repo / "sub")] == ["also_ok.sql"]


# ── the shared explicit-path check ───────────────────────────────────────
def test_ensure_safe_path_accepts_a_real_file_and_a_real_directory(bad_repo):
    assert ensure_safe_path(bad_repo, bad_repo / "ok.sql") == (bad_repo / "ok.sql").resolve()
    assert ensure_safe_path(bad_repo, bad_repo / "sub") == (bad_repo / "sub").resolve()


@pytest.mark.parametrize("rel,reason", [
    ("secret.sql", "symbolic link"), ("creds", "symbolic link"), ("inrepo_link.sql", "symbolic link"),
    ("dangling.sql", "symbolic link"), ("creds/token.sql", "outside"), ("../escape.sql", "outside"),
])
def test_ensure_safe_path_rejects_links_and_escapes(bad_repo, rel, reason):
    with pytest.raises(UnsafeRepoPath, match=reason):
        ensure_safe_path(bad_repo, bad_repo / rel)


# ── both importers reach the same verdict from the same walker ───────────
@pytest.fixture
def importer(monkeypatch):
    created: dict[str, str] = {}

    class _FS:
        def __init__(self, *a): ...
        async def find_by_path(self, ws, rel, branch):
            return None
        async def create(self, ws, req):
            created[req.path] = req.content
            return SimpleNamespace(is_dirty=True), req.content

    class _DB:
        async def flush(self): ...

    monkeypatch.setattr("app.services.file_service.FileService", _FS)
    monkeypatch.setattr("app.services.git_lock.check_lock_held", lambda: None)

    async def run(repo_dir):
        out = await git_api._import_tree(_DB(), SimpleNamespace(id="w"), repo_dir=repo_dir, branch="main",
                                         org_id="o", token=None, upsert=False)
        return out, created
    return run


async def test_the_workspace_import_rejects_exactly_what_the_walker_rejects(bad_repo, importer):
    outcome, created = await importer(bad_repo)
    walker_rejected = {e.rel: e.rejected for e in walk_repo_files(bad_repo) if e.rejected}
    assert {s["path"]: s["reason"] for s in outcome.skipped_files} == walker_rejected
    assert sorted(created) == ["ok.sql", "sub/also_ok.sql"]
    assert SECRET not in "".join(created.values())


@pytest.mark.parametrize("path", ["secret.sql", "token_link.sql", "inrepo_link.sql", "dangling.sql", "pipe.sql", "creds"])
def test_flink_refuses_what_the_walker_rejects(bad_repo, path):
    """Same repo, same verdict: every entry the walker rejects also stops a Flink job that would read it."""
    assert any(e.rel == path and e.rejected for e in walk_repo_files(bad_repo))
    with pytest.raises(WorkspaceError) as e:
        flink_proxy._collect_sql(bad_repo, path)
    assert e.value.status_code == 400
    assert SECRET not in str(e.value.detail) and "/etc/passwd" not in str(e.value.detail)


def test_flink_directory_assembly_refuses_a_rejected_sql_file_and_names_it(bad_repo):
    with pytest.raises(WorkspaceError) as e:
        flink_proxy._collect_sql(bad_repo, None)
    msg = e.value.detail["message"]
    assert "is not read" in msg and ("secret.sql" in msg or "token_link.sql" in msg or "pipe.sql" in msg or "dangling.sql" in msg or "inrepo_link.sql" in msg)
    assert SECRET not in msg


def test_flink_assembles_the_clean_part_of_a_repo_when_nothing_is_rejected(tmp_path):
    r = tmp_path / "r"
    (r / "d").mkdir(parents=True)
    (r / "d" / "a.sql").write_text("SELECT 1")
    (r / "b.sql").write_text("SELECT 2")
    (r / ".git").mkdir()
    (r / ".git" / "x.sql").write_text("SELECT 'never'")
    sql, resolved = flink_proxy._collect_sql(r, None)
    assert resolved == "." and "SELECT 1;" in sql and "SELECT 2;" in sql and "never" not in sql


# ── the real Flink routes, end to end ────────────────────────────────────
@pytest.fixture
def flink_routes(monkeypatch, tmp_path, bad_repo):
    """POST /flink/jobs/from-git and /sync with the clone replaced by a copy of `bad_repo`; a fake runner."""
    s = get_settings()
    monkeypatch.setattr(s, "flinksql_delegation_jwt_secret", "flink-delegation-secret")
    monkeypatch.setattr(s, "flinksql_runner_url", "http://flink.test")
    state = SimpleNamespace(runner_calls=[], repo=bad_repo)

    def handler(req: httpx.Request) -> httpx.Response:
        state.runner_calls.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"description": "x\n[flink-git] url=https://github.com/a/b.git ref=main path=secret.sql sha=abc"})
        return httpx.Response(201, json={"id": "j1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def fake_token(*_a):
        return None

    def fake_clone(git_url, dest, git_ref, provider=None, token=None):
        shutil.copytree(state.repo, dest, symlinks=True)
        return git_api.CloneResult(commit_sha="a" * 40, branch=git_ref or "main")

    monkeypatch.setattr(git_api, "load_git_token", fake_token)
    monkeypatch.setattr(git_api, "_do_clone", fake_clone)
    return state


def _post(client, gateway_headers, path, body):
    return client.post(path, json=body, headers=gateway_headers(user_name="alice", roles=["data_engineer"]))


def test_from_git_with_a_symlinked_sql_path_is_refused_and_nothing_reaches_the_runner(client, gateway_headers, flink_routes):
    r = _post(client, gateway_headers, "/api/v1/flink/jobs/from-git",
              {"git_url": "https://github.com/a/b.git", "sql_path": "secret.sql"})
    assert r.status_code == 400, r.text
    assert flink_routes.runner_calls == []
    assert "root:" not in r.text and SECRET not in r.text


def test_from_git_with_no_path_in_a_repo_with_symlinked_sql_files_is_refused(client, gateway_headers, flink_routes):
    r = _post(client, gateway_headers, "/api/v1/flink/jobs/from-git", {"git_url": "https://github.com/a/b.git"})
    assert r.status_code == 400 and flink_routes.runner_calls == []


def test_from_git_with_a_clean_repo_creates_the_job_with_its_sql(client, gateway_headers, flink_routes, tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "job.sql").write_text("INSERT INTO t SELECT 1")
    flink_routes.repo = clean
    r = _post(client, gateway_headers, "/api/v1/flink/jobs/from-git", {"git_url": "https://github.com/a/b.git"})
    assert r.status_code == 201, r.text
    (call,) = flink_routes.runner_calls
    assert b"INSERT INTO t SELECT 1" in call.content


def test_sync_follows_the_same_rules_even_when_the_stored_provenance_path_is_a_symlink(client, gateway_headers, flink_routes):
    """The path comes from the job's description, which a user can edit: sync must not trust it."""
    r = _post(client, gateway_headers, "/api/v1/flink/jobs/3f1c2b7e-0000-4000-8000-000000000001/sync", {})
    assert r.status_code == 400, r.text
    assert [c.method for c in flink_routes.runner_calls] == ["GET"]           # the PATCH with the SQL never happened
    assert "root:" not in r.text and SECRET not in r.text


# ── guard: nobody reads clone files on their own ─────────────────────────
RAW_READS = re.compile(r"\bos\.walk\(|\.read_bytes\(|\.read_text\(|\.rglob\(|\.iterdir\(|\.glob\(|os\.scandir\(|os\.listdir\(|\.open\(|(?<![\w.])open\(")
# Modules allowed to touch files directly, and why. A repository clone is never read from any of them.
ALLOWED = {
    "app/git/safe_read.py": "the one place that reads clone files",
    "app/storage/secrets.py": "its own secret store file, not repository content",
    "app/worker.py": "writes its own heartbeat file, not repository content",
    "app/core/rbac.py": "reads the published permissions.json shipped with the image, not repository content",
}


def test_no_module_reads_or_walks_files_outside_the_shared_helper():
    offenders = []
    for py in sorted(APP.rglob("*.py")):
        rel = str(py.relative_to(APP.parent))
        if rel in ALLOWED:
            continue
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if RAW_READS.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        "read repository files through app/git/safe_read.py (read_repo_file / walk_repo_files / ensure_safe_path), "
        "or add the module to ALLOWED with a reason:\n" + "\n".join(offenders)
    )


def test_both_importers_use_the_shared_helper():
    for module in ("app/api/git.py", "app/api/flink_proxy.py"):
        text = (APP.parent / module).read_text()
        assert "app.git.safe_read" in text and "walk_repo_files" in text, module
