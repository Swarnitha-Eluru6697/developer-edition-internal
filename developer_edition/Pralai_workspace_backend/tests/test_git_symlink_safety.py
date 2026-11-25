"""W-012: importing a repository must never read through a symbolic link.

A malicious repo can commit `secret.sql -> /etc/passwd`, or a symlink to a mounted secret directory. Following
it would copy a file from the Workspace pod into the user's workspace (or into a Flink job's SQL). Every
file read from a clone now uses lstat, rejects symlinks and non-regular files, resolves the path and
requires it to stay inside the clone, and opens with O_NOFOLLOW. The target must not even be OPENED.
"""
from __future__ import annotations

import builtins
import io
import os
import shutil
import signal
import subprocess
from types import SimpleNamespace

import pytest

from app.api import flink_proxy
from app.api import git as git_api
from app.core.errors import WorkspaceError
from app.git.safe_read import FileTooLarge, UnsafeRepoPath, ensure_inside, read_repo_file

SECRET = "TOP-SECRET-MOUNTED-TOKEN-4f9c"


@pytest.fixture
def secrets_dir(tmp_path):
    d = tmp_path / "mounted-secrets"
    d.mkdir()
    (d / "token.txt").write_text(SECRET)
    (d / "db.sql").write_text(f"-- {SECRET}\nSELECT 1;")
    return d


@pytest.fixture
def opened(monkeypatch):
    """Record every path opened (or stat-read) through the usual file APIs while a test runs."""
    seen: list[str] = []
    real_os_open, real_open, real_io_open = os.open, builtins.open, io.open

    def note(p):
        try:
            seen.append(os.path.realpath(p))
        except (TypeError, ValueError):
            pass

    monkeypatch.setattr(os, "open", lambda p, *a, **k: (note(p), real_os_open(p, *a, **k))[1])
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (note(p), real_open(p, *a, **k))[1])
    monkeypatch.setattr(io, "open", lambda p, *a, **k: (note(p), real_io_open(p, *a, **k))[1])
    return seen


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"})


@pytest.fixture
def malicious_clone(tmp_path, secrets_dir):
    """A real clone of a real git repo whose COMMITTED tree contains the attack symlinks."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    (src / "ok.sql").write_text("SELECT 'fine';")
    (src / "sub").mkdir()
    (src / "sub" / "also_ok.sql").write_text("SELECT 2;")
    os.symlink("/etc/passwd", src / "secret.sql")                                   # the acceptance case
    os.symlink(str(secrets_dir), src / "creds")                                     # symlink INTO a secret directory
    os.symlink(str(secrets_dir / "token.txt"), src / "token_link.sql")              # file inside it
    os.symlink("ok.sql", src / "inrepo_link.sql")                                   # harmless target: still not followed
    os.symlink("/does/not/exist", src / "dangling.sql")
    os.symlink("loop", src / "loop")                                                # a loop
    os.symlink("../../../../etc/passwd", src / "sub" / "relative_escape.sql")
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "attack", cwd=src)
    dest = tmp_path / "clone"
    _git("clone", "-q", str(src), str(dest), cwd=tmp_path)
    assert os.path.islink(dest / "secret.sql"), "the fixture must produce a real symlink in the clone"
    return dest


# ── the acceptance test: import a repo with the attack symlinks ──────────
@pytest.fixture
def importer(monkeypatch):
    created: dict[str, str] = {}

    class _FS:
        def __init__(self, *a): ...
        async def find_by_path(self, ws, rel, branch):
            return None
        async def update(self, ws, fid, req): ...
        async def create(self, ws, req):
            created[req.path] = req.content
            return SimpleNamespace(is_dirty=True), req.content

    class _DB:
        async def flush(self): ...

    monkeypatch.setattr("app.services.file_service.FileService", _FS)
    monkeypatch.setattr("app.services.git_lock.check_lock_held", lambda: None)

    async def run(repo_dir, upsert=False):
        out = await git_api._import_tree(_DB(), SimpleNamespace(id="w"), repo_dir=repo_dir, branch="main",
                                         org_id="o", token=None, upsert=upsert)
        return out, created
    return run


async def test_import_rejects_every_symlink_without_reading_its_target(malicious_clone, secrets_dir, importer, opened):
    outcome, created = await importer(malicious_clone)

    assert sorted(created) == ["ok.sql", "sub/also_ok.sql"], created           # only the real files came in
    blob = "\n".join(created.values())
    assert SECRET not in blob and "root:" not in blob                          # nothing from outside the repo
    skipped = {s["path"]: s["reason"] for s in outcome.skipped_files}
    for link in ("secret.sql", "token_link.sql", "inrepo_link.sql", "dangling.sql", "sub/relative_escape.sql", "creds", "loop"):
        assert "symbolic link" in skipped.get(link, ""), (link, skipped)       # each one is reported, none silently dropped
    assert not outcome.failed_files and outcome.files_imported == 2

    leaked = [p for p in opened if p.startswith(str(secrets_dir.resolve())) or p == os.path.realpath("/etc/passwd")]
    assert leaked == [], f"the import opened a symlink target: {leaked}"


async def test_the_pull_path_is_covered_too(malicious_clone, importer):
    _, created = await importer(malicious_clone, upsert=True)                  # pull re-imports with upsert=True
    assert "secret.sql" not in created and SECRET not in "".join(created.values())


async def test_a_clean_repo_imports_exactly_as_before(tmp_path, importer):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "a.sql").write_text("SELECT 1;")
    (tmp_path / "r" / "d").mkdir()
    (tmp_path / "r" / "d" / "b.py").write_text("print(1)")
    (tmp_path / "r" / "bin.dat").write_bytes(b"\xff\xfe\x00")
    outcome, created = await importer(tmp_path / "r")
    assert created == {"a.sql": "SELECT 1;", "d/b.py": "print(1)"}
    assert [s["reason"] for s in outcome.skipped_files] == ["binary"]


async def test_the_oversize_limit_still_applies(tmp_path, importer, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "max_file_size_bytes", 10)
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "big.sql").write_text("SELECT 1;" * 10)
    (tmp_path / "r" / "small.sql").write_text("x")
    outcome, created = await importer(tmp_path / "r")
    assert list(created) == ["small.sql"]
    assert outcome.skipped_files == [{"path": "big.sql", "reason": "over size limit"}]


# ── the helper ───────────────────────────────────────────────────────────
def test_a_regular_file_is_read(tmp_path):
    (tmp_path / "f.sql").write_text("SELECT 1;")
    assert read_repo_file(tmp_path, tmp_path / "f.sql", max_bytes=100) == b"SELECT 1;"


def test_a_symlink_to_a_file_outside_is_rejected_and_never_opened(tmp_path, secrets_dir, opened):
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink(secrets_dir / "token.txt", root / "s.sql")
    with pytest.raises(UnsafeRepoPath, match="symbolic link"):
        read_repo_file(root, root / "s.sql", max_bytes=100)
    assert not [p for p in opened if p.startswith(str(secrets_dir.resolve()))]


def test_a_symlink_to_etc_passwd_is_rejected(tmp_path, opened):
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink("/etc/passwd", root / "secret.sql")
    with pytest.raises(UnsafeRepoPath):
        read_repo_file(root, root / "secret.sql", max_bytes=10_000)
    assert os.path.realpath("/etc/passwd") not in opened


def test_a_file_reached_through_a_symlinked_directory_is_rejected(tmp_path, secrets_dir):
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink(secrets_dir, root / "creds")
    with pytest.raises(UnsafeRepoPath, match="outside"):
        read_repo_file(root, root / "creds" / "db.sql", max_bytes=1000)        # not a symlink itself, its PARENT is


def test_a_symlinked_directory_that_stays_inside_the_repo_is_allowed_for_the_parent_only(tmp_path):
    root = tmp_path / "repo"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.sql").write_text("SELECT 1;")
    os.symlink(root / "real", root / "alias")
    assert read_repo_file(root, root / "alias" / "f.sql", max_bytes=100) == b"SELECT 1;"   # resolves inside the clone


def test_a_dotdot_path_that_escapes_the_clone_is_rejected(tmp_path, secrets_dir):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(UnsafeRepoPath, match="outside"):
        read_repo_file(root, root / ".." / "mounted-secrets" / "token.txt", max_bytes=100)


def test_a_fifo_is_rejected_without_blocking(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    os.mkfifo(root / "pipe.sql")
    def _blocked(*_a):
        raise AssertionError("blocked reading a FIFO")                         # a clean failure, not a killed test run
    previous = signal.signal(signal.SIGALRM, _blocked)
    signal.alarm(5)                                                           # a read of a FIFO with no writer would hang
    try:
        with pytest.raises(UnsafeRepoPath, match="regular file"):
            read_repo_file(root, root / "pipe.sql", max_bytes=100)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_directory_is_not_a_file(tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(UnsafeRepoPath):
        read_repo_file(tmp_path, tmp_path / "d", max_bytes=100)


def test_a_missing_file_is_an_ordinary_oserror(tmp_path):
    with pytest.raises(OSError):
        read_repo_file(tmp_path, tmp_path / "nope.sql", max_bytes=100)


def test_the_size_limit_is_enforced_before_reading_everything(tmp_path):
    (tmp_path / "big").write_bytes(b"x" * 1000)
    with pytest.raises(FileTooLarge):
        read_repo_file(tmp_path, tmp_path / "big", max_bytes=999)
    assert read_repo_file(tmp_path, tmp_path / "big", max_bytes=1000) == b"x" * 1000


def test_a_swap_to_a_symlink_after_the_check_is_stopped_by_O_NOFOLLOW(tmp_path, monkeypatch):
    """Defence in depth: even if lstat were fooled (a race), the open itself refuses to follow a symlink."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "decoy.sql").write_text("decoy")
    os.symlink(root / "decoy.sql", root / "swapped.sql")                       # inside the clone, so realpath() passes
    from app.git import safe_read
    real_lstat = os.lstat
    monkeypatch.setattr(safe_read.os, "lstat", lambda p, *a, **k: real_lstat(root / "decoy.sql") if str(p).endswith("swapped.sql") else real_lstat(p, *a, **k))
    with pytest.raises(UnsafeRepoPath):
        read_repo_file(root, root / "swapped.sql", max_bytes=100)


def test_ensure_inside_accepts_the_root_and_children_and_rejects_siblings(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo-evil").mkdir()                                           # shares the string prefix "repo"
    assert ensure_inside(tmp_path / "repo", tmp_path / "repo") == (tmp_path / "repo").resolve()
    ensure_inside(tmp_path / "repo", tmp_path / "repo" / "a" / "b.sql")
    with pytest.raises(UnsafeRepoPath):
        ensure_inside(tmp_path / "repo", tmp_path / "repo-evil" / "x.sql")


# ── Flink "create job from Git" reads the clone too ──────────────────────
@pytest.fixture
def flink_repo(tmp_path, secrets_dir):
    r = tmp_path / "flink"
    r.mkdir()
    (r / "ok.sql").write_text("INSERT INTO t SELECT 1")
    os.symlink("/etc/passwd", r / "passwd.sql")
    os.symlink(secrets_dir / "db.sql", r / "job.sql")                          # a conventional entrypoint name
    os.symlink(secrets_dir, r / "evil")
    (r / "plain").mkdir()
    (r / "plain" / "a.sql").write_text("SELECT 1")
    os.symlink("/etc/passwd", r / "plain" / "leak.sql")
    return r


@pytest.mark.parametrize("sql_path", ["passwd.sql", "job.sql", "evil", "evil/db.sql", "plain/leak.sql"],
                         ids=lambda p: p.replace("/", "_"))
def test_flink_explicit_sql_path_through_a_symlink_is_rejected(flink_repo, sql_path, opened, secrets_dir):
    with pytest.raises(WorkspaceError) as e:
        flink_proxy._collect_sql(flink_repo, sql_path)
    assert e.value.status_code == 400 and "symbolic link" in e.value.detail["message"].lower() or "outside" in e.value.detail["message"].lower()
    assert SECRET not in str(e.value.detail)
    assert not [p for p in opened if p.startswith(str(secrets_dir.resolve())) or p == os.path.realpath("/etc/passwd")]


def test_flink_default_entrypoint_that_is_a_symlink_is_not_followed(flink_repo, opened, secrets_dir):
    """No sql_path: `job.sql` is a conventional entrypoint and is a symlink into the secret directory."""
    with pytest.raises(WorkspaceError):
        flink_proxy._collect_sql(flink_repo, None)
    assert not [p for p in opened if p.startswith(str(secrets_dir.resolve()))]


def test_flink_concatenation_rejects_a_symlinked_sql_file(flink_repo, secrets_dir, opened):
    os.unlink(flink_repo / "job.sql")                                          # leave only the directory scan
    shutil.rmtree(flink_repo / "plain")
    os.unlink(flink_repo / "evil")
    with pytest.raises(WorkspaceError) as e:
        flink_proxy._collect_sql(flink_repo, None)
    assert "symbolic link" in e.value.detail["message"].lower()
    assert os.path.realpath("/etc/passwd") not in opened


def test_flink_a_normal_repo_still_assembles(tmp_path):
    r = tmp_path / "r"
    (r / "d").mkdir(parents=True)
    (r / "d" / "a.sql").write_text("SELECT 1")
    (r / "b.sql").write_text("SELECT 2")
    sql, resolved = flink_proxy._collect_sql(r, None)
    assert resolved == "." and "SELECT 1;" in sql and "SELECT 2;" in sql
    sql, resolved = flink_proxy._collect_sql(r, "b.sql")
    assert (sql, resolved) == ("SELECT 2", "b.sql")


def test_flink_a_fifo_named_like_sql_is_rejected_not_read(tmp_path):
    """A plain read of a FIFO with no writer blocks forever; the reader must refuse non-regular files."""
    r = tmp_path / "r"
    r.mkdir()
    (r / "a.sql").write_text("SELECT 1")
    os.mkfifo(r / "pipe.sql")

    def _blocked(*_a):
        raise AssertionError("blocked reading a FIFO")
    previous = signal.signal(signal.SIGALRM, _blocked)
    signal.alarm(5)
    try:
        with pytest.raises(WorkspaceError) as e:
            flink_proxy._collect_sql(r, None)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert "regular file" in e.value.detail["message"]


def test_flink_a_symlink_to_an_in_repo_file_is_still_not_followed(tmp_path):
    """The rule is 'reject symbolic links', not only 'reject ones that leave the repo'."""
    r = tmp_path / "r"
    r.mkdir()
    (r / "real.sql").write_text("SELECT 1")
    os.symlink("real.sql", r / "link.sql")
    with pytest.raises(WorkspaceError) as e:
        flink_proxy._collect_sql(r, "link.sql")
    assert "symbolic link" in e.value.detail["message"].lower()


def test_flink_an_explicit_path_that_is_a_symlinked_directory_is_rejected_even_inside_the_repo(tmp_path):
    r = tmp_path / "r"
    (r / "real").mkdir(parents=True)
    (r / "real" / "a.sql").write_text("SELECT 1")
    os.symlink("real", r / "alias")
    with pytest.raises(WorkspaceError) as e:
        flink_proxy._collect_sql(r, "alias")
    assert "symbolic link" in e.value.detail["message"].lower()


def test_the_read_itself_is_bounded_so_a_huge_file_is_never_loaded(tmp_path, monkeypatch):
    from app.git import safe_read
    (tmp_path / "f").write_bytes(b"x" * 50)
    asked = []

    class _F:
        def read(self, n=-1):
            asked.append(n)
            return b"x" * (50 if n < 0 else min(n, 50))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(safe_read.os, "fdopen", lambda fd, mode: (os.close(fd), _F())[1])
    assert read_repo_file(tmp_path, tmp_path / "f", max_bytes=100) == b"x" * 50
    assert asked == [101], asked                                             # max_bytes + 1, never "everything"
