"""WS-GIT-009: Git credentials never touch disk, never show up in errors, and
git never follows an HTTP redirect.

Uses real git against a local smart-HTTP server (`git http-backend` behind a
tiny CGI shim) so the assertions are about what git actually does, not about
what we told it to do.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.git.git_local import LocalRepo, git_auth_env, scrub, split_userinfo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TOKEN = "ghp_SuperSecretToken_" + uuid.uuid4().hex
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
GIT_ID = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _serve(self):
        srv = self.server
        srv.requests.append((self.command, self.path, self.headers.get("Authorization")))
        if srv.redirect_to:
            self.send_response(302)
            self.send_header("Location", srv.redirect_to + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if srv.require_auth and self.headers.get("Authorization") != f"Basic {BASIC}":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, qs = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(srv.root), "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path, "QUERY_STRING": qs, "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "tester", "REMOTE_ADDR": "127.0.0.1",
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
        sep = b"\r\n\r\n" if b"\r\n\r\n" in out else b"\n\n"
        head, _, rest = out.partition(sep)
        status, headers = 200, []
        for line in head.splitlines():
            k, _, v = line.decode().partition(":")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                headers.append((k, v.strip()))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(rest)))
        self.end_headers()
        self.wfile.write(rest)

    do_GET = do_POST = _serve


def _server(root: Path, *, require_auth=True, redirect_to=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.root, srv.require_auth, srv.redirect_to, srv.requests = root, require_auth, redirect_to, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **GIT_ID})


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    bare = root / "repo.git"
    _git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git("init", "-b", "main", str(seed))
    (seed / "a.sql").write_text("select 1;\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("push", str(bare), "main", cwd=seed)
    srv, base = _server(root)
    yield SimpleNamespace(srv=srv, url=f"{base}/repo.git", root=root, tmp=tmp_path)
    srv.shutdown()


def _assert_no_secret_on_disk(directory: Path):
    for p in directory.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert TOKEN.encode() not in data, f"token found in {p}"
            assert BASIC.encode() not in data, f"encoded credential found in {p}"


def _authed_requests(srv):
    return [r for r in srv.requests if r[2] == f"Basic {BASIC}"]


def test_clone_commit_push_pull_authenticate_without_writing_the_token(remote, monkeypatch):
    for k, v in GIT_ID.items():
        monkeypatch.setenv(k, v)
    wc = remote.tmp / "wc"

    async def run():
        local = LocalRepo(wc, remote.url, provider="github", token=TOKEN)
        await local.clone_or_open(branch="main")
        _assert_no_secret_on_disk(wc)
        await local.commit_files({"b.sql": b"select 2;\n"}, "add b", "t", "t@example.com")
        await local.push("main")
        _assert_no_secret_on_disk(wc)

        # a second working copy picks the change up through pull
        other = remote.tmp / "wc2"
        o = LocalRepo(other, remote.url, provider="github", token=TOKEN)
        await o.clone_or_open(branch="main")
        await local.commit_files({"c.sql": b"select 3;\n"}, "add c", "t", "t@example.com")
        await local.push("main")
        sha = await o.pull("main")
        assert (other / "c.sql").exists()
        assert sha == await local.head_sha()
        _assert_no_secret_on_disk(other)
        return sha

    asyncio.run(run())
    # every git request carried the env-supplied header; it was sent, not stored
    assert _authed_requests(remote.srv)
    assert any(m == "POST" and "receive-pack" in p for m, p, _ in _authed_requests(remote.srv))
    assert "x-access-token" not in (wc / ".git" / "config").read_text()


def test_missing_token_fails_fast_and_errors_carry_no_secret(remote):
    async def run():
        local = LocalRepo(remote.tmp / "nope", remote.url, provider="github", token="wrong-" + TOKEN)
        with pytest.raises(Exception) as ei:
            await local.clone_or_open(branch="main")
        return ei.value

    err = asyncio.run(run())  # GIT_TERMINAL_PROMPT=0 → no hang waiting for a password
    assert TOKEN not in scrub(err, "wrong-" + TOKEN)


def test_push_failure_message_is_scrubbed(remote, monkeypatch):
    for k, v in GIT_ID.items():
        monkeypatch.setenv(k, v)
    wc = remote.tmp / "wc"

    async def run():
        local = LocalRepo(wc, remote.url, provider="github", token=TOKEN)
        await local.clone_or_open(branch="main")
        await local.commit_files({"x.sql": b"1"}, "x", "t", "t@example.com")
        remote.srv.require_auth = True
        local._env = git_auth_env(remote.url, "github", TOKEN + "-revoked")
        local._token = TOKEN + "-revoked"
        with pytest.raises(RuntimeError) as ei:
            await local.push("main")
        return str(ei.value)

    msg = asyncio.run(run())
    assert TOKEN not in msg


def test_redirects_are_not_followed(tmp_path):
    internal_root = tmp_path / "internal"
    internal_root.mkdir()
    internal, internal_base = _server(internal_root, require_auth=False)
    redirector, base = _server(tmp_path, require_auth=False, redirect_to=internal_base)
    try:
        async def run():
            local = LocalRepo(tmp_path / "wc", f"{base}/repo.git", provider="github", token=TOKEN)
            with pytest.raises(Exception):
                await local.clone_or_open(branch="main")
        asyncio.run(run())
        assert redirector.requests            # git did talk to the allowed host
        assert internal.requests == []        # ...but never followed it elsewhere
    finally:
        internal.shutdown()
        redirector.shutdown()


def test_do_clone_authenticates_via_env_and_refuses_credential_urls(remote):
    from app.api import git as git_api

    assert not hasattr(git_api, "_authed_clone_url")  # nobody can build token URLs any more
    dest = remote.tmp / "clone"
    git_api._do_clone(remote.url, str(dest), "main", provider="github", token=TOKEN)
    assert (dest / "a.sql").exists()
    _assert_no_secret_on_disk(dest)
    assert _authed_requests(remote.srv)

    authed = remote.url.replace("http://", f"http://x-access-token:{TOKEN}@")
    with pytest.raises(ValueError):
        git_api._do_clone(authed, str(remote.tmp / "legacy"), "main")


def test_flink_git_fetch_uses_env_credentials(remote, monkeypatch):
    """flink_proxy's job-registration clone goes through the same mechanism:
    the stored token authenticates, and is in no URL / config / error."""
    from app.api import flink_proxy
    from app.api import git as git_api

    async def _token(_u, _p):
        return TOKEN

    monkeypatch.setattr(git_api, "load_git_token", _token)
    # The local test server is http://127.0.0.1 — egress policy (tested in
    # tests/test_flink_git_egress.py) would rightly refuse it.
    import app.core.git_egress_policy as gep
    monkeypatch.setattr(gep, "assert_binding_url_allowed", lambda *_a: None)
    sql, path, sha, branch = asyncio.run(flink_proxy._fetch_sql_from_git(
        git_url=remote.url, git_ref="main", sql_path=None, provider="github", user_id="u",
    ))
    assert "select 1" in sql and len(sha) == 40 and branch == "main"
    assert _authed_requests(remote.srv)


def test_flink_git_fetch_errors_are_scrubbed(remote, monkeypatch):
    from app.api import flink_proxy
    from app.api import git as git_api
    from app.core.errors import WorkspaceError

    async def _token(_u, _p):
        return "bad-" + TOKEN

    monkeypatch.setattr(git_api, "load_git_token", _token)
    # The local test server is http://127.0.0.1 — egress policy (tested in
    # tests/test_flink_git_egress.py) would rightly refuse it.
    import app.core.git_egress_policy as gep
    monkeypatch.setattr(gep, "assert_binding_url_allowed", lambda *_a: None)
    with pytest.raises(WorkspaceError) as ei:
        asyncio.run(flink_proxy._fetch_sql_from_git(
            git_url=remote.url, git_ref="main", sql_path=None, provider="github", user_id="u",
        ))
    assert TOKEN not in str(ei.value.detail)


def test_existing_working_copy_has_leftover_token_scrubbed_on_open(remote):
    """A working copy written by the old code (or left behind by a crash
    mid-push under it) gets its stored remote cleaned the next time it's
    used."""
    wc = remote.tmp / "old"
    _git("clone", str(remote.root / "repo.git"), str(wc))
    _git("remote", "set-url", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r.git", cwd=wc)
    assert TOKEN in (wc / ".git" / "config").read_text()

    asyncio.run(LocalRepo(wc, remote.url, provider="github", token=TOKEN).clone_or_open(branch="main"))
    cfg = (wc / ".git" / "config").read_text()
    assert TOKEN not in cfg
    assert remote.url in cfg


def test_set_remote_url_refuses_credentials(remote):
    wc = remote.tmp / "wc"
    local = LocalRepo(wc, remote.url, provider="github", token=TOKEN)
    asyncio.run(local.clone_or_open(branch="main"))
    with pytest.raises(ValueError):
        asyncio.run(local.set_remote_url(f"https://x-access-token:{TOKEN}@github.com/o/r.git"))
    _assert_no_secret_on_disk(wc)


def test_local_repo_strips_userinfo_from_constructor_url():
    lr = LocalRepo(Path("/nonexistent"), f"https://oauth2:{TOKEN}@gitlab.com/g/p.git", provider="gitlab")
    assert lr._url == "https://gitlab.com/g/p.git"
    keys = {lr.env[f"GIT_CONFIG_KEY_{i}"]: lr.env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(lr.env["GIT_CONFIG_COUNT"]))}
    assert keys["http.followRedirects"] == "false"
    assert keys["http.https://gitlab.com/.extraHeader"] == (
        "Authorization: Basic " + base64.b64encode(f"oauth2:{TOKEN}".encode()).decode()
    )


def test_git_auth_env_without_token_still_disables_redirects_and_prompts():
    env = git_auth_env("https://github.com/o/r.git")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    vals = [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
    assert not any("Authorization" in v for v in vals)


@pytest.mark.parametrize("text", [
    f"fatal: could not read from https://x-access-token:{TOKEN}@github.com/o/r",
    f"token={TOKEN}",
    f"Authorization: Basic {BASIC}",
    f"url-quoted: {TOKEN.replace('_', '%5F')}",
])
def test_scrub_removes_every_form_of_the_secret(text):
    out = scrub(text, TOKEN)
    assert TOKEN not in out and BASIC not in out
    assert "x-access-token:" not in out


def test_split_userinfo():
    assert split_userinfo("https://u:p%40ss@h.com:8443/x.git") == ("https://h.com:8443/x.git", "u", "p@ss")
    assert split_userinfo("https://h.com/x.git") == ("https://h.com/x.git", None, None)


# ── Route-level: push through the real endpoint ─────────────────────────

def test_push_route_uses_env_credentials_and_leaves_nothing_on_disk(remote, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import git as git_api
    from app.core.deps import get_db
    from app.main import app
    from app.models import GitBinding
    from app.services.audit_service import AuditService
    from app.services.workspace_service import WorkspaceService

    from conftest import _headers

    for k, v in GIT_ID.items():
        monkeypatch.setenv(k, v)
    ws_id = uuid.uuid4()
    git_root = remote.tmp / "gitroot"
    monkeypatch.setenv("WORKSPACE_GIT_ROOT", str(git_root))
    wc = git_root / str(ws_id)
    _git("clone", "-q", str(remote.root / "repo.git"), str(wc))
    _git("remote", "set-url", "origin", remote.url, cwd=wc)
    (wc / "new.sql").write_text("select 9;\n")
    _git("add", "-A", cwd=wc)
    _git("commit", "-m", "local", cwd=wc)

    binding = GitBinding(provider="github", repo_url=remote.url, current_branch="main",
                         default_branch="main", status="ready")

    class _DB:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: binding)
        async def flush(self): ...
        async def commit(self): ...
        async def rollback(self): ...
        def add(self, _r): ...

    async def _db():
        yield _DB()

    class _NoLock:
        def __init__(self, _w): ...
        async def __aenter__(self): ...
        async def __aexit__(self, *e): return False

    async def _noop(*a, **k): ...

    async def _token(_u, _p):
        return TOKEN

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _get(self, user, wid):
        return SimpleNamespace(id=ws_id, name="w")

    # http://127.0.0.1 isn't an allowed host; egress has its own tests.
    monkeypatch.setattr(git_api, "_assert_binding_egress", lambda b: None)
    monkeypatch.setattr(git_api, "workspace_git_lock", _NoLock)
    monkeypatch.setattr(git_api, "load_git_token", _token)
    monkeypatch.setattr(AuditService, "record", _noop)
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(WorkspaceService, "get", _get)
    app.dependency_overrides[get_db] = _db
    try:
        r = TestClient(app).post(f"/api/v1/git/workspaces/{ws_id}/push",
                                 headers=_headers(roles=["data_engineer"]))
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 202, r.text
    assert any("receive-pack" in p for _, p, _a in _authed_requests(remote.srv))
    _assert_no_secret_on_disk(wc)
    log = subprocess.run(["git", "--git-dir", str(remote.root / "repo.git"), "log", "--oneline", "main"],
                         capture_output=True, text=True).stdout
    assert "local" in log


# ── A lost lock kills the git process in flight ─────────────────────────

def test_lost_lock_kills_running_git_and_blocks_further_mutation(tmp_path):
    """A git process that is mid-flight when the lock is lost is killed (not
    left running alongside the new holder), and no later git command starts."""
    import time

    from app.git.git_local import run_git
    from app.services.git_lock import GitLockLostError, LockGuard

    guard = LockGuard("k")
    # A git subprocess that blocks until killed: fetch from a server that
    # never answers.
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    _git("init", "-q", str(tmp_path / "w"))
    threading.Timer(0.5, guard.lost.set).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(GitLockLostError):
            run_git(["fetch", f"http://127.0.0.1:{port}/r.git"], cwd=tmp_path / "w", guard=guard)
    finally:
        sock.close()
    assert time.monotonic() - t0 < 5   # killed promptly, didn't wait for git to finish
    with pytest.raises(GitLockLostError):
        run_git(["status"], cwd=tmp_path / "w", guard=guard)
