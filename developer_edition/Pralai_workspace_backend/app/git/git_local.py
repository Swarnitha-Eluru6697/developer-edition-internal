"""Local Git working-copy operations.

For commit/push we materialize a local working copy in the data plane
(a tmpfs or PVC). Workflow:
  1. Clone the bound repo on first use; checkout branch.
  2. Pull updates from object storage (the user's workspace files) into the working copy.
  3. Commit + push using credentials from the secrets vault.

The working copy is per-(workspace, branch) and ephemeral: it can be
rebuilt from object storage + remote at any time.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse, urlunparse
from pathlib import Path

import git as gitpy
import structlog

log = structlog.get_logger()


# ── Credentials without touching disk (WS-GIT-009) ──────────────────────
# Tokens used to be embedded in the remote URL (x-access-token:<tok>@host),
# which git writes into .git/config; a crash between "set authed URL" and
# "scrub it" left the token on disk. Instead every network git subprocess
# gets the credential as an HTTP header via GIT_CONFIG_COUNT/KEY_n/VALUE_n
# env vars — config git reads from its own environment, never persisted.

# Provider-specific Basic-auth usernames that pair with a token as password.
_TOKEN_USER = {
    "github": "x-access-token",
    "gitlab": "oauth2",
    "bitbucket": "x-token-auth",
}


def basic_auth_value(provider: str, token: str) -> str:
    user = _TOKEN_USER.get(provider, "git")
    return base64.b64encode(f"{user}:{token}".encode()).decode("ascii")


def _url_prefix(repo_url: str) -> str | None:
    p = urlparse(repo_url)
    if not p.scheme or not p.hostname:
        return None
    host = p.hostname + (f":{p.port}" if p.port else "")
    return f"{p.scheme}://{host}/"


def git_auth_env(repo_url: str, provider: str | None = None, token: str | None = None) -> dict[str, str]:
    """Environment for ONE git subprocess talking to repo_url.

    Always: no redirects (an allowed host must not be able to bounce us to an
    internal one), no interactive prompts, no credential helper that could
    persist anything. With a token: an Authorization header scoped to
    repo_url's scheme://host/ only.
    """
    entries: list[tuple[str, str]] = [
        ("http.followRedirects", "false"),
        ("credential.helper", ""),
    ]
    prefix = _url_prefix(repo_url)
    if token and prefix:
        entries.append((
            f"http.{prefix}.extraHeader",
            f"Authorization: Basic {basic_auth_value(provider or '', token)}",
        ))
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        # Never let tracing dump the Authorization header into logs. (Not
        # GIT_CURL_VERBOSE: git checks only for its presence, so setting it
        # to anything — even "0" — turns verbose curl output ON.)
        "GIT_TRACE": "0",
        "GIT_TRACE_CURL": "0",
        "GIT_TRACE_REDACT": "1",
        "GIT_CONFIG_COUNT": str(len(entries)),
    }
    for i, (k, v) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def split_userinfo(url: str) -> tuple[str, str | None, str | None]:
    """(url without user:password, username, password). Legacy callers still
    hand us token-in-URL clone URLs; strip them before git sees them."""
    p = urlparse(url)
    if p.password is None:
        return url, None, None
    host = (p.hostname or "") + (f":{p.port}" if p.port else "")
    clean = urlunparse((p.scheme, host, p.path, p.params, p.query, p.fragment))
    return clean, unquote(p.username or ""), unquote(p.password)


_USERINFO_RE = re.compile(r"(https?://)[^/\s@]+@")
_AUTH_HEADER_RE = re.compile(r"(Authorization:\s*\w+\s+)\S+", re.IGNORECASE)


def scrub(text: object, *secrets: str | None) -> str:
    """Remove credentials from anything headed for a log line / exception /
    API response: the raw secrets (plain, URL-quoted, Basic-encoded),
    userinfo in URLs, and Authorization header values."""
    s = str(text)
    for sec in secrets:
        if not sec:
            continue
        for form in {sec, quote(sec, safe="")}:
            s = s.replace(form, "***")
        for user in set(_TOKEN_USER.values()) | {"git"}:
            s = s.replace(base64.b64encode(f"{user}:{sec}".encode()).decode("ascii"), "***")
    s = _USERINFO_RE.sub(r"\1***@", s)
    s = _AUTH_HEADER_RE.sub(r"\1***", s)
    return s


class GitCommandFailed(RuntimeError):
    """A git subprocess exited non-zero. Message is already scrubbed."""

    def __init__(self, args: list[str], returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {args[0] if args else ''} failed ({returncode}): {stderr.strip()[:500]}")


_ABORT_POLL_SECONDS = 0.1


def run_git(
    args: list[str], *, cwd: Path | str | None = None, env: dict[str, str] | None = None,
    secrets: tuple[str | None, ...] = (), guard=None,
) -> str:
    """Run one git command (blocking; call via asyncio.to_thread).

    Every working-copy mutation goes through here so it can be stopped the
    moment this request's workspace Git lock is lost (WS-GIT-009): the
    lock's guard — taken from the calling context unless passed — is checked
    before starting and polled while git runs; if it trips, the git process
    is killed and GitLockLostError raised, so a second lock holder never
    mutates the same working copy concurrently with this one.
    """
    import subprocess

    if guard is None:
        from app.services.git_lock import current_lock_guard
        guard = current_lock_guard()
    if guard is not None:
        guard.check()
    proc = subprocess.Popen(
        ["git", *args], cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # Own process group: network commands fork helpers (git-remote-https)
        # that must die with git, or they'd keep running — and keep our
        # pipes open — after an abort.
        start_new_session=True,
    )
    while True:
        try:
            out, err = proc.communicate(timeout=_ABORT_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if guard is not None and guard.lost.is_set():
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                log.error("git_aborted_lock_lost", command=args[0] if args else "", cwd=str(cwd))
                guard.check()  # raises GitLockLostError
    if proc.returncode != 0:
        raise GitCommandFailed(args, proc.returncode, scrub(err.decode(errors="replace"), *secrets))
    return out.decode(errors="replace").strip()


@dataclass(frozen=True)
class SyncResult:
    """Outcome of rebasing a working copy onto its remote — see sync_to_remote."""
    synced: bool
    remote_sha: str | None
    discarded_local_commits: bool


class LocalRepo:
    """Wraps a gitpython Repo with async-friendly methods."""

    def __init__(
        self, repo_dir: Path, repo_url: str,
        *, provider: str | None = None, token: str | None = None,
    ):
        self._dir = repo_dir
        # Never keep credentials in the URL — it would land in .git/config.
        self._url, _, url_password = split_userinfo(repo_url)
        self._token = token or url_password
        self._env = git_auth_env(self._url, provider, self._token)

    @property
    def env(self) -> dict[str, str]:
        """Per-subprocess git environment carrying the credential (if any)."""
        return dict(self._env)

    def _scrub(self, text: object) -> str:
        return scrub(text, self._token)

    @property
    def path(self) -> Path:
        return self._dir

    def _git(self, *args: str, network: bool = False, extra_env: dict[str, str] | None = None) -> str:
        env = dict(self._env) if network else {}
        if extra_env:
            env.update(extra_env)
        return run_git(list(args), cwd=self._dir, env=env, secrets=(self._token,))

    async def _run(self, *args: str, network: bool = False, extra_env: dict[str, str] | None = None) -> str:
        return await asyncio.to_thread(
            lambda: self._git(*args, network=network, extra_env=extra_env)
        )

    async def clone_or_open(self, branch: str) -> gitpy.Repo:
        if self._dir.exists() and (self._dir / ".git").exists():
            # Scrub any credential an older build (or a crash mid-push under
            # it) left in the stored remote URL — the token must not stay on
            # disk even for working copies created before this fix.
            try:
                current = await self._run("remote", "get-url", "origin")
            except GitCommandFailed:
                current = None  # no origin yet
            if current is not None and current != self._url:
                await self._run("remote", "set-url", "origin", self._url)
            return await asyncio.to_thread(gitpy.Repo, str(self._dir))
        self._dir.mkdir(parents=True, exist_ok=True)
        # Shallow clone for speed
        await asyncio.to_thread(lambda: run_git(
            ["clone", "--depth", "50", "--branch", branch, self._url, str(self._dir)],
            env=self.env, secrets=(self._token,),
        ))
        return await asyncio.to_thread(gitpy.Repo, str(self._dir))

    async def sync_to_remote(self, branch: str) -> str | None:
        """Rebase the working copy onto the remote tip before committing.

        The working copy is derived state — file content lives in object storage
        and is rewritten into the tree on every commit. But `clone_or_open` only
        *opens* an existing checkout, so its base commit is however old the first
        clone was. Committing on that stale base produces a branch that has
        diverged from the remote, and the push is then rejected as
        non-fast-forward with nothing in the UI explaining why.

        So: fetch the branch and hard-reset onto it. Resetting is safe here
        precisely because the tree is rebuilt from storage afterwards — the file
        content survives; only the local commit objects (which never reached the
        remote) are dropped and replaced by the new commit.

        Returns a SyncResult. ``synced`` is False when the remote is
        unreachable — the caller then keeps its current base rather than failing
        outright, and a later push surfaces the problem.

        ``discarded_local_commits`` is True when the reset threw away commits
        that never reached the remote (the classic "commit succeeded, push was
        rejected" state). Those commits' files are already marked non-dirty in
        metadata, so the caller MUST re-materialize the whole branch rather than
        just the dirty set — otherwise their content silently vanishes from Git.

        Credentials (if any) ride along in this LocalRepo's per-subprocess env.
        A lost workspace lock (GitLockLostError) is never swallowed here.
        """
        try:
            # Shallow clones have no remote-tracking refs, so resolve the fetched
            # tip through FETCH_HEAD rather than origin/<branch>.
            await self._run("fetch", "origin", branch, "--depth=50", network=True)
            remote_sha = await self._run("rev-parse", "FETCH_HEAD")
        except GitCommandFailed as e:  # offline / auth / no such branch
            log.warning("git_sync_fetch_failed", branch=branch, error=self._scrub(e)[:200])
            return SyncResult(synced=False, remote_sha=None, discarded_local_commits=False)

        # Is our HEAD already contained in the remote tip? If not, the reset
        # below drops local-only commits.
        discarded = False
        try:
            head = await self._run("rev-parse", "HEAD")
            if head != remote_sha:
                try:
                    await self._run("merge-base", "--is-ancestor", head, remote_sha)
                except GitCommandFailed:
                    discarded = True  # non-zero exit → HEAD not on the remote
        except GitCommandFailed:  # no commits yet
            pass

        try:
            await self._run("reset", "--hard", remote_sha)
            # Keep the local branch name pointed at the synced commit.
            await self._run("checkout", "-B", branch)
            log.info(
                "git_synced_to_remote",
                branch=branch, sha=remote_sha[:8], discarded_local=discarded,
            )
            return SyncResult(synced=True, remote_sha=remote_sha, discarded_local_commits=discarded)
        except GitCommandFailed as e:
            log.warning("git_sync_reset_failed", branch=branch, error=self._scrub(e)[:200])
            return SyncResult(synced=False, remote_sha=None, discarded_local_commits=False)

    async def checkout(self, branch: str, create: bool = False) -> None:
        await self._run("fetch", "origin", network=True)
        if create:
            await self._run("checkout", "-B", branch)
        else:
            await self._run("checkout", branch)

    async def status(self) -> dict:
        repo = await asyncio.to_thread(gitpy.Repo, str(self._dir))
        diff_index = await asyncio.to_thread(repo.index.diff, None)
        modified = [d.a_path for d in diff_index]
        untracked = list(repo.untracked_files)
        try:
            ahead, behind = await asyncio.to_thread(
                self._ahead_behind, repo, repo.active_branch.name,
            )
        except Exception:
            ahead, behind = 0, 0
        head_commit = repo.head.commit if repo.head.is_valid() else None
        return {
            "branch": repo.active_branch.name,
            "modified_files": modified,
            "untracked_files": untracked,
            "ahead": ahead,
            "behind": behind,
            "last_commit_sha": head_commit.hexsha if head_commit else None,
            "last_commit_message": head_commit.message.strip() if head_commit else None,
        }

    @staticmethod
    def _ahead_behind(repo: gitpy.Repo, branch: str) -> tuple[int, int]:
        try:
            commits_ahead = list(repo.iter_commits(f"origin/{branch}..{branch}"))
            commits_behind = list(repo.iter_commits(f"{branch}..origin/{branch}"))
            return len(commits_ahead), len(commits_behind)
        except Exception:
            return 0, 0

    async def commit_files(
        self,
        files_to_write: dict[str, bytes],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        from app.services.git_lock import check_lock_held

        check_lock_held()  # writing the tree is a mutation too
        for relpath, content in files_to_write.items():
            target = self._dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        await self._run("add", "-A")
        if not await self._run("status", "--porcelain"):
            raise RuntimeError("Nothing to commit")
        ident = {
            "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
        }
        await self._run("commit", "--no-verify", "-q", "-m", message, extra_env=ident)
        return await self._run("rev-parse", "HEAD")

    async def set_remote_url(self, url: str) -> None:
        """Point origin at a URL. Credentials are refused — they'd be persisted
        to .git/config; pass the token to LocalRepo instead."""
        clean, _, password = split_userinfo(url)
        if password is not None:
            raise ValueError("refusing to store credentials in the remote URL")
        await self._run("remote", "set-url", "origin", clean)

    async def head_sha(self) -> str:
        return await self._run("rev-parse", "HEAD")

    async def push(self, branch: str) -> None:
        try:
            await self._run("push", "--porcelain", "origin", f"{branch}:{branch}", network=True)
        except GitCommandFailed as e:
            raise RuntimeError(f"Push failed: {e.stderr.strip()[:500]}") from None

    async def pull(self, branch: str) -> str:
        """Fetch origin and hard-reset the branch to it. Returns the new HEAD sha."""
        await self._run("fetch", "origin", branch, network=True)
        await self._run("reset", "--hard", "FETCH_HEAD")
        return await self._run("rev-parse", "HEAD")


def working_dir_for(workspace_id: str) -> Path:
    """Where this workspace's local working copy lives in the data plane."""
    base = Path(os.environ.get("WORKSPACE_GIT_ROOT", "/var/lib/workspace/git"))
    return base / workspace_id
