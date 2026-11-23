"""Bitbucket Cloud provider."""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import httpx

from app.git.base import GitProvider, PullRequest, Repo, TokenPair


class BitbucketProvider:
    name = "bitbucket"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        webhook_secret: str | None,
        api_base: str = "https://api.bitbucket.org/2.0",
        oauth_authorize_endpoint: str = "https://bitbucket.org/site/oauth2/authorize",
        oauth_token_endpoint: str = "https://bitbucket.org/site/oauth2/access_token",
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
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self._authorize}?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, redirect_uri: str, code_verifier: str | None = None,
    ) -> TokenPair:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_endpoint,
                data=data,
                auth=(self._client_id, self._client_secret),
            )
            resp.raise_for_status()
            data = resp.json()
        return TokenPair(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
        )

    async def refresh_token(self, refresh_token: str) -> TokenPair:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_endpoint,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(self._client_id, self._client_secret),
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
        url = f"{self._api_base}/repositories?role=member&pagelen=100"
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"}) as client:
            while url:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                for r in data.get("values", []):
                    clone_url = next(
                        (l["href"] for l in r.get("links", {}).get("clone", []) if l.get("name") == "https"),
                        r.get("links", {}).get("html", {}).get("href", ""),
                    )
                    repos.append(Repo(
                        id=r["uuid"],
                        name=r["name"],
                        full_name=r["full_name"],
                        default_branch=r.get("mainbranch", {}).get("name", "main"),
                        clone_url=clone_url,
                        private=r.get("is_private", True),
                    ))
                url = data.get("next")
        return repos

    async def create_pull_request(
        self, access_token: str, repo_full_name: str,
        source_branch: str, target_branch: str, title: str, body: str,
    ) -> PullRequest:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"}) as client:
            resp = await client.post(
                f"{self._api_base}/repositories/{repo_full_name}/pullrequests",
                json={
                    "title": title, "description": body,
                    "source": {"branch": {"name": source_branch}},
                    "destination": {"branch": {"name": target_branch}},
                },
            )
            resp.raise_for_status()
            pr = resp.json()
        return PullRequest(
            id=str(pr["id"]), number=pr["id"],
            url=pr["links"]["html"]["href"],
            state=pr["state"].lower(),
            source_branch=source_branch, target_branch=target_branch,
            title=title,
        )

    async def get_pull_request(self, access_token: str, repo_full_name: str, pr_number: int) -> PullRequest:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {access_token}"}) as client:
            resp = await client.get(f"{self._api_base}/repositories/{repo_full_name}/pullrequests/{pr_number}")
            resp.raise_for_status()
            pr = resp.json()
        return PullRequest(
            id=str(pr["id"]), number=pr["id"],
            url=pr["links"]["html"]["href"],
            state=pr["state"].lower(),
            source_branch=pr["source"]["branch"]["name"],
            target_branch=pr["destination"]["branch"]["name"],
            title=pr["title"],
        )

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        if not self._webhook_secret:
            return False  # no secret configured → webhooks disabled, never accepted
        sig = next((v for k, v in headers.items() if k.lower() == "x-hub-signature"), "")
        expected = "sha256=" + hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
