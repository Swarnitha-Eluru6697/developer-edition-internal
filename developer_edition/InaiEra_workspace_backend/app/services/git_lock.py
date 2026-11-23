"""Per-workspace distributed lock serializing Git working-copy mutations
(WS-GIT-009).

The local working copy lives on each pod's own ephemeral volume (`emptyDir`,
not shared across replicas — see docker/helm/workspace/templates/
deployment.yaml), so a plain in-process asyncio.Lock only protects against
two concurrent requests landing on the *same* pod. With `replicas: 2+` (the
chart's default), two commit/push/pull requests for the same workspace can
just as easily land on different pods and race independently. This uses
Redis — already the shared coordination point for everything else in this
service (execution queue, OAuth state, WS tickets) — so "serialized" holds
across the whole deployment, not just one process.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

import structlog

from app.services.execution_router import get_redis

log = structlog.get_logger()

_LOCK_TTL_SECONDS = 60
# Renew well inside the TTL so one slow/missed renewal round doesn't drop the
# lock under a long clone/push/pull.
_RENEW_INTERVAL_SECONDS = _LOCK_TTL_SECONDS / 3
_ACQUIRE_TIMEOUT_SECONDS = 30
_POLL_INTERVAL_SECONDS = 0.25

# Only release if we still hold it — otherwise a slow request could delete a
# *different* holder's lock after its own TTL already expired.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Compare-and-extend: only push the TTL out while the key still carries OUR
# token. If it expired and someone else took it, we must not extend theirs.
_EXTEND_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class GitLockTimeoutError(Exception):
    """Another request is already mutating this workspace's repo."""


class GitLockLostError(GitLockTimeoutError):
    """The lock expired / was taken over while this request still held it.
    The operation was aborted so two writers never touch the working copy at
    the same time. Subclasses GitLockTimeoutError so every caller that maps
    "lock contention" to 409 maps this to 409 too."""


class LockGuard:
    """Shared between the lock holder, its renewal task and the git
    subprocess wrapper (app/git/git_local.run_git). `lost` is a
    threading.Event because it's read from worker threads: run_git polls it
    and kills the running git process the moment it is set."""

    def __init__(self, key: str):
        self.key = key
        self.lost = threading.Event()

    def check(self) -> None:
        if self.lost.is_set():
            raise GitLockLostError(
                "Lost the repository lock mid-operation (another operation took over) — "
                "this operation was aborted; retry it"
            )


_current_guard: contextvars.ContextVar[LockGuard | None] = contextvars.ContextVar(
    "workspace_git_lock_guard", default=None,
)


def current_lock_guard() -> LockGuard | None:
    """The guard of the git lock held by the current task, if any.
    Context vars propagate into asyncio.to_thread, so worker threads see it."""
    return _current_guard.get()


def check_lock_held() -> None:
    """Raise GitLockLostError if the current task's git lock was lost. Call
    before any working-copy mutation that doesn't go through run_git."""
    guard = _current_guard.get()
    if guard is not None:
        guard.check()


async def _renew_forever(r, key: str, token: str, guard: LockGuard | None = None) -> None:
    """Keep extending the lock's TTL while the holder is still working.

    A clone/push/pull of a large repo can easily outlive a fixed 60s TTL;
    without renewal the key would expire mid-operation and a second request
    could start mutating the same working copy. Runs until cancelled by the
    lock's release (or until it discovers the lock is no longer ours)."""
    ttl_ms = int(_LOCK_TTL_SECONDS * 1000)
    while True:
        await asyncio.sleep(_RENEW_INTERVAL_SECONDS)
        try:
            extended = await r.eval(_EXTEND_SCRIPT, 1, key, token, ttl_ms)
        except Exception as e:  # noqa: BLE001 — transient Redis error: retry next round
            log.warning("git_lock_renew_error", key=key, error=str(e)[:200])
            continue
        if not extended:
            # Not ours any more: signal the holder. run_git kills any git
            # process in flight and refuses to start new ones; the lock's
            # exit raises GitLockLostError so the request fails (409) and
            # its DB transaction rolls back.
            log.error("git_lock_lost", key=key)
            if guard is not None:
                guard.lost.set()
            return


@asynccontextmanager
async def workspace_git_lock(workspace_id: UUID) -> AsyncIterator[None]:
    r = get_redis()
    key = f"git-lock:{workspace_id}"
    token = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ACQUIRE_TIMEOUT_SECONDS

    acquired = False
    while loop.time() < deadline:
        acquired = await r.set(key, token, nx=True, ex=_LOCK_TTL_SECONDS)
        if acquired:
            break
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    if not acquired:
        raise GitLockTimeoutError(
            f"Another operation is already updating this repository (workspace {workspace_id})"
        )
    guard = LockGuard(key)
    ctx_token = _current_guard.set(guard)
    renewer = asyncio.create_task(_renew_forever(r, key, token, guard))
    try:
        yield
    except GitLockLostError:
        raise
    except BaseException as exc:
        if guard.lost.is_set():
            # Whatever the body failed with (e.g. a killed git process
            # surfaced as a 400), the root cause is the lost lock.
            guard.check()
        raise
    else:
        if guard.lost.is_set():
            # The body finished, but not exclusively: fail it anyway so the
            # request's transaction is rolled back rather than committed.
            guard.check()
    finally:
        _current_guard.reset(ctx_token)
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await renewer
        if not guard.lost.is_set():
            try:
                await r.eval(_RELEASE_SCRIPT, 1, key, token)
            except Exception:
                pass  # best-effort release; the TTL cleans it up regardless
