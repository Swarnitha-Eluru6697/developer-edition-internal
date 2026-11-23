"""Git credentials: a project names one (`credential_ref`); the secret itself never lives in its URL (DBT-009).

A `git_url` with user info (`https://user:token@host/repo.git`) used to be accepted, stored, returned in API output, logged and copied
into each run's Secret. Now the runner refuses it at the boundary: a project carries a clean, canonical repository URL plus an optional
`credential_ref` — a short name of a credential the OPERATOR provisioned for the runner as an environment variable fed from a Secret:

    GIT_CREDENTIAL_<REF>=<token>          e.g.  GIT_CREDENTIAL_ACME_GITHUB   <-   credential_ref "acme_github"

The token is given to the git subprocess as an `Authorization` header scoped to the repository's own host (`http.<scheme>://<host>/.extraHeader`
through git's env-supplied config), never in a URL, a command line, `.git/config` or a log line; redirects to other hosts are not followed with it.

SUPPORTED AUTHENTICATION (DG-010) — exactly two mechanisms, and nothing else is interpreted by the runner:

  1. HTTPS + a per-project `credential_ref` (above). The only credential the runner adds to a git call.
  2. Everything else — ssh remotes, a credential helper, GIT_ASKPASS, a proxy, a private CA — is the POD'S OWN environment, which the operator
     provides (SSH_AUTH_SOCK / GIT_SSH_COMMAND / ~/.ssh, git config, HTTPS_PROXY, GIT_SSL_CAINFO ...). The runner does not manage ssh keys.

Every git subprocess INHERITS the process environment and only ADDS `git_env()` on top (`{**os.environ, **git_env(...)}`; GitPython merges its
`env=` / `custom_environment` the same way): HOME, proxy variables, CA settings, SSH_AUTH_SOCK and GIT_ASKPASS are never dropped.
`GIT_TERMINAL_PROMPT=0` is added so a missing credential fails fast instead of waiting for a terminal that does not exist. See
tests/test_git_environment.py.
"""
from __future__ import annotations

import base64
import os
import re
from urllib.parse import urlsplit

ENV_PREFIX = "GIT_CREDENTIAL_"
REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def has_userinfo(url: str) -> bool:
    """True when the URL carries a secret-capable user info: any user/password on http(s) (`https://user:token@host/x`, `https://token@host/x`),
    or a password on any other scheme. A bare user name on ssh (`ssh://git@host/x`) is an account name, not a secret, and stays allowed."""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc or ""
        if parts.scheme.lower() in ("http", "https"):
            return parts.username is not None or parts.password is not None or "@" in netloc
        return parts.password is not None
    except ValueError:
        return True                                   # unparsable: treat as unsafe rather than guess


def assert_clean_url(url: str) -> str:
    if has_userinfo(url):
        raise ValueError("git_url must not contain credentials (user:token@host): register the plain repository URL and "
                         "name a credential with credential_ref")
    return url


def env_name(ref: str) -> str:
    return ENV_PREFIX + ref.upper()


def known_refs() -> list[str]:
    return sorted(k[len(ENV_PREFIX):].lower() for k, v in os.environ.items() if k.startswith(ENV_PREFIX) and v)


def token_for(ref: str | None) -> str | None:
    """The token behind `ref`, or None (no ref, or the operator has not provisioned it)."""
    if not ref or not REF_PATTERN.match(ref):
        return None
    return os.environ.get(env_name(ref)) or None


def git_env(url: str, token: str | None) -> dict[str, str]:
    """Environment for a git subprocess: no prompts, and — when there is a token — the Authorization header for this URL's host only."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if not token:
        return env
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return env                                    # ssh and friends authenticate another way
    host = f"{parts.scheme}://{parts.netloc}/"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env.update({"GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"http.{host}.extraHeader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
                "GIT_CONFIG_KEY_1": "http.followRedirects", "GIT_CONFIG_VALUE_1": "false"})
    return env
