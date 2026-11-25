"""Git host/scheme allowlist (WS-GIT-009).

Unlike app/core/egress_policy.py's deny-list (blocks loopback/link-local for
already-scoped DB connection probes), this is a strict ALLOWLIST: a git
clone URL is server-side content the workspace pod fetches on the caller's
behalf from wherever `repo_url` says, so an unrestricted scheme/host is a
direct SSRF/egress primitive — point a "repo_url" at file://, an internal
admin service, or any host reachable from this pod and gitpython will
happily try to interact with it. Only https:// to an explicitly allowed
host is permitted; a self-hosted GitLab/Bitbucket/Azure DevOps instance has
to be added to GIT_ALLOWED_HOSTS by whoever operates this deployment, not
asserted by whoever calls the API.
"""
from __future__ import annotations

from urllib.parse import urlparse

from app.core.config import get_settings


class GitEgressPolicyError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def assert_repo_url_allowed(repo_url: str) -> None:
    """Raise GitEgressPolicyError unless repo_url is an https:// URL whose
    host is on (or a subdomain of) this deployment's allowlist."""
    parsed = urlparse(repo_url)

    if parsed.scheme != "https":
        raise GitEgressPolicyError(
            f"scheme '{parsed.scheme or '(none)'}' is not permitted — only https:// clone URLs are allowed"
        )
    if not parsed.hostname:
        raise GitEgressPolicyError("no host in repo URL")

    host = parsed.hostname.lower()
    allowed = get_settings().git_allowed_hosts
    if not any(host == h or host.endswith(f".{h}") for h in allowed):
        raise GitEgressPolicyError(
            f"host '{host}' is not on the allowed Git host list for this deployment"
        )


# ── Per-provider host binding (WS-GIT-009) ──────────────────────────────
# The global allowlist alone lets a binding named "github" point at any
# allowed host (say, a self-hosted GitLab) — and the provider's token would
# then be sent there. Each provider is pinned to the hosts it actually lives
# on. Entries match exactly; entries starting with "." match subdomains.
_PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "github": ("github.com", ".github.com"),
    "gitlab": ("gitlab.com",),
    "bitbucket": ("bitbucket.org",),
    "azure_devops": ("dev.azure.com", ".visualstudio.com"),
}


def _provider_hosts(provider: str) -> tuple[str, ...]:
    hosts = _PROVIDER_HOSTS.get(provider)
    if hosts is None:
        raise GitEgressPolicyError(f"unknown Git provider '{provider}'")
    if provider == "gitlab":
        extra = getattr(get_settings(), "git_gitlab_hosts", None) or []
        hosts = hosts + tuple(h.lower() for h in extra)
    return hosts


def assert_provider_host(provider: str, repo_url: str) -> None:
    """Raise GitEgressPolicyError unless repo_url's host belongs to `provider`."""
    host = (urlparse(repo_url).hostname or "").lower()
    if not host:
        raise GitEgressPolicyError("no host in repo URL")
    for h in _provider_hosts(provider):
        if (h.startswith(".") and host.endswith(h)) or host == h:
            return
    raise GitEgressPolicyError(
        f"host '{host}' is not a {provider} host — a {provider} binding can't point at it"
    )


def assert_binding_url_allowed(provider: str, repo_url: str) -> None:
    """Full check for a repo URL a binding will talk to: https + global
    allowlist + the provider's own hosts. Run on bind AND before every
    operation that reaches the remote, so bindings created before these
    checks existed (or before the allowlist was tightened) are re-validated."""
    assert_repo_url_allowed(repo_url)
    assert_provider_host(provider, repo_url)
