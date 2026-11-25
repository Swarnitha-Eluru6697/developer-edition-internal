"""WS-GIT-009: the per-workspace Git lock is renewed while held (so a long
clone/push/pull keeps it), never extends someone else's lock, stops renewing
on release, and the routes release it on every path."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import git_lock
from app.services.git_lock import GitLockLostError, GitLockTimeoutError, workspace_git_lock


class FakeRedis:
    """In-memory Redis with real TTL semantics for SET NX EX and the two Lua
    scripts the lock uses (release, compare-and-extend)."""

    def __init__(self):
        self.data: dict[str, tuple[str, float]] = {}
        self.extends = 0

    def _now(self):
        return asyncio.get_running_loop().time()

    def _get(self, key):
        v = self.data.get(key)
        if v and v[1] <= self._now():
            del self.data[key]
            return None
        return v[0] if v else None

    async def set(self, key, value, nx=False, ex=None):
        if nx and self._get(key) is not None:
            return None
        self.data[key] = (value, self._now() + ex)
        return True

    async def eval(self, script, _n, key, token, *args):
        if self._get(key) != token:
            return 0
        if "pexpire" in script:
            self.extends += 1
            self.data[key] = (token, self._now() + int(args[0]) / 1000)
            return 1
        del self.data[key]
        return 1


@pytest.fixture
def redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(git_lock, "get_redis", lambda: r)
    monkeypatch.setattr(git_lock, "_LOCK_TTL_SECONDS", 0.3)
    monkeypatch.setattr(git_lock, "_RENEW_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(git_lock, "_ACQUIRE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(git_lock, "_POLL_INTERVAL_SECONDS", 0.02)
    return r


def test_lock_is_renewed_past_its_ttl_while_held(redis):
    ws = uuid.uuid4()

    async def run():
        async with workspace_git_lock(ws):
            await asyncio.sleep(1.0)  # > 3x the TTL
            # still ours: a second acquirer must time out
            with pytest.raises(GitLockTimeoutError):
                async with workspace_git_lock(ws):
                    pass
        # released → immediately acquirable again
        async with workspace_git_lock(ws):
            pass

    asyncio.run(run())
    assert redis.extends >= 5


def test_without_renewal_the_lock_would_have_expired(redis, monkeypatch):
    """Control: proves the renewal (not the fake) is what kept it alive."""
    monkeypatch.setattr(git_lock, "_RENEW_INTERVAL_SECONDS", 3600)
    ws = uuid.uuid4()

    async def run():
        async with workspace_git_lock(ws):
            await asyncio.sleep(0.5)
            async with workspace_git_lock(ws):  # TTL lapsed → second holder gets in
                pass

    asyncio.run(run())


def test_release_stops_renewal(redis):
    ws = uuid.uuid4()

    async def run():
        async with workspace_git_lock(ws):
            await asyncio.sleep(0.15)
        n = redis.extends
        await asyncio.sleep(0.4)
        assert redis.extends == n  # no renewal after release
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(run())


def test_renewal_never_extends_another_holders_lock(redis):
    ws = uuid.uuid4()
    key = f"git-lock:{ws}"

    async def run():
        with pytest.raises(GitLockLostError):  # and losing it fails the block
            async with workspace_git_lock(ws):
                # Simulate our lock having expired and someone else taking it.
                redis.data[key] = ("someone-else", asyncio.get_running_loop().time() + 0.25)
                await asyncio.sleep(0.15)
                # Renewer must not have touched the other holder's key.
                assert redis.data[key][0] == "someone-else"
        # and our release didn't delete their lock either
        assert redis._get(key) == "someone-else"

    asyncio.run(run())
    assert redis.extends == 0


def test_lock_released_when_body_raises(redis):
    ws = uuid.uuid4()

    async def run():
        with pytest.raises(RuntimeError):
            async with workspace_git_lock(ws):
                raise RuntimeError("boom")
        assert redis._get(f"git-lock:{ws}") is None

    asyncio.run(run())


# ── Route-level: every acquisition is released, on every path ───────────

def test_commit_releases_lock_when_failing_between_acquire_and_work(monkeypatch):
    """Previously the lock was entered before the try/finally started, so an
    exception in the dirty-file query leaked it until the TTL ran out."""
    from fastapi.testclient import TestClient

    from app.api import git as git_api
    from app.core.deps import get_db
    from app.main import app
    from app.models import GitBinding
    from app.services.workspace_service import WorkspaceService

    from conftest import _headers

    events: list[str] = []

    class _Lock:
        def __init__(self, _ws):
            pass

        async def __aenter__(self):
            events.append("acquire")

        async def __aexit__(self, *exc):
            events.append("release")
            return False

    binding = GitBinding(provider="github", repo_url="https://github.com/o/r.git",
                         current_branch="main", default_branch="main", status="ready")
    calls = {"n": 0}

    class _DB:
        async def execute(self, *_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return SimpleNamespace(scalar_one_or_none=lambda: binding)
            raise RuntimeError("db exploded inside the locked section")

        async def flush(self): ...
        async def commit(self): ...
        async def rollback(self): ...
        def add(self, _r): ...

    async def _db():
        yield _DB()

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _get(self, user, wid):
        return SimpleNamespace(id=wid, name="w")

    monkeypatch.setattr(git_api, "workspace_git_lock", _Lock)
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(WorkspaceService, "get", _get)
    app.dependency_overrides[get_db] = _db
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(f"/api/v1/git/workspaces/{uuid.uuid4()}/commit",
                        json={"message": "m"}, headers=_headers(roles=["data_engineer"]))
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 500
    assert events == ["acquire", "release"]


# ── Losing the lock mid-operation aborts the operation ──────────────────

from app.services.git_lock import check_lock_held  # noqa: E402


def _steal(redis, ws):
    redis.data[f"git-lock:{ws}"] = ("other-writer", asyncio.get_running_loop().time() + 60)


def test_lost_lock_trips_guard_and_fails_the_block(redis):
    ws = uuid.uuid4()

    async def run():
        with pytest.raises(GitLockLostError):
            async with workspace_git_lock(ws):
                check_lock_held()          # fine while we hold it
                _steal(redis, ws)
                await asyncio.sleep(0.25)  # > renew interval: renewer notices
                check_lock_held()          # next mutation is refused
                raise AssertionError("must not get here")
        assert redis._get(f"git-lock:{ws}") == "other-writer"  # theirs, untouched

    asyncio.run(run())


def test_block_that_ignores_the_guard_still_fails_on_exit(redis):
    """Even if the body finished without checking, a lost lock means the work
    wasn't exclusive: the block raises so the request's DB transaction rolls
    back instead of committing."""
    ws = uuid.uuid4()

    async def run():
        with pytest.raises(GitLockLostError):
            async with workspace_git_lock(ws):
                _steal(redis, ws)
                await asyncio.sleep(0.25)

    asyncio.run(run())


def test_other_errors_after_losing_the_lock_surface_as_lock_lost(redis):
    ws = uuid.uuid4()

    async def run():
        with pytest.raises(GitLockLostError):
            async with workspace_git_lock(ws):
                _steal(redis, ws)
                await asyncio.sleep(0.25)
                raise ValueError("git process killed")  # e.g. a killed git → 400 upstream

    asyncio.run(run())


def test_guard_only_visible_inside_the_lock(redis):
    async def run():
        check_lock_held()  # no lock → no-op
        async with workspace_git_lock(uuid.uuid4()):
            from app.services.git_lock import current_lock_guard
            assert current_lock_guard() is not None
            # propagates into worker threads (git runs via asyncio.to_thread)
            assert await asyncio.to_thread(current_lock_guard) is current_lock_guard()
        from app.services.git_lock import current_lock_guard
        assert current_lock_guard() is None

    asyncio.run(run())


def test_push_route_returns_409_when_lock_is_lost_mid_push(redis, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import git as git_api
    from app.core.deps import get_db
    from app.main import app
    from app.models import GitBinding
    from app.services.audit_service import AuditService
    from app.services.workspace_service import WorkspaceService
    import app.git.git_local as gl

    from conftest import _headers

    ws_id = uuid.uuid4()
    progress = []

    class _Local:
        def __init__(self, *a, **k): ...
        async def clone_or_open(self, **k): ...
        async def push(self, **k):
            progress.append("push-started")
            _steal(redis, ws_id)             # lock expires / another pod takes it
            await asyncio.sleep(0.3)
            check_lock_held()                # what run_git does before/while running git
            progress.append("push-finished")  # must never be reached

    binding = GitBinding(provider="github", repo_url="https://github.com/o/r.git",
                         current_branch="main", default_branch="main", status="ready")

    class _DB:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: binding)
        async def flush(self): ...
        async def commit(self): progress.append("db-commit")
        async def rollback(self): progress.append("db-rollback")
        def add(self, _r): ...

    async def _db():
        db = _DB()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _noop(*a, **k): ...

    async def _token(*_a):
        return "tok"

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _get(self, user, wid):
        return SimpleNamespace(id=ws_id, name="w")

    monkeypatch.setattr(gl, "LocalRepo", _Local)
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
    assert r.status_code == 409, r.text
    assert "Lost the repository lock" in r.json()["detail"]["message"]
    assert "push-finished" not in progress
    assert "db-commit" not in progress and "db-rollback" in progress
