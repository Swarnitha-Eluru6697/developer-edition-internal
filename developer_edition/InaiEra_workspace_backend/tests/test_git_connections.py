"""Git connection status for the "From Git repository" dialog."""
from __future__ import annotations

import pytest

import app.git as git_mod
from app.api.git import GitConnectionOut


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    for v in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITLAB_CLIENT_ID", "BITBUCKET_CLIENT_ID",
              "AZURE_DEVOPS_CLIENT_ID", "ADO_CLIENT_ID", "AZURE_DEVOPS_TENANT_ID"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(git_mod, "_providers", None)
    yield
    git_mod._providers = None


def test_oauth_unavailable_without_a_configured_oauth_app():
    assert all(not git_mod.oauth_available(p) for p in git_mod.SUPPORTED_PROVIDERS)


def test_oauth_available_once_the_provider_app_is_configured(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csecret")
    assert git_mod.oauth_available("github") is True
    assert git_mod.oauth_available("gitlab") is False


def test_azure_devops_without_tenant_is_pat_only_not_oauth(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_CLIENT_ID", "cid")
    monkeypatch.setenv("AZURE_DEVOPS_CLIENT_SECRET", "csecret")
    assert git_mod.oauth_available("azure_devops") is False


def test_connection_status_never_exposes_a_token():
    fields = set(GitConnectionOut.model_fields)
    assert not {"token", "access_token", "vault_path", "secret"} & fields
