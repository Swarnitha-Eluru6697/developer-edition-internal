"""Git connections: commit an agent's YAML to GitHub, GitLab, Bitbucket or Azure DevOps.

A GitConnection row is one account on one provider, authenticated with a
personal access token (or, on Bitbucket, an app password / API token or a
repository/workspace access token; on Azure DevOps, a PAT scoped to one
organization). It exists so that an agent exported
from this platform also lands in version control — the YAML a person
downloads is the same file committed to their repo.

Commits go through each provider's file API rather than a local git
checkout: one file per commit needs no clone, no git binary in the
container, and no working copy left behind holding a token.

Rules this module keeps (the same ones the workspace's Git integration
keeps):

  * The token is encrypted at rest and never returned — reads mask it.
  * A token is checked against the provider before it is saved, and the
    account it belongs to is recorded, so "connected" is a fact.
  * Every outbound call goes through egress.safe_http_request: no private
    or link-local hosts, no cross-origin redirects carrying the token.
  * Provider errors come back as a sentence about what to fix (bad token,
    missing scope, repo not visible), never as a raw 500, and never with
    the token in them.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import yaml
from sqlmodel import Session

from . import egress
from .models import GitConnection
from .secrets_crypto import decrypt_secret, encrypt_secret, require_durable_key

MASK = "••••••••"
PROVIDERS = ("github", "gitlab", "bitbucket", "azure_devops")
PROVIDER_LABELS = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket",
                   "azure_devops": "Azure DevOps"}
# Oldest REST version every API used here has: Azure DevOps Server 2020
# answers 6.0, so pinning it keeps on-prem collections working too.
_ADO_API_VERSION = "6.0"
DEFAULT_FOLDER = "agents"
_TIMEOUT = 15.0
_MAX_PAGES = 5


class GitIntegrationError(RuntimeError):
    """A provider call failed in a way the person can act on.

    status is the HTTP status this platform should answer with: 400 for
    something wrong with what was entered (token, repo, branch), 502 when
    the provider itself could not be reached."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- hosts

def normalize_base_url(provider: str, base_url: Optional[str]) -> Optional[str]:
    """Self-hosted instance root (GitHub Enterprise, self-managed GitLab),
    or None for the provider's public cloud. https only; no credentials,
    paths beyond the root, queries or fragments."""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        if provider == "azure_devops":
            # A PAT belongs to one organization, and nothing else names it.
            raise GitIntegrationError(
                "Enter the organization URL, e.g. https://dev.azure.com/your-org "
                "(or your Azure DevOps Server collection URL)."
            )
        return None
    if provider == "bitbucket":
        raise GitIntegrationError(
            "Only Bitbucket Cloud (bitbucket.org) is supported — leave the server URL empty."
        )
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise GitIntegrationError("The server URL must start with https:// and name a host.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GitIntegrationError("The server URL must not contain credentials, a query or a fragment.")
    host = parsed.hostname.lower()
    if provider == "azure_devops":
        # The organization is the first path segment on dev.azure.com; on a
        # legacy *.visualstudio.com host or a Server collection the URL as
        # given is the root. Either way it must name more than a bare host.
        if host == "dev.azure.com" and not parsed.path.strip("/"):
            raise GitIntegrationError("Add your organization to the URL: https://dev.azure.com/your-org.")
        if host == "dev.azure.com":
            return f"https://dev.azure.com/{parsed.path.strip('/').split('/')[0]}"
        return f"https://{parsed.netloc}{parsed.path.rstrip('/')}"
    if host in ("github.com", "api.github.com", "gitlab.com"):
        return None
    return f"https://{parsed.netloc}{parsed.path.rstrip('/')}"


def api_base(provider: str, base_url: Optional[str]) -> str:
    if provider == "github":
        return f"{base_url}/api/v3" if base_url else "https://api.github.com"
    if provider == "gitlab":
        return f"{base_url or 'https://gitlab.com'}/api/v4"
    if provider == "azure_devops":
        return base_url or ""
    return "https://api.bitbucket.org/2.0"


def web_base(provider: str, base_url: Optional[str]) -> str:
    return base_url or {"github": "https://github.com", "gitlab": "https://gitlab.com",
                        "bitbucket": "https://bitbucket.org", "azure_devops": "https://dev.azure.com"}[provider]


def _auth_headers(provider: str, token: str, username: Optional[str]) -> Dict[str, str]:
    if provider == "github":
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}
    if provider == "gitlab":
        return {"PRIVATE-TOKEN": token}
    if provider == "azure_devops":
        # A PAT goes as Basic auth with an empty username.
        return {"Authorization": f"Basic {_ado_basic(token)}", "Accept": "application/json"}
    # Bitbucket: an app password or Atlassian API token goes with the
    # account's username/email as Basic auth; a repository/workspace
    # access token goes alone as Bearer.
    if username:
        basic = base64.b64encode(f"{username}:{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}
    return {"Authorization": f"Bearer {token}"}


def _ado_basic(token: str) -> str:
    return base64.b64encode(f":{token}".encode()).decode()


# --------------------------------------------------------------------------- HTTP

class _Client:
    def __init__(self, provider: str, token: str, *, base_url: Optional[str] = None,
                 username: Optional[str] = None):
        if provider not in PROVIDERS:
            raise GitIntegrationError(
                f"Unknown provider '{provider}'. Use github, gitlab, bitbucket or azure_devops."
            )
        if not (token or "").strip():
            raise GitIntegrationError("A personal access token is required.")
        self.provider = provider
        self.token = token.strip()
        self.username = (username or "").strip() or None
        self.base_url = normalize_base_url(provider, base_url)
        self.api = api_base(provider, self.base_url)
        self.label = PROVIDER_LABELS[provider]

    def _scrub(self, text: str) -> str:
        text = (text or "").replace(self.token, MASK)
        if self.username:
            basic = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
            text = text.replace(basic, MASK)
        if self.provider == "azure_devops":
            text = text.replace(_ado_basic(self.token), MASK)
        return text[:400]

    def call(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Any = None,
             data: Any = None, ok: Tuple[int, ...] = (200, 201), what: str = "") -> Dict[str, Any]:
        url = path if path.startswith("https://") else f"{self.api}{path}"
        headers = _auth_headers(self.provider, self.token, self.username)
        try:
            resp = egress.safe_http_request(method, url, headers=headers, params=params, json=json_body,
                                            data=data, timeout=_TIMEOUT)
        except egress.SSRFBlocked as e:
            raise GitIntegrationError(f"{self.label} host refused by the egress policy: {e}") from None
        except Exception as e:  # network / TLS / timeout
            raise GitIntegrationError(
                f"Couldn't reach {self.label} at {urlparse(url).hostname}: {self._scrub(str(e))}", 502
            ) from None
        status = resp["status_code"]
        # Azure DevOps answers a rejected PAT with 203 and its sign-in page
        # rather than 401; anything else would read as a JSON parse error.
        if self.provider == "azure_devops" and status == 203:
            status = 401
        if status in ok:
            return resp
        raise self._error(status, resp.get("text") or "", what)

    def _error(self, status: int, text: str, what: str) -> GitIntegrationError:
        detail = ""
        try:
            body = json.loads(text)
            if isinstance(body, dict):
                err = body.get("error")
                detail = (body.get("message") or (err.get("message") if isinstance(err, dict) else err)
                          or body.get("error_description") or "")
        except ValueError:
            pass
        detail = self._scrub(str(detail or "")).strip()
        suffix = f" ({detail})" if detail else ""
        target = f" for {what}" if what else ""
        if status == 401:
            return GitIntegrationError(f"{self.label} rejected the token — it's invalid, expired or revoked{suffix}.")
        if status == 403:
            return GitIntegrationError(
                f"The token isn't allowed to do this{target} — check its scopes{suffix}. {SCOPE_HINT[self.provider]}"
            )
        if status == 404:
            return GitIntegrationError(
                f"{self.label} couldn't find {what or 'that resource'}, or this token can't see it{suffix}."
            )
        if status in (409, 422):
            return GitIntegrationError(f"{self.label} refused the change{target}{suffix}.", 409)
        if status == 429:
            return GitIntegrationError(f"{self.label} rate limit reached — try again in a minute.", 429)
        return GitIntegrationError(f"{self.label} answered HTTP {status}{target}{suffix}.", 502)

    def json(self, *a, **kw) -> Any:
        r = self.call(*a, **kw)
        try:
            return json.loads(r["text"] or "null")
        except ValueError:
            raise GitIntegrationError(f"{self.label} sent a response that isn't JSON.", 502) from None


SCOPE_HINT = {
    "github": "Classic tokens need the 'repo' scope; fine-grained tokens need Contents: Read and write on the repository.",
    "gitlab": "The token needs the 'api' scope (or 'read_api' + 'write_repository') and Developer access to the project.",
    "bitbucket": "App passwords / API tokens need Repositories: Read and Write; access tokens need repository:write.",
    "azure_devops": "The PAT needs the Code: Read & write scope, created in the same organization as the URL.",
}


def _enc(repo: str) -> str:
    return quote(repo, safe="")


def _repo_path(repo: str) -> str:
    """owner/name for GitHub and Bitbucket, with each part URL-quoted."""
    parts = [p for p in (repo or "").strip().strip("/").split("/") if p]
    if len(parts) < 2:
        raise GitIntegrationError("Name the repository as owner/repository.")
    return "/".join(quote(p, safe="") for p in parts)


def _ado_repo(repo: str) -> Tuple[str, str]:
    """project/repository for Azure DevOps, each part URL-quoted."""
    parts = [p for p in (repo or "").strip().strip("/").split("/") if p]
    if len(parts) != 2:
        raise GitIntegrationError("Name the repository as project/repository.")
    return quote(parts[0], safe=""), quote(parts[1], safe="")


def _ado_get(client: _Client, path: str, *, params: Optional[dict] = None, what: str = "", **kw) -> Any:
    return client.json("GET", path, params={**(params or {}), "api-version": _ADO_API_VERSION}, what=what, **kw)


# --------------------------------------------------------------------------- operations

def whoami(client: _Client) -> str:
    """The account the token belongs to — proves the token works."""
    p = client.provider
    if p == "github":
        return client.json("GET", "/user", what="the token's account").get("login") or "unknown"
    if p == "gitlab":
        return client.json("GET", "/user", what="the token's account").get("username") or "unknown"
    if p == "azure_devops":
        # connectionData answers on dev.azure.com and on Server alike, and an
        # anonymous caller gets no authenticatedUser back.
        data = client.json("GET", "/_apis/connectionData", what="the organization")
        user = data.get("authenticatedUser") or {}
        name = user.get("providerDisplayName") or ""
        if not name or name.lower() == "anonymous":
            raise GitIntegrationError("Azure DevOps didn't accept the token for this organization — check the URL and the PAT.")
        return name
    try:
        u = client.json("GET", "/user", what="the token's account")
        return u.get("username") or u.get("nickname") or u.get("display_name") or "unknown"
    except GitIntegrationError:
        if client.username:
            raise
        # Repository/workspace access tokens can't read /user; listing what
        # they can reach is the check they do pass.
        client.json("GET", "/repositories", params={"role": "member", "pagelen": 1}, what="repositories")
        return "access token"


def list_repos(client: _Client) -> List[Dict[str, Any]]:
    p, out = client.provider, []
    if p == "github":
        for page in range(1, _MAX_PAGES + 1):
            batch = client.json("GET", "/user/repos", params={"per_page": 100, "page": page, "sort": "updated"},
                                what="repositories")
            out += [{"full_name": r["full_name"], "default_branch": r.get("default_branch") or "main",
                     "private": bool(r.get("private")), "url": r.get("html_url")} for r in batch]
            if len(batch) < 100:
                break
    elif p == "gitlab":
        for page in range(1, _MAX_PAGES + 1):
            batch = client.json("GET", "/projects", params={"membership": "true", "simple": "true", "per_page": 100,
                                                            "page": page, "order_by": "last_activity_at"},
                                what="projects")
            out += [{"full_name": r["path_with_namespace"], "default_branch": r.get("default_branch") or "main",
                     "private": r.get("visibility") != "public", "url": r.get("web_url")} for r in batch]
            if len(batch) < 100:
                break
    elif p == "azure_devops":
        body = _ado_get(client, "/_apis/git/repositories", what="repositories")
        for r in body.get("value", []):
            proj = r.get("project") or {}
            out.append({"full_name": f"{proj.get('name')}/{r.get('name')}",
                        "default_branch": (r.get("defaultBranch") or "refs/heads/main").removeprefix("refs/heads/"),
                        "private": proj.get("visibility") != "public", "url": r.get("webUrl")})
    else:
        url: Optional[str] = "/repositories"
        params: Optional[dict] = {"role": "member", "pagelen": 100, "sort": "-updated_on"}
        for _ in range(_MAX_PAGES):
            body = client.json("GET", url, params=params, what="repositories")
            out += [{"full_name": r["full_name"], "default_branch": (r.get("mainbranch") or {}).get("name") or "main",
                     "private": bool(r.get("is_private")), "url": ((r.get("links") or {}).get("html") or {}).get("href")}
                    for r in body.get("values", [])]
            url, params = body.get("next"), None
            if not url:
                break
            if urlparse(url).hostname != "api.bitbucket.org":
                break
    return out


def list_branches(client: _Client, repo: str) -> List[str]:
    p, names = client.provider, []
    what = f"repository '{repo}'"
    if p == "github":
        for page in range(1, _MAX_PAGES + 1):
            batch = client.json("GET", f"/repos/{_repo_path(repo)}/branches", params={"per_page": 100, "page": page},
                                what=what)
            names += [b["name"] for b in batch]
            if len(batch) < 100:
                break
    elif p == "gitlab":
        for page in range(1, _MAX_PAGES + 1):
            batch = client.json("GET", f"/projects/{_enc(repo)}/repository/branches",
                                params={"per_page": 100, "page": page}, what=what)
            names += [b["name"] for b in batch]
            if len(batch) < 100:
                break
    elif p == "azure_devops":
        proj, name = _ado_repo(repo)
        body = _ado_get(client, f"/{proj}/_apis/git/repositories/{name}/refs", params={"filter": "heads/"},
                        what=what)
        names = [r["name"].removeprefix("refs/heads/") for r in body.get("value", [])]
    else:
        body = client.json("GET", f"/repositories/{_repo_path(repo)}/refs/branches", params={"pagelen": 100},
                           what=what)
        names = [b["name"] for b in body.get("values", [])]
    return names


def clean_path(path: str) -> str:
    p = (path or "").strip().replace("\\", "/").strip("/")
    if not p or any(seg in ("", ".", "..") for seg in p.split("/")):
        raise GitIntegrationError("Give a file path inside the repository, e.g. agents/my-agent.yaml.")
    if not re.search(r"\.ya?ml$", p, re.I):
        raise GitIntegrationError("The file path must end in .yaml or .yml.")
    return p


def commit_file(client: _Client, repo: str, branch: str, path: str, content: str,
                message: str) -> Dict[str, Any]:
    """Create or update one file on one branch as a single commit."""
    path = clean_path(path)
    branch = (branch or "").strip()
    if not branch:
        raise GitIntegrationError("Choose a branch to commit to.")
    message = (message or "").strip() or f"Update {path}"
    p = client.provider
    what = f"'{path}' on {repo}@{branch}"
    if p == "github":
        rp = _repo_path(repo)
        fp = quote(path, safe="/")
        existing = client.call("GET", f"/repos/{rp}/contents/{fp}", params={"ref": branch}, ok=(200, 404),
                               what=f"repository '{repo}' branch '{branch}'")
        sha = json.loads(existing["text"]).get("sha") if existing["status_code"] == 200 else None
        body = {"message": message, "branch": branch,
                "content": base64.b64encode(content.encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        r = client.json("PUT", f"/repos/{rp}/contents/{fp}", json_body=body, what=what)
        return {"created": not sha, "commit_sha": (r.get("commit") or {}).get("sha"),
                "commit_url": (r.get("commit") or {}).get("html_url"),
                "file_url": (r.get("content") or {}).get("html_url")}
    if p == "gitlab":
        proj = _enc(repo)
        existing = client.call("GET", f"/projects/{proj}/repository/files/{_enc(path)}", params={"ref": branch},
                               ok=(200, 404), what=f"project '{repo}' branch '{branch}'")
        exists = existing["status_code"] == 200
        r = client.json("POST", f"/projects/{proj}/repository/commits", what=what, json_body={
            "branch": branch, "commit_message": message,
            "actions": [{"action": "update" if exists else "create", "file_path": path, "content": content}],
        })
        web = web_base("gitlab", client.base_url)
        return {"created": not exists, "commit_sha": r.get("id"), "commit_url": r.get("web_url"),
                "file_url": f"{web}/{repo}/-/blob/{quote(branch, safe='')}/{quote(path, safe='/')}"}
    if p == "azure_devops":
        proj, name = _ado_repo(repo)
        base = f"/{proj}/_apis/git/repositories/{name}"
        ver = {"api-version": _ADO_API_VERSION}
        refs = _ado_get(client, f"{base}/refs", params={"filter": f"heads/{branch}"},
                        what=f"repository '{repo}' branch '{branch}'")
        head = next((r for r in refs.get("value", []) if r.get("name") == f"refs/heads/{branch}"), None)
        if not head:
            raise GitIntegrationError(f"Azure DevOps has no branch '{branch}' in '{repo}'.")
        existing = client.call("GET", f"{base}/items", ok=(200, 404), what=f"repository '{repo}' branch '{branch}'",
                               params={**ver, "path": f"/{path}", "versionDescriptor.version": branch,
                                       "versionDescriptor.versionType": "branch"})
        exists = existing["status_code"] == 200
        # A push names the branch tip it builds on, so a commit that raced
        # this one is refused rather than overwritten.
        r = client.json("POST", f"{base}/pushes", params=ver, what=what, json_body={
            "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": head["objectId"]}],
            "commits": [{"comment": message, "changes": [{
                "changeType": "edit" if exists else "add", "item": {"path": f"/{path}"},
                "newContent": {"content": content, "contentType": "rawtext"},
            }]}],
        })
        sha = ((r.get("commits") or [{}])[0]).get("commitId")
        web = f"{client.base_url}/{proj}/_git/{name}"
        return {"created": not exists, "commit_sha": sha,
                "commit_url": f"{web}/commit/{sha}" if sha else None,
                "file_url": f"{web}?path=/{quote(path, safe='/')}&version=GB{quote(branch, safe='')}"}
    rp = _repo_path(repo)
    existing = client.call("GET", f"/repositories/{rp}/src/{quote(branch, safe='')}/{quote(path, safe='/')}",
                           ok=(200, 404), what=f"repository '{repo}' branch '{branch}'")
    r = client.call("POST", f"/repositories/{rp}/src", data={path: content, "message": message, "branch": branch},
                    ok=(200, 201), what=what)
    loc = (r.get("headers") or {}).get("location") or ""
    sha = loc.rstrip("/").rsplit("/", 1)[-1] if "/commit/" in loc else None
    web = f"https://bitbucket.org/{repo}"
    return {"created": existing["status_code"] == 404, "commit_sha": sha,
            "commit_url": f"{web}/commits/{sha}" if sha else None,
            "file_url": f"{web}/src/{quote(branch, safe='')}/{quote(path, safe='/')}"}


# --------------------------------------------------------------------------- YAML

def agent_yaml(dsl: Dict[str, Any]) -> str:
    """The agent's DSL as YAML — the same document the JSON export
    produces, so it can be imported back."""
    head = (f"# InaiEra agent: {dsl.get('name') or 'untitled'}\n"
            f"# Exported {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} — DSL v{dsl.get('dsl_version', '1')}\n")
    return head + yaml.safe_dump(dsl, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


def agent_filename(name: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (name or "agent").lower()).strip("-") or "agent") + ".yaml"


# --------------------------------------------------------------------------- rows

def client_for(conn: GitConnection) -> _Client:
    try:
        token = decrypt_secret(conn.token)
    except Exception:
        raise GitIntegrationError(
            f"The stored token for '{conn.name}' can't be decrypted (the encryption key changed). "
            f"Edit the connection and enter the token again.", 409
        ) from None
    return _Client(conn.provider, token or "", base_url=conn.base_url, username=conn.username)


def describe(conn: GitConnection) -> Dict[str, Any]:
    return {
        "id": conn.id, "name": conn.name, "provider": conn.provider,
        "provider_label": PROVIDER_LABELS.get(conn.provider, conn.provider),
        "base_url": conn.base_url, "username": conn.username, "account": conn.account,
        "token": MASK if conn.token else None,
        "default_repo": conn.default_repo, "default_branch": conn.default_branch,
        "default_folder": conn.default_folder or DEFAULT_FOLDER,
        "commit_on_download": bool(conn.commit_on_download),
        "created_at": conn.created_at.isoformat() if conn.created_at else None,
        "last_checked_at": conn.last_checked_at.isoformat() if conn.last_checked_at else None,
        "last_error": conn.last_error,
        "web_url": web_base(conn.provider, conn.base_url),
    }


def create(session: Session, *, name: str, provider: str, token: str, base_url: Optional[str],
           username: Optional[str], account: str, default_repo: Optional[str], default_branch: Optional[str],
           default_folder: Optional[str], commit_on_download: bool) -> GitConnection:
    require_durable_key("a Git access token")
    conn = GitConnection(
        id=f"git_{uuid.uuid4().hex[:12]}", name=(name or "").strip() or f"{PROVIDER_LABELS[provider]} · {account}",
        provider=provider, base_url=normalize_base_url(provider, base_url), username=(username or "").strip() or None,
        token=encrypt_secret(token.strip()), account=account,
        default_repo=(default_repo or "").strip() or None, default_branch=(default_branch or "").strip() or None,
        default_folder=(default_folder or "").strip().strip("/") or DEFAULT_FOLDER,
        commit_on_download=bool(commit_on_download), last_checked_at=datetime.utcnow(), last_error=None,
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return conn
