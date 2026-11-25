"""WS-GIT-009: every operation that reaches a bound repo's remote re-checks
the egress policy (incl. per-provider host binding), and commit/push/pull/
branch ops refuse a binding that isn't 'ready'."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import git as git_api
from app.core import git_egress_policy
from app.core.deps import get_db
from app.core.git_egress_policy import (
    GitEgressPolicyError, assert_binding_url_allowed, assert_provider_host,
)
from app.main import app
from app.models import GitBinding
from app.services.audit_service import AuditService
from app.services.workspace_service import WorkspaceService

from conftest import _headers

ROLES = ["data_engineer"]


# ── Per-provider host binding ───────────────────────────────────────────

@pytest.mark.parametrize("provider,url", [
    ("github", "https://github.com/o/r.git"),
    ("gitlab", "https://gitlab.com/g/p.git"),
    ("bitbucket", "https://user@bitbucket.org/w/r.git"),
    ("azure_devops", "https://dev.azure.com/org/proj/_git/repo"),
    ("azure_devops", "https://org.visualstudio.com/proj/_git/repo"),
])
def test_provider_host_match_is_accepted(provider, url):
    assert_binding_url_allowed(provider, url)


@pytest.mark.parametrize("provider,url", [
    ("github", "https://gitlab.com/g/p.git"),
    ("github", "https://bitbucket.org/w/r.git"),
    ("github", "https://evilgithub.com/o/r.git"),
    ("gitlab", "https://github.com/o/r.git"),
    ("bitbucket", "https://github.com/o/r.git"),
    ("azure_devops", "https://github.com/o/r.git"),
    ("azure_devops", "https://visualstudio.com.evil/x"),
    ("unknown", "https://github.com/o/r.git"),
])
def test_provider_host_mismatch_is_rejected(provider, url):
    with pytest.raises(GitEgressPolicyError):
        assert_provider_host(provider, url)


def test_self_hosted_gitlab_needs_both_allowlist_and_gitlab_hosts(monkeypatch):
    url = "https://git.acme.internal/g/p.git"
    settings = SimpleNamespace(git_allowed_hosts=["git.acme.internal", "github.com"], git_gitlab_hosts=[])
    monkeypatch.setattr(git_egress_policy, "get_settings", lambda: settings)
    with pytest.raises(GitEgressPolicyError):
        assert_binding_url_allowed("gitlab", url)       # allowed host, but not a configured GitLab
    settings.git_gitlab_hosts = ["git.acme.internal"]
    assert_binding_url_allowed("gitlab", url)
    with pytest.raises(GitEgressPolicyError):
        assert_binding_url_allowed("github", url)       # still not a GitHub host


def test_bind_and_create_from_git_reject_provider_mismatch(api):
    client, _ = api
    r = client.post(f"/api/v1/git/workspaces/{uuid.uuid4()}/binding",
                    json={"provider": "github", "repo_url": "https://gitlab.com/g/p.git"},
                    headers=_headers())
    assert r.status_code == 400
    r = client.post("/api/v1/git/workspaces/from-git",
                    json={"provider": "github", "repo_url": "https://gitlab.com/g/p.git",
                          "workspace_name": "w"}, headers=_headers())
    assert r.status_code == 400
    assert "not a github host" in r.json()["detail"]["message"]


# ── Route guards ────────────────────────────────────────────────────────

class _DB:
    def __init__(self, binding):
        self.binding = binding

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalar_one_or_none=lambda: self.binding,
                               scalars=lambda: SimpleNamespace(all=lambda: []))

    async def flush(self): ...
    async def commit(self): ...
    async def rollback(self): ...
    def add(self, _r): ...


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(binding=None, git_touched=[], locks=[])

    async def _db():
        yield _DB(state.binding)

    async def _ensure_user(self, principal):
        return SimpleNamespace(id=uuid.uuid4(), external_id=principal.user_id)

    async def _get(self, user, wid):
        return SimpleNamespace(id=wid, name="w")

    async def _noop(*a, **k): ...

    async def _token(*_a):
        return "tok"

    class _Lock:
        def __init__(self, ws):
            state.locks.append(ws)
        async def __aenter__(self): ...
        async def __aexit__(self, *e): return False

    class _Local:
        """Records that the route got as far as touching git."""
        def __init__(self, *a, **k):
            state.git_touched.append(a)
        async def clone_or_open(self, **k): ...
        async def sync_to_remote(self, b): return None
        async def push(self, **k): ...
        async def pull(self, b): return "sha"
        async def checkout(self, *a, **k): ...
        async def commit_files(self, **k): raise RuntimeError("Nothing to commit")
        async def head_sha(self): return "0" * 40

    import app.git.git_local as gl
    monkeypatch.setattr(gl, "LocalRepo", _Local)
    monkeypatch.setattr(git_api, "workspace_git_lock", _Lock)
    monkeypatch.setattr(git_api, "load_git_token", _token)
    monkeypatch.setattr(AuditService, "record", _noop)
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    monkeypatch.setattr(WorkspaceService, "get", _get)
    app.dependency_overrides[get_db] = _db
    yield TestClient(app), state
    app.dependency_overrides.clear()


def _binding(status="ready", provider="github", url="https://github.com/o/r.git"):
    return GitBinding(provider=provider, repo_url=url, current_branch="main", default_branch="main",
                      status=status, status_reason="clone failed: boom" if status == "failed" else None,
                      repo_full_name="o/r")


OPS = [
    ("post", "commit", {"message": "m", "allow_empty": True}),
    ("post", "push", None),
    ("post", "pull", None),
    ("post", "branches", {"name": "feature"}),
    ("patch", "branch", {"branch": "dev"}),
]


# Pull is the recovery path out of 'failed' (it re-syncs the whole tree), so
# it's refused only while 'provisioning' — see tests/test_git_pull_reimport.py.
NOT_READY = [(m, o, b, st) for (m, o, b) in OPS for st in ("provisioning", "failed")
             if not (o == "pull" and st == "failed")]


@pytest.mark.parametrize("method,op,body,status", NOT_READY)
def test_ops_refuse_binding_that_is_not_ready(api, method, op, body, status):
    client, state = api
    state.binding = _binding(status=status)
    r = getattr(client, method)(f"/api/v1/git/workspaces/{uuid.uuid4()}/{op}",
                                json=body, headers=_headers(roles=ROLES))
    assert r.status_code == 409, r.text
    assert status in r.json()["detail"]["message"]
    assert state.git_touched == [] and state.locks == []


@pytest.mark.parametrize("method,op,body", OPS)
@pytest.mark.parametrize("url", [
    "http://github.com/o/r.git",                  # pre-allowlist binding: plain http
    "https://169.254.169.254/latest/meta-data",   # pre-allowlist binding: metadata IP
    "https://gitlab.com/g/p.git",                 # allowed host, wrong provider
])
def test_ops_recheck_egress_on_existing_bindings(api, method, op, body, url):
    client, state = api
    state.binding = _binding(url=url)
    r = getattr(client, method)(f"/api/v1/git/workspaces/{uuid.uuid4()}/{op}",
                                json=body, headers=_headers(roles=ROLES))
    assert r.status_code == 403, r.text
    assert state.git_touched == [] and state.locks == []


def test_pull_request_route_rechecks_egress(api):
    client, state = api
    state.binding = _binding(url="https://169.254.169.254/x")
    r = client.post(f"/api/v1/git/workspaces/{uuid.uuid4()}/pull-requests",
                    json={"source_branch": "a", "target_branch": "main", "title": "t", "body": ""},
                    headers=_headers(roles=ROLES))
    assert r.status_code == 403


@pytest.mark.parametrize("method,op,body", OPS)
def test_ops_proceed_under_lock_for_ready_allowed_binding(api, method, op, body):
    client, state = api
    state.binding = _binding()
    r = getattr(client, method)(f"/api/v1/git/workspaces/{uuid.uuid4()}/{op}",
                                json=body, headers=_headers(roles=ROLES))
    assert r.status_code < 300, r.text
    assert len(state.locks) == 1 and state.git_touched
