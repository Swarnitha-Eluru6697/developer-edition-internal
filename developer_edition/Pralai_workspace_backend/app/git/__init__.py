"""Git provider factory + local working-copy ops."""
from __future__ import annotations

import os

import structlog

from app.core.config import get_settings
from app.core.git_egress_policy import assert_provider_host, assert_repo_url_allowed
from app.git.azure_devops import AzureDevOpsProvider
from app.git.base import GitProvider
from app.git.bitbucket import BitbucketProvider
from app.git.github import GitHubProvider
from app.git.gitlab import GitLabProvider

log = structlog.get_logger()

# The well-known placeholder. Accepted ONLY when environment == "dev"; in
# staging/prod a missing (or placeholder) secret disables webhook acceptance
# for that provider — verify_webhook then always returns False — rather than
# letting anyone who knows "dev-secret" forge events.
_DEV_WEBHOOK_SECRET = "dev-secret"

_providers: dict[str, GitProvider] | None = None


SUPPORTED_PROVIDERS = ("github", "gitlab", "bitbucket", "azure_devops")


def oauth_available(name: str) -> bool:
    """True when this deployment has an OAuth app configured for `name`.
    Providers are only registered when their client id/secret are set, and
    Azure DevOps additionally needs an Entra ID tenant for real OAuth."""
    global _providers
    if _providers is None:
        _providers = _init_providers()
    provider = _providers.get(name)
    if provider is None:
        return False
    return bool(getattr(provider, "oauth_configured", True))


def get_provider(name: str, instance_url: str | None = None) -> GitProvider:
    global _providers
    if _providers is None:
        _providers = _init_providers()
    if name == "gitlab" and instance_url and instance_url != "https://gitlab.com":
        # Self-hosted GitLab: build a fresh provider per instance. Its OAuth
        # token exchange POSTs GITLAB_CLIENT_SECRET to instance_url, so the
        # host must be on the operator's allowlist (WS-GIT-009) — never just
        # whatever the caller asked for — and must be one of the configured
        # GitLab hosts (git_gitlab_hosts), not merely some allowed host.
        # Raises GitEgressPolicyError.
        assert_repo_url_allowed(instance_url)
        assert_provider_host("gitlab", instance_url)
        return GitLabProvider(
            client_id=os.environ["GITLAB_CLIENT_ID"],
            client_secret=os.environ["GITLAB_CLIENT_SECRET"],
            webhook_secret=_webhook_secret("GITLAB_WEBHOOK_SECRET"),
            instance_url=instance_url,
        )
    if name not in _providers:
        raise ValueError(f"Unknown Git provider: {name}")
    return _providers[name]


def _init_providers() -> dict[str, GitProvider]:
    providers: dict[str, GitProvider] = {}
    # GitHub
    if os.environ.get("GITHUB_CLIENT_ID"):
        providers["github"] = GitHubProvider(
            client_id=os.environ["GITHUB_CLIENT_ID"],
            client_secret=os.environ["GITHUB_CLIENT_SECRET"],
            webhook_secret=_webhook_secret("GITHUB_WEBHOOK_SECRET"),
        )
    # GitLab (SaaS)
    if os.environ.get("GITLAB_CLIENT_ID"):
        providers["gitlab"] = GitLabProvider(
            client_id=os.environ["GITLAB_CLIENT_ID"],
            client_secret=os.environ["GITLAB_CLIENT_SECRET"],
            webhook_secret=_webhook_secret("GITLAB_WEBHOOK_SECRET"),
        )
    # Bitbucket Cloud
    if os.environ.get("BITBUCKET_CLIENT_ID"):
        providers["bitbucket"] = BitbucketProvider(
            client_id=os.environ["BITBUCKET_CLIENT_ID"],
            client_secret=os.environ["BITBUCKET_CLIENT_SECRET"],
            webhook_secret=_webhook_secret("BITBUCKET_WEBHOOK_SECRET"),
        )
    # Azure DevOps. AZURE_DEVOPS_* is the Entra ID OAuth app registration;
    # the older ADO_* names still work for a PAT-only setup.
    ado_client_id = os.environ.get("AZURE_DEVOPS_CLIENT_ID") or os.environ.get("ADO_CLIENT_ID")
    if ado_client_id:
        ado_kwargs = dict(
            client_id=ado_client_id,
            client_secret=(
                os.environ.get("AZURE_DEVOPS_CLIENT_SECRET") or os.environ.get("ADO_CLIENT_SECRET", "")
            ),
            webhook_basic_user=os.environ.get("ADO_WEBHOOK_USER", "webhook"),
            webhook_basic_pass=_webhook_secret("ADO_WEBHOOK_PASS"),
            organization=os.environ.get("ADO_ORGANIZATION"),
        )
        try:
            providers["azure_devops"] = AzureDevOpsProvider(
                tenant_id=os.environ.get("AZURE_DEVOPS_TENANT_ID") or None, **ado_kwargs,
            )
        except ValueError as e:
            # Bad tenant id: keep the PAT path working, OAuth stays disabled.
            log.error("azure_devops_oauth_disabled", reason=str(e))
            providers["azure_devops"] = AzureDevOpsProvider(tenant_id=None, **ado_kwargs)
    return providers


def _webhook_secret(env_name: str) -> str | None:
    """The configured webhook secret, or None (= reject all webhooks).

    Outside dev, the known placeholder is never used: neither as a fallback
    for a missing secret nor when someone sets it explicitly."""
    value = os.environ.get(env_name) or None
    if get_settings().environment == "dev":
        return value or _DEV_WEBHOOK_SECRET
    if value is None or value == _DEV_WEBHOOK_SECRET:
        log.warning(
            "git_webhook_secret_missing", env_var=env_name,
            effect="webhooks for this provider will be rejected",
        )
        return None
    return value
