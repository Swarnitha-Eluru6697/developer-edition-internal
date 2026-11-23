"""WS-GIT-009: create-workspace-from-Git makes 'provisioning' durable before
the clone, runs the clone+import under the workspace Git lock, and commits a
final 'ready'/'failed' — never a silent 'ready' when files were dropped."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import git as git_api
from app.core.config import get_settings
from app.core.deps import get_db
from app.main import app
from app.models import GitBinding
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

from conftest import _headers

BODY = {"provider": "github", "repo_url": "https://github.com/o/r.git", "workspace_name": "w"}


class FakeDB:
    """Records what was durable at each commit; rollback restores the binding
    to its last committed state (as a real session would on refresh)."""

    def __init__(self, state):
        self.s = state

    def add(self, row):
        if isinstance(row, GitBinding):
            self.s.binding = row

    async def flush(self):
        if self.s.binding is not None and self.s.binding.id is None:
            self.s.binding.id = uuid.uuid4()

    async def commit(self):
        b = self.s.binding
        self.s.commits.append((b.status, b.status_reason) if b is not None else None)

    async def rollback(self):
        self.s.rollbacks += 1

    async def refresh(self, row):
        last = next((c for c in reversed(self.s.commits) if c), None)
        if last:
            row.status, row.status_reason = last

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.fixture
def api(monkeypatch, tmp_path):
    state = SimpleNamespace(binding=None, commits=[], rollbacks=0, lock_held=False, locks=0)

    async def _db():
        yield FakeDB(state)

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _create(self, user, req):
        return SimpleNamespace(id=uuid.uuid4(), name=req.name, storage_prefix="p")

    async def _noop(*a, **k): ...

    class _Lock:
        def __init__(self, _ws):
            state.locks += 1
        async def __aenter__(self):
            state.lock_held = True
        async def __aexit__(self, *e):
            state.lock_held = False
            return False

    monkeypatch.setenv("WORKSPACE_GIT_ROOT", str(tmp_path / "git"))
    monkeypatch.setattr(git_api, "workspace_git_lock", _Lock)
    monkeypatch.setattr(git_api, "save_git_token", _noop)
    monkeypatch.setattr(AuditService, "record", _noop)
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(WorkspaceService, "create", _create)
    app.dependency_overrides[get_db] = _db
    yield TestClient(app), state
    app.dependency_overrides.clear()


def _post(client):
    return client.post("/api/v1/git/workspaces/from-git", json=BODY, headers=_headers())


def test_provisioning_is_committed_before_clone_and_clone_runs_under_lock(api, monkeypatch):
    client, state = api
    seen = {}

    async def _import(db, ws, **k):
        seen["commits"] = list(state.commits)
        seen["locked"] = state.lock_held
        return git_api._ImportOutcome(files_imported=3)

    monkeypatch.setattr(git_api, "_clone_and_import", _import)
    r = _post(client)
    assert r.status_code == 201, r.text
    assert seen["commits"] == [("provisioning", None)]   # durable + visible before cloning
    assert seen["locked"] is True
    assert state.commits[-1] == ("ready", None)
    assert r.json()["status"] == "ready"


def test_clone_failure_commits_failed_with_reason(api, monkeypatch):
    client, state = api

    async def _import(db, ws, **k):
        return git_api._ImportOutcome(error="clone failed: auth")

    monkeypatch.setattr(git_api, "_clone_and_import", _import)
    r = _post(client)
    assert r.json()["status"] == "failed"
    assert state.commits[-1] == ("failed", "clone failed: auth")


def test_crash_during_import_rolls_back_and_commits_failed(api, monkeypatch):
    client, state = api

    async def _import(db, ws, **k):
        raise RuntimeError("storage exploded token=SECRETTOKEN")

    monkeypatch.setattr(git_api, "_clone_and_import", _import)
    client.post  # noqa: B018
    r = client.post("/api/v1/git/workspaces/from-git",
                    json={**BODY, "access_token": "SECRETTOKEN"}, headers=_headers())
    assert r.status_code == 201
    assert state.rollbacks == 1
    status, reason = state.commits[-1]
    assert status == "failed" and reason.startswith("import aborted")
    assert "SECRETTOKEN" not in reason and "SECRETTOKEN" not in r.text
    assert state.lock_held is False


def test_lock_contention_ends_failed_and_returns_409(api, monkeypatch):
    client, state = api

    class _Busy:
        def __init__(self, _w): ...
        async def __aenter__(self):
            raise git_api.GitLockTimeoutError("busy")
        async def __aexit__(self, *e): return False

    monkeypatch.setattr(git_api, "workspace_git_lock", _Busy)
    r = _post(client)
    assert r.status_code == 409
    assert state.commits[-1][0] == "failed"


def test_lock_lost_mid_import_rolls_back_persists_failed_and_returns_409(api, monkeypatch):
    client, state = api

    async def _import(db, ws, **k):
        raise git_api.GitLockLostError("Lost the repository lock mid-operation")

    monkeypatch.setattr(git_api, "_clone_and_import", _import)
    r = _post(client)
    assert r.status_code == 409
    assert "Lost the repository lock" in r.json()["detail"]["message"]
    assert state.rollbacks == 1
    status, reason = state.commits[-1]
    assert status == "failed" and reason.startswith("lock lost mid-import")


# ── The real _clone_and_import: nothing is dropped silently ─────────────

@pytest.fixture
def repo_files(monkeypatch):
    """Make _do_clone 'clone' a fixed tree; FileService records/refuses rows."""
    files: dict[str, bytes] = {}
    created: list[str] = []
    refuse: set[str] = set()

    def _clone(url, dest, branch, **k):
        from pathlib import Path
        for rel, data in files.items():
            p = Path(dest) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return git_api.CloneResult(commit_sha="a" * 40, branch=branch or "main")

    class _FS:
        def __init__(self, *a): ...
        async def create(self, ws, req):
            if req.path in refuse:
                from app.core.errors import validation_failed
                raise validation_failed("path", "Invalid path")
            created.append(req.path)
            return SimpleNamespace(is_dirty=True), req.content

    import app.services.file_service as fs_mod
    monkeypatch.setattr(git_api, "_do_clone", _clone)
    monkeypatch.setattr(fs_mod, "FileService", _FS)
    return SimpleNamespace(files=files, created=created, refuse=refuse)


def test_per_file_import_error_makes_binding_failed_and_is_listed(api, repo_files):
    client, state = api
    repo_files.files.update({"a.sql": b"select 1", "bad.sql": b"select 2"})
    repo_files.refuse.add("bad.sql")
    r = _post(client).json()
    assert r["status"] == "failed"
    assert r["files_imported"] == 1
    assert r["failed_files"] == [{"path": "bad.sql", "reason": "Invalid path"}]
    status, reason = state.commits[-1]
    assert status == "failed" and "bad.sql" in reason


def test_hitting_file_limit_makes_binding_failed(api, repo_files, monkeypatch):
    client, state = api
    monkeypatch.setattr(get_settings(), "max_files_per_workspace", 2)
    repo_files.files.update({f"d{i}/f.sql": b"x" for i in range(5)})
    r = _post(client).json()
    assert r["status"] == "failed"
    assert r["files_imported"] == 2 and r["truncated_file_count"] == 3
    assert "file limit reached" in state.commits[-1][1]


def test_binary_and_oversized_files_are_reported_not_silently_dropped(api, repo_files, monkeypatch):
    client, state = api
    monkeypatch.setattr(get_settings(), "max_file_size_bytes", 10)
    repo_files.files.update({"a.sql": b"select 1", "logo.png": b"\x89PNG\xff\xfe", "big.sql": b"x" * 50})
    r = _post(client).json()
    assert r["status"] == "ready"        # nothing that should be importable was lost...
    assert {f["path"]: f["reason"] for f in r["skipped_files"]} == {
        "logo.png": "binary", "big.sql": "over size limit",
    }
    status, reason = state.commits[-1]   # ...and the skips are persisted, not just returned
    assert status == "ready" and "logo.png" in reason and "big.sql" in reason


def test_clean_import_is_ready_with_no_reason(api, repo_files):
    client, state = api
    repo_files.files.update({"a.sql": b"select 1", "m/b.py": b"print(1)"})
    r = _post(client).json()
    assert r["status"] == "ready" and r["files_imported"] == 2
    assert state.commits[-1] == ("ready", None)
