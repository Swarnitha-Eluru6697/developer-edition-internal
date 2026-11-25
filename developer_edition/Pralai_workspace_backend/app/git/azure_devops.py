"""Azure DevOps provider.

Notes specific to ADO:
- PR endpoint requires `refs/heads/` prefix on source/target branch names.
- Project and repo are identified by GUIDs; we resolve them from owner/repo paths.
- Personal Access Tokens (PATs) are commonly used in lieu of OAuth for internal tools.
  This implementation supports both.
- OAuth goes through Microsoft Entra ID v2 (the legacy app.vssps.visualstudio.com
  OAuth is deprecated): authorization-code + PKCE (S256) against
  login.microsoftonline.com/{tenant}, requesting the Azure DevOps resource
  (499b84ac-1321-427f-aa17-267ca6975798) plus offline_access for a refresh
  token. Enabled only when tenant id, client id and client secret are all
  configured (AZURE_DEVOPS_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET); otherwise
  the OAuth methods raise NotImplementedError and the API answers with a
  clean validation error pointing at the PAT path.
"""
from __future__ import annotations

import base64
import hmac
import re
from urllib.parse import quote, urlencode

import httpx

from app.git.base import GitProvider, PullRequest, Repo, TokenPair


_TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")


class AzureDevOpsProvider:
    name = "azure_devops"
    api_version = "7.1"

    # Azure DevOps' well-known resource id; ".default" = whatever delegated
    # permissions the app registration was granted for it.
    oauth_scope = "499b84ac-1321-427f-aa17-267ca6975798/.default offline_access"
    entra_authority = "https://login.microsoftonline.com"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        webhook_basic_user: str,
        webhook_basic_pass: str | None,
        organization: str | None = None,
        tenant_id: str | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_basic_user = webhook_basic_user
        self._webhook_basic_pass = webhook_basic_pass
        self._default_org = organization  # for default flows
        if tenant_id is not None and not _TENANT_RE.fullmatch(tenant_id):
            # It's interpolated into the authority URL — a GUID or a domain
            # name only, never a path/host fragment.
            raise ValueError("AZURE_DEVOPS_TENANT_ID must be a tenant GUID or domain name")
        self._tenant_id = tenant_id

    def _api_base(self, organization: str) -> str:
        return f"https://dev.azure.com/{organization}"

    @property
    def oauth_configured(self) -> bool:
        return bool(self._tenant_id and self._client_id and self._client_secret)

    def _require_oauth(self) -> None:
        if not self.oauth_configured:
            raise NotImplementedError(
                "Entra ID OAuth for Azure DevOps is not configured on this deployment "
                "(AZURE_DEVOPS_TENANT_ID, AZURE_DEVOPS_CLIENT_ID, AZURE_DEVOPS_CLIENT_SECRET)"
            )

    def _endpoint(self, kind: str) -> str:
        return f"{self.entra_authority}/{self._tenant_id}/oauth2/v2.0/{kind}"

    def oauth_authorize_url(
        self, state: str, redirect_uri: str, code_challenge: str | None = None,
    ) -> str:
        self._require_oauth()
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": self.oauth_scope,
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self._endpoint('authorize')}?{urlencode(params)}"

    async def exchange_code_for_token(
        self, code: str, redirect_uri: str, code_verifier: str | None = None,
    ) -> TokenPair:
        self._require_oauth()
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": self.oauth_scope,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        return await self._token_request(data)

    async def refresh_token(self, refresh_token: str) -> TokenPair:
        self._require_oauth()
        return await self._token_request({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": self.oauth_scope,
        })

    async def _token_request(self, data: dict) -> TokenPair:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self._endpoint("token"),
                data=data,
                headers={"Accept": "application/json"},
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or "access_token" not in body:
            # Only the OAuth error code — never echo the request (it carries
            # the client secret / code) or a token-bearing response.
            raise RuntimeError(
                f"Entra ID token request failed: HTTP {resp.status_code} "
                f"{body.get('error', 'unknown_error')}"
            )
        return TokenPair(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
        )

    async def list_repos(self, access_token: str) -> list[Repo]:
        # ADO requires an organization to scope this. In practice we resolve
        # the user's accessible organizations first via /_apis/accounts.
        if not self._default_org:
            raise RuntimeError("Azure DevOps organization not configured")
        org = self._default_org
        repos: list[Repo] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            # List all projects in the org
            projects_resp = await client.get(
                f"{self._api_base(org)}/_apis/projects",
                params={"api-version": self.api_version},
                headers=self._headers(access_token),
            )
            projects_resp.raise_for_status()
            for project in projects_resp.json().get("value", []):
                proj_name = project["name"]
                repos_resp = await client.get(
                    f"{self._api_base(org)}/{quote(proj_name)}/_apis/git/repositories",
                    params={"api-version": self.api_version},
                    headers=self._headers(access_token),
                )
                repos_resp.raise_for_status()
                for r in repos_resp.json().get("value", []):
                    repos.append(Repo(
                        id=r["id"],
                        name=r["name"],
                        full_name=f"{org}/{proj_name}/{r['name']}",
                        default_branch=(r.get("defaultBranch") or "refs/heads/main").replace("refs/heads/", ""),
                        clone_url=r["remoteUrl"],
                        private=True,
                    ))
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
        org, project, repo = repo_full_name.split("/", 2)
        url = (
            f"{self._api_base(org)}/{quote(project)}/_apis/git/repositories"
            f"/{quote(repo)}/pullrequests"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={"api-version": self.api_version},
                headers=self._headers(access_token),
                json={
                    "title": title,
                    "description": body,
                    "sourceRefName": f"refs/heads/{source_branch}",
                    "targetRefName": f"refs/heads/{target_branch}",
                },
            )
            resp.raise_for_status()
            pr = resp.json()
        return self._to_pr(pr, repo_full_name)

    async def get_pull_request(
        self,
        access_token: str,
        repo_full_name: str,
        pr_number: int,
    ) -> PullRequest:
        org, project, repo = repo_full_name.split("/", 2)
        url = (
            f"{self._api_base(org)}/{quote(project)}/_apis/git/repositories"
            f"/{quote(repo)}/pullrequests/{pr_number}"
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                params={"api-version": self.api_version},
                headers=self._headers(access_token),
            )
            resp.raise_for_status()
            pr = resp.json()
        return self._to_pr(pr, repo_full_name)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        # ADO webhooks support HTTP Basic on the webhook URL itself.
        if not self._webhook_basic_pass:
            return False  # no secret configured → webhooks disabled, never accepted
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(auth[6:], validate=True).partition(b":")
        except Exception:
            return False
        # Constant-time on both halves; evaluate both so timing doesn't reveal
        # which one was wrong.
        user_ok = hmac.compare_digest(user, self._webhook_basic_user.encode())
        pass_ok = hmac.compare_digest(password, self._webhook_basic_pass.encode())
        return user_ok and pass_ok

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        # PAT: Authorization is Basic with empty username
        # OAuth: Authorization is Bearer
        if ":" in token:
            return {"Authorization": f"Basic {token}"}
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _to_pr(pr: dict, repo_full_name: str) -> PullRequest:
        status_map = {"active": "open", "completed": "merged", "abandoned": "closed"}
        return PullRequest(
            id=str(pr["pullRequestId"]),
            number=pr["pullRequestId"],
            url=pr.get("_links", {}).get("web", {}).get("href", ""),
            state=status_map.get(pr.get("status"), "open"),
            source_branch=pr["sourceRefName"].replace("refs/heads/", ""),
            target_branch=pr["targetRefName"].replace("refs/heads/", ""),
            title=pr["title"],
        )
