"""WS-GIT-009: the OAuth connect flow can't be pointed at an arbitrary
GitLab host (client-secret exfiltration / SSRF), and can't be completed in a
browser other than the one that started it (account-linking CSRF)."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.api import git as git_api
from app.core.deps import get_db
from app.core.git_egress_policy import GitEgressPolicyError
from app.git import get_provider
from app.main import app
from app.services.git_oauth_state import BINDING_COOKIE, binding_matches, new_browser_binding
from app.services.workspace_service import WorkspaceService

from conftest import _headers


@pytest.fixture(autouse=True)
def _gitlab_env(monkeypatch):
    for k in ("GITLAB_CLIENT_ID", "GITLAB_CLIENT_SECRET", "GITLAB_WEBHOOK_SECRET"):
        monkeypatch.setenv(k, "x")


# ── Self-hosted GitLab instance_url must be allowlisted ──────────────────

@pytest.mark.parametrize("url", [
    "https://attacker.example",
    "http://gitlab.com",                 # not https
    "https://169.254.169.254",           # metadata endpoint
    "https://gitlab.com.attacker.example",
])
def test_unlisted_gitlab_instance_is_refused(url):
    with pytest.raises(GitEgressPolicyError):
        get_provider("gitlab", instance_url=url)


def test_allowlisted_gitlab_instance_is_accepted(monkeypatch):
    from app.core import git_egress_policy
    monkeypatch.setattr(git_egress_policy, "get_settings",
                        lambda: SimpleNamespace(git_allowed_hosts=["git.acme.internal"],
                                                git_gitlab_hosts=["git.acme.internal"]))
    p = get_provider("gitlab", instance_url="https://git.acme.internal")
    assert p._instance == "https://git.acme.internal"


@pytest.mark.parametrize("gitlab_hosts", [[], ["other.gitlab.internal"]])
def test_allowlisted_instance_that_is_not_a_configured_gitlab_is_refused(monkeypatch, gitlab_hosts):
    """On the global allowlist isn't enough: the instance must be a configured
    GitLab host too, or GITLAB_CLIENT_SECRET could be POSTed to, say, an
    allowed Bitbucket/ADO host (per-provider host binding, WS-GIT-009)."""
    from app.core import git_egress_policy
    monkeypatch.setattr(git_egress_policy, "get_settings",
                        lambda: SimpleNamespace(git_allowed_hosts=["git.acme.internal"],
                                                git_gitlab_hosts=gitlab_hosts))
    with pytest.raises(GitEgressPolicyError):
        get_provider("gitlab", instance_url="https://git.acme.internal")


@pytest.fixture
def api(monkeypatch):
    async def _db():
        yield SimpleNamespace(execute=None, flush=None, add=lambda *_: None)

    async def _ensure_user(self, principal):
        return SimpleNamespace(id="u-row", external_id=principal.user_id)

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(WorkspaceService, "ensure_user", _ensure_user)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_connect_with_unlisted_instance_is_422_and_stores_no_state(api, monkeypatch):
    stored = []

    async def _store(*a, **k):
        stored.append(k)
    monkeypatch.setattr(git_api, "store_oauth_state", _store)
    r = api.get("/api/v1/git/gitlab/connect", params={"instance_url": "https://attacker.example"},
                headers=_headers())
    assert r.status_code == 400
    assert "not permitted" in r.json()["detail"]["message"]
    assert stored == []


# ── Callback must come from the browser that started the flow ────────────

def test_binding_matches_only_its_own_nonce():
    nonce, h = new_browser_binding()
    other, _ = new_browser_binding()
    assert binding_matches(nonce, h)
    assert not binding_matches(other, h)
    assert not binding_matches(None, h)
    assert not binding_matches(nonce, None)


def _callback_setup(monkeypatch, binding_hash):
    exchanged = []

    async def _consume(_state):
        return {"user_id": "attacker", "provider": "github", "redirect_uri": "r",
                "code_verifier": "v", "instance_url": None, "browser_binding": binding_hash}

    class _Provider:
        async def exchange_code_for_token(self, *a, **k):
            exchanged.append(a)
            return SimpleNamespace(access_token="victim-token")
    monkeypatch.setattr(git_api, "consume_oauth_state", _consume)
    monkeypatch.setattr(git_api, "get_provider", lambda *a, **k: _Provider())
    return exchanged


@pytest.mark.parametrize("cookie", [None, "someone-elses-nonce"])
def test_callback_without_matching_browser_binding_is_rejected(monkeypatch, cookie):
    _, binding_hash = new_browser_binding()
    exchanged = _callback_setup(monkeypatch, binding_hash)
    client = TestClient(app)
    if cookie:
        client.cookies.set(BINDING_COOKIE, cookie)
    r = client.get("/api/v1/git/github/callback", params={"code": "c", "state": "s"}, follow_redirects=False)
    assert r.status_code in (302, 307)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert "git_connect_error" in q
    assert exchanged == []        # the code was never exchanged, no token saved
