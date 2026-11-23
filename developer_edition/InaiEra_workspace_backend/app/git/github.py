"""GitHub provider."""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import httpx

from app.git.base import GitProvider, PullRequest, Repo, TokenPair


class GitHubProvider:
    name = "github"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        webhook_secret: str | None,
        api_base: str = "https://api.github.com",
        oauth_authorize_endpoint: str = "https://github.com/login/oauth/authorize",
        oauth_token_endpoint: str = "https://github.com/login/oauth/access_token",
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_secret = webhook_secret
        self._api_base = api_base
        self._authorize = oauth_authorize_endpoint
        self._token_endpoint = oauth_token_endpoint

    def oauth_authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None,
    ) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": "repo read:user user:email",
            "state": state,
        }
        # GitHub's classic OAuth Apps don't document PKCE support the way
        # GitLab/Bitbucket do, but per RFC 6749 an authorization server must
        # ignore parameters it doesn't recognize — sending it costs nothing
        # and is what WS-GIT-009 asks for uniformly across providers.
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self._authorize}?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, redirect_uri: str, code_verifier: str | None = None,
    ) -> TokenPair:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self._token_endpoint,
                headers={"Accept": "application/json"},
                data=data,
            )
            resp.raise_for_status()
            data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"GitHub OAuth exchange failed: {data}")
        return TokenPair(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
        )

    async def refresh_token(self, refresh_token: str) -> TokenPair:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self._token_endpoint,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return TokenPair(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
        )

    async def list_repos(self, access_token: str) -> list[Repo]:
        repos: list[Repo] = []
        page = 1
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                resp = await client.get(
                    f"{self._api_base}/user/repos",
                    headers=self._headers(access_token),
                    params={"per_page": 100, "page": page, "sort": "updated"},
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for r in batch:
                    repos.append(Repo(
                        id=str(r["id"]),
                        name=r["name"],
                        full_name=r["full_name"],
                        default_branch=r["default_branch"],
                        clone_url=r["clone_url"],
                        private=r["private"],
                    ))
                if len(batch) < 100:
                    break
                page += 1
        return repos

    async def create_pull_request(
        self,
        access_token: str,
        repo_full_name: str,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
    ) -> PullRequest:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._api_base}/repos/{repo_full_name}/pulls",
                headers=self._headers(access_token),
                json={
                    "title": title,
                    "body": body,
                    "head": source_branch,
                    "base": target_branch,
                },
            )
            resp.raise_for_status()
            pr = resp.json()
        return self._to_pr(pr)

    async def get_pull_request(
        self,
        access_token: str,
        repo_full_name: str,
        pr_number: int,
    ) -> PullRequest:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._api_base}/repos/{repo_full_name}/pulls/{pr_number}",
                headers=self._headers(access_token),
            )
            resp.raise_for_status()
            pr = resp.json()
        return self._to_pr(pr)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            return False  # no secret configured → webhooks disabled, never accepted
        sig = _header(headers, "X-Hub-Signature-256")
        if not sig.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _to_pr(pr: dict) -> PullRequest:
        state = pr["state"]
        if pr.get("merged"):
            state = "merged"
        return PullRequest(
            id=str(pr["id"]),
            number=pr["number"],
            url=pr["html_url"],
            state=state,
            source_branch=pr["head"]["ref"],
            target_branch=pr["base"]["ref"],
            title=pr["title"],
        )


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup (Starlette lower-cases header names)."""
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return ""
