"""WS-GIT-009: Azure DevOps OAuth via Entra ID v2, constant-time ADO webhook
auth, and no known-default webhook secret outside dev."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import app.git as git_pkg
from app.git import azure_devops as ado_mod
from app.git.azure_devops import AzureDevOpsProvider
from app.main import app

from conftest import _headers

TENANT = "11111111-2222-3333-4444-555555555555"


def _ado(**kw):
    args = dict(client_id="cid", client_secret="csecret", webhook_basic_user="hook",
                webhook_basic_pass="hookpass", tenant_id=TENANT)
    args.update(kw)
    return AzureDevOpsProvider(**args)


def test_ado_authorize_url_is_entra_v2_with_pkce():
    url = _ado().oauth_authorize_url(state="st", redirect_uri="https://ws/cb", code_challenge="chal")
    p = urlparse(url)
    assert f"{p.scheme}://{p.netloc}{p.path}" == \
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    assert q["client_id"] == "cid" and q["response_type"] == "code" and q["state"] == "st"
    assert q["scope"] == "499b84ac-1321-427f-aa17-267ca6975798/.default offline_access"
    assert q["code_challenge"] == "chal" and q["code_challenge_method"] == "S256"
    assert "csecret" not in url


@pytest.fixture
def token_endpoint(monkeypatch):
    seen = []
    reply = {"status": 200, "json": {"access_token": "AT", "refresh_token": "RT", "expires_in": 3599}}

    def handler(request: httpx.Request):
        seen.append((str(request.url), parse_qs(request.content.decode())))
        return httpx.Response(reply["status"], json=reply["json"])

    real = httpx.AsyncClient
    monkeypatch.setattr(ado_mod.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return SimpleNamespace(seen=seen, reply=reply)


def test_ado_code_exchange_sends_verifier_and_parses_tokens(token_endpoint):
    pair = asyncio.run(_ado().exchange_code_for_token("CODE", "https://ws/cb", code_verifier="VER"))
    assert (pair.access_token, pair.refresh_token, pair.expires_in) == ("AT", "RT", 3599)
    url, form = token_endpoint.seen[0]
    assert url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["CODE"]
    assert form["code_verifier"] == ["VER"] and form["client_secret"] == ["csecret"]
    assert form["redirect_uri"] == ["https://ws/cb"]


def test_ado_refresh(token_endpoint):
    pair = asyncio.run(_ado().refresh_token("OLD"))
    assert pair.access_token == "AT"
    form = token_endpoint.seen[0][1]
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["OLD"]
    assert "offline_access" in form["scope"][0]


def test_ado_token_error_does_not_echo_secrets(token_endpoint):
    token_endpoint.reply.update(status=400, json={"error": "invalid_grant", "error_description": "csecret CODE"})
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(_ado().exchange_code_for_token("CODE", "r", code_verifier="VER"))
    assert "invalid_grant" in str(ei.value)
    assert "csecret" not in str(ei.value) and "CODE" not in str(ei.value)


@pytest.mark.parametrize("kw", [{"tenant_id": None}, {"client_secret": ""}])
def test_ado_oauth_unconfigured_raises_not_implemented(kw):
    p = _ado(**kw)
    with pytest.raises(NotImplementedError):
        p.oauth_authorize_url(state="s", redirect_uri="r")
    with pytest.raises(NotImplementedError):
        asyncio.run(p.exchange_code_for_token("c", "r"))
    with pytest.raises(NotImplementedError):
        asyncio.run(p.refresh_token("r"))


@pytest.mark.parametrize("tenant", ["evil.com/x", "a?b", "../x", ""])
def test_ado_rejects_tenant_that_could_alter_the_authority_url(tenant):
    with pytest.raises(ValueError):
        _ado(tenant_id=tenant)


@pytest.fixture
def fresh_providers(monkeypatch):
    for k in ("GITHUB_CLIENT_ID", "GITLAB_CLIENT_ID", "BITBUCKET_CLIENT_ID", "ADO_CLIENT_ID",
              "AZURE_DEVOPS_CLIENT_ID", "AZURE_DEVOPS_CLIENT_SECRET", "AZURE_DEVOPS_TENANT_ID",
              "GITHUB_WEBHOOK_SECRET", "GITLAB_WEBHOOK_SECRET", "BITBUCKET_WEBHOOK_SECRET",
              "ADO_WEBHOOK_PASS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(git_pkg, "_providers", None)
    yield monkeypatch
    git_pkg._providers = None


def _env_settings(monkeypatch, environment):
    monkeypatch.setattr(git_pkg, "get_settings", lambda: SimpleNamespace(environment=environment))


def test_ado_configured_from_azure_devops_env(fresh_providers):
    m = fresh_providers
    m.setenv("AZURE_DEVOPS_CLIENT_ID", "cid")
    m.setenv("AZURE_DEVOPS_CLIENT_SECRET", "sec")
    m.setenv("AZURE_DEVOPS_TENANT_ID", TENANT)
    p = git_pkg.get_provider("azure_devops")
    assert p.oauth_configured
    assert TENANT in p.oauth_authorize_url(state="s", redirect_uri="r")


def test_ado_connect_without_oauth_config_is_clean_validation_error(fresh_providers):
    from app.core.deps import get_db
    from app.services.workspace_service import WorkspaceService

    async def _db():
        yield SimpleNamespace()

    async def _ensure_user(self, principal):
        return SimpleNamespace(id="u", external_id=principal.user_id)

    fresh_providers.setattr(WorkspaceService, "ensure_user", _ensure_user)
    fresh_providers.setenv("ADO_CLIENT_ID", "cid")  # PAT-only setup
    app.dependency_overrides[get_db] = _db
    try:
        r = TestClient(app).get("/api/v1/git/azure_devops/connect", headers=_headers())
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 400
    assert "personal access token" in r.json()["detail"]["message"]


def test_ado_webhook_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(ado_mod.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    p = _ado()
    good = "Basic " + base64.b64encode(b"hook:hookpass").decode()
    bad = "Basic " + base64.b64encode(b"hook:wrong").decode()
    assert p.verify_webhook({"authorization": good}, b"") is True
    assert p.verify_webhook({"Authorization": bad}, b"") is False
    assert len(calls) == 4  # user AND password compared, constant-time, on both calls
    assert p.verify_webhook({"Authorization": "Basic !!notb64"}, b"") is False


# ── Webhook secrets: the "dev-secret" default never works outside dev ───

def _gh_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("environment", ["staging", "prod"])
@pytest.mark.parametrize("configured", [None, "dev-secret"])
def test_known_default_webhook_secret_rejected_outside_dev(fresh_providers, environment, configured):
    m = fresh_providers
    _env_settings(m, environment)
    for name in ("GITHUB", "GITLAB", "BITBUCKET"):
        m.setenv(f"{name}_CLIENT_ID", "x")
        m.setenv(f"{name}_CLIENT_SECRET", "x")
        if configured:
            m.setenv(f"{name}_WEBHOOK_SECRET", configured)
    m.setenv("ADO_CLIENT_ID", "x")
    if configured:
        m.setenv("ADO_WEBHOOK_PASS", configured)
    body = b'{"x":1}'
    assert not git_pkg.get_provider("github").verify_webhook(
        {"x-hub-signature-256": _gh_sig("dev-secret", body)}, body)
    assert not git_pkg.get_provider("gitlab").verify_webhook({"x-gitlab-token": "dev-secret"}, body)
    assert not git_pkg.get_provider("bitbucket").verify_webhook(
        {"x-hub-signature": _gh_sig("dev-secret", body)}, body)
    assert not git_pkg.get_provider("azure_devops").verify_webhook(
        {"authorization": "Basic " + base64.b64encode(b"webhook:dev-secret").decode()}, body)
    # self-hosted GitLab path: no KeyError, and no default either
    m.setenv("GITLAB_CLIENT_ID", "x")
    from app.core import git_egress_policy
    m.setattr(git_egress_policy, "get_settings", lambda: SimpleNamespace(git_allowed_hosts=["git.acme.io"], git_gitlab_hosts=["git.acme.io"]))
    assert not git_pkg.get_provider("gitlab", instance_url="https://git.acme.io").verify_webhook(
        {"x-gitlab-token": "dev-secret"}, body)


def test_real_secret_still_works_outside_dev(fresh_providers):
    m = fresh_providers
    _env_settings(m, "prod")
    m.setenv("GITHUB_CLIENT_ID", "x")
    m.setenv("GITHUB_CLIENT_SECRET", "x")
    m.setenv("GITHUB_WEBHOOK_SECRET", "s3cr3t-real")
    body = b"{}"
    gh = git_pkg.get_provider("github")
    assert gh.verify_webhook({"x-hub-signature-256": _gh_sig("s3cr3t-real", body)}, body)
    assert not gh.verify_webhook({"x-hub-signature-256": _gh_sig("dev-secret", body)}, body)


def test_dev_keeps_the_placeholder_for_local_work(fresh_providers):
    m = fresh_providers
    _env_settings(m, "dev")
    m.setenv("GITHUB_CLIENT_ID", "x")
    m.setenv("GITHUB_CLIENT_SECRET", "x")
    body = b"{}"
    assert git_pkg.get_provider("github").verify_webhook(
        {"x-hub-signature-256": _gh_sig("dev-secret", body)}, body)


def test_webhook_route_rejects_bad_signature_with_401(fresh_providers):
    m = fresh_providers
    _env_settings(m, "prod")
    m.setenv("GITHUB_CLIENT_ID", "x")
    m.setenv("GITHUB_CLIENT_SECRET", "x")
    client = TestClient(app)
    body = b"{}"
    r = client.post("/api/v1/git/webhooks/github", content=body,
                    headers={"X-Hub-Signature-256": _gh_sig("dev-secret", body)})
    assert r.status_code == 401
    assert client.post("/api/v1/git/webhooks/nope", content=b"{}").status_code == 404
