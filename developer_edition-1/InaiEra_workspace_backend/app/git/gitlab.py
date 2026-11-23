"""GitLab provider."""
from __future__ import annotations

import hmac
from urllib.parse import quote, urlencode

import httpx

from app.git.base import GitProvider, PullRequest, Repo, TokenPair


class GitLabProvider:
    name = "gitlab"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        webhook_secret: str | None,
        instance_url: str = "https://gitlab.com",
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_secret = webhook_secret
        self._instance = instance_url.rstrip("/")

    @property
    def api_base(self) -> str:
        return f"{self._instance}/api/v4"

    def oauth_authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None,
    ) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "api read_user read_repository write_repository",
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self._instance}/oauth/authorize?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, redirect_uri: str, code_verifier: str | None = None,
    ) -> TokenPair:
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._instance}/oauth/token",
                data=data,
            )
            resp.raise_for_status()
            data = resp.json()
        return TokenPair(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
        )

    async def refresh_token(self, refresh_token: str) -> TokenPair:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._instance}/oauth/token",
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
                    f"{self.api_base}/projects",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={
                        "membership": "true", "per_page": 100, "page": page,
                        "order_by": "last_activity_at",
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for r in batch:
                    repos.append(Repo(
                        id=str(r["id"]),
                        name=r["name"],
                        full_name=r["path_with_namespace"],
                        default_branch=r.get("default_branch") or "main",
                        clone_url=r["http_url_to_repo"],
                        private=r["visibility"] != "public",
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
        project_id = quote(repo_full_name, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.api_base}/projects/{project_id}/merge_requests",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "title": title,
                    "description": body,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                },
            )
            resp.raise_for_status()
            mr = resp.json()
        return self._to_pr(mr, repo_full_name)

    async def get_pull_request(
        self,
        access_token: str,
        repo_full_name: str,
        pr_number: int,
    ) -> PullRequest:
        project_id = quote(repo_full_name, safe="")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.api_base}/projects/{project_id}/merge_requests/{pr_number}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            mr = resp.json()
        return self._to_pr(mr, repo_full_name)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            return False  # no secret configured → webhooks disabled, never accepted
        token = next((v for k, v in headers.items() if k.lower() == "x-gitlab-token"), "")
        return hmac.compare_digest(token.encode(), self._webhook_secret.encode())

    @staticmethod
    def _to_pr(mr: dict, repo_full_name: str) -> PullRequest:
        state_map = {"opened": "open", "closed": "closed", "merged": "merged", "locked": "closed"}
        return PullRequest(
            id=str(mr["id"]),
            number=mr["iid"],
            url=mr["web_url"],
            state=state_map.get(mr["state"], "closed"),
            source_branch=mr["source_branch"],
            target_branch=mr["target_branch"],
            title=mr["title"],
        )
