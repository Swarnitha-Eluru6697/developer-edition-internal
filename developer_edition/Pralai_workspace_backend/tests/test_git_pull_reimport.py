"""WS-GIT-009: pull reports every file it couldn't re-import — returned to
the caller and persisted on the binding — and a pull with failures never
looks like a clean success (and blocks commit/push until a clean pull)."""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import git as git_api
from app.core.deps import get_db
from app.main import app
from app.models import GitBinding
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

from conftest import _headers

ROLES = ["data_engineer"]


@pytest.fixture
def api(monkeypatch, tmp_path):
    ws_id = uuid.uuid4()
    st = SimpleNamespace(binding=None, tree={}, refuse=set(), db_error_on=None,
                         commits=[], rollbacks=0, upserted=[], created=[], ws_id=ws_id)
    repo_dir = tmp_path / "git" / str(ws_id)
    monkeypatch.setenv("WORKSPACE_GIT_ROOT", str(tmp_path / "git"))

    class _Local:
        def __init__(self, *a, **k): ...
        async def clone_or_open(self, **k): ...
        async def pull(self, branch):
            for rel, data in st.tree.items():
                p = repo_dir / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            return "f" * 40

    class _FS:
        def __init__(self, *a): ...
        async def find_by_path(self, ws, rel, branch):
            return SimpleNamespace(id=rel, is_dirty=True) if rel.startswith("existing") else None
        async def update(self, ws, fid, req):
            self._check(fid)
            st.upserted.append(fid)
        async def create(self, ws, req):
            self._check(req.path)
            st.created.append(req.path)
            return SimpleNamespace(is_dirty=True), req.content
        def _check(self, path):
            if path == st.db_error_on:
                raise OperationalError("insert", {}, Exception("connection reset"))
            if path in st.refuse:
                from app.core.errors import validation_failed
                raise validation_failed("path", "Invalid path")

    class _DB:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(scalar_one_or_none=lambda: st.binding)
        async def flush(self): ...
        async def commit(self):
            st.commits.append((st.binding.status, st.binding.status_reason))
        async def rollback(self):
            st.rollbacks += 1
        async def refresh(self, row): ...
        def add(self, _r): ...

    async def _db():
        db = _DB()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    class _NoLock:
        def __init__(self, _w): ...
        async def __aenter__(self): ...
        async def __aexit__(self, *e): return False

    async def _noop(*a, **k): ...

    async def _token(*_a):
        return None

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _get(self, user, wid):
        return SimpleNamespace(id=ws_id, name="w")

    import app.git.git_local as gl
    import app.services.file_service as fs_mod
    monkeypatch.setattr(gl, "LocalRepo", _Local)
    monkeypatch.setattr(fs_mod, "FileService", _FS)
    monkeypatch.setattr(git_api, "workspace_git_lock", _NoLock)
    monkeypatch.setattr(git_api, "load_git_token", _token)
    monkeypatch.setattr(AuditService, "record", _noop)
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(WorkspaceService, "get", _get)
    app.dependency_overrides[get_db] = _db
    yield TestClient(app), st
    app.dependency_overrides.clear()


def _binding(status="ready", reason=None):
    return GitBinding(provider="github", repo_url="https://github.com/o/r.git", current_branch="main",
                      default_branch="main", status=status, status_reason=reason)


def _pull(client, st):
    return client.post(f"/api/v1/git/workspaces/{st.ws_id}/pull", headers=_headers(roles=ROLES))


def test_clean_pull_is_pulled_and_ready(api):
    client, st = api
    st.binding = _binding()
    st.tree = {"existing.sql": b"select 1", "new.sql": b"select 2"}
    r = _pull(client, st)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pulled" and body["files_synced"] == 2
    assert body["binding_status"] == "ready" and body["failed_files"] == []
    assert st.upserted == ["existing.sql"] and st.created == ["new.sql"]
    assert st.commits[-1] == ("ready", None)


def test_pull_with_failed_files_is_reported_persisted_and_blocks_commit(api):
    client, st = api
    st.binding = _binding()
    st.tree = {"ok.sql": b"select 1", "bad.sql": b"select 2", "existing-bad.sql": b"x"}
    st.refuse = {"bad.sql", "existing-bad.sql"}
    body = _pull(client, st).json()
    assert body["status"] == "pulled_with_errors"
    assert body["files_synced"] == 1 and body["failed_file_count"] == 2
    assert {f["path"] for f in body["failed_files"]} == {"bad.sql", "existing-bad.sql"}
    assert body["binding_status"] == "failed"
    status, reason = st.commits[-1]
    assert status == "failed" and "bad.sql" in reason and "existing-bad.sql" in reason

    # the workspace copy of those files is stale: commit/push must refuse now
    for op, js in (("commit", {"message": "m", "allow_empty": True}), ("push", None)):
        r = client.post(f"/api/v1/git/workspaces/{st.ws_id}/{op}", json=js, headers=_headers(roles=ROLES))
        assert r.status_code == 409, (op, r.text)


def test_clean_pull_recovers_a_failed_binding(api):
    client, st = api
    st.binding = _binding("failed", "1 file(s) failed to import: bad.sql (Invalid path)")
    st.tree = {"bad.sql": b"select 2"}
    body = _pull(client, st).json()
    assert body["status"] == "pulled" and body["binding_status"] == "ready"
    assert st.commits[-1] == ("ready", None)


def test_pull_refused_while_provisioning(api):
    client, st = api
    st.binding = _binding("provisioning")
    assert _pull(client, st).status_code == 409


def test_pull_reports_file_limit_as_failure(api, monkeypatch):
    from app.core.config import get_settings
    client, st = api
    monkeypatch.setattr(get_settings(), "max_files_per_workspace", 1)
    st.binding = _binding()
    st.tree = {"a.sql": b"1", "b.sql": b"2", "c.sql": b"3"}
    body = _pull(client, st).json()
    assert body["status"] == "pulled_with_errors" and body["truncated_file_count"] == 2
    assert "file limit reached" in st.commits[-1][1]


def test_pull_reports_binary_files_without_failing(api):
    client, st = api
    st.binding = _binding()
    st.tree = {"a.sql": b"1", "img.png": b"\x89PNG\xff"}
    body = _pull(client, st).json()
    assert body["status"] == "pulled" and body["binding_status"] == "ready"
    assert body["skipped_files"] == [{"path": "img.png", "reason": "binary"}]
    assert "img.png" in st.commits[-1][1]


def test_pull_db_error_rolls_back_and_persists_failed(api):
    client, st = api
    st.binding = _binding()
    st.tree = {"a.sql": b"1", "b.sql": b"2"}
    st.db_error_on = "b.sql"
    r = _pull(client, st)
    assert r.status_code == 400
    assert st.rollbacks >= 1
    status, reason = st.commits[0]
    assert status == "failed" and "re-import aborted" in reason
