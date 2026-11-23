"""GitProvider abstraction.

Three concrete implementations: github.py, gitlab.py, azure_devops.py.
All Git provider interactions in the codebase go through this protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Repo:
    id: str
    name: str
    full_name: str
    default_branch: str
    clone_url: str
    private: bool


@dataclass(frozen=True)
class PullRequest:
    id: str
    number: int
    url: str
    state: str  # 'open' | 'closed' | 'merged'
    source_branch: str
    target_branch: str
    title: str


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str | None
    expires_in: int | None


class GitProvider(Protocol):
    name: str  # 'github' | 'gitlab' | 'azure_devops'

    async def list_repos(self, access_token: str) -> list[Repo]:
        ...

    async def create_pull_request(
        self,
        access_token: str,
        repo_full_name: str,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
    ) -> PullRequest:
        ...

    async def get_pull_request(
        self,
        access_token: str,
        repo_full_name: str,
        pr_number: int,
    ) -> PullRequest:
        ...

    async def refresh_token(self, refresh_token: str) -> TokenPair:
        ...

    def oauth_authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None,
    ) -> str:
        ...

    async def exchange_code_for_token(
        self, code: str, redirect_uri: str, code_verifier: str | None = None,
    ) -> TokenPair:
        ...

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        ...
