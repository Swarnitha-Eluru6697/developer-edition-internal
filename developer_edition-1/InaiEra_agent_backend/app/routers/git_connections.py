"""Git connections (Settings → Git integrations) and committing an
agent's YAML to one. See app/git_integrations.py for the provider calls."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import git_integrations as gi
from ..db import get_session
from ..models import Agent, GitConnection
from ..secrets_crypto import encrypt_secret, require_durable_key, using_ephemeral_key

router = APIRouter(prefix="/api", tags=["git"])


def _http(e: gi.GitIntegrationError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _get(session: Session, connection_id: str) -> GitConnection:
    conn = session.get(GitConnection, connection_id)
    if not conn:
        raise HTTPException(404, f"No Git connection with id '{connection_id}'.")
    return conn


class GitCredentialsIn(BaseModel):
    provider: str
    token: str
    base_url: Optional[str] = None
    username: Optional[str] = None


class GitConnectionIn(GitCredentialsIn):
    name: Optional[str] = None
    default_repo: Optional[str] = None
    default_branch: Optional[str] = None
    default_folder: Optional[str] = None
    commit_on_download: bool = False


class GitConnectionPatch(BaseModel):
    name: Optional[str] = None
    # Omit, or send the mask back, to keep the stored token.
    token: Optional[str] = None
    base_url: Optional[str] = None
    username: Optional[str] = None
    default_repo: Optional[str] = None
    default_branch: Optional[str] = None
    default_folder: Optional[str] = None
    commit_on_download: Optional[bool] = None


@router.get("/git-connections", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO))])
def list_git_connections(session: Session = Depends(get_session)) -> Dict[str, Any]:
    rows = session.exec(select(GitConnection).order_by(GitConnection.created_at)).all()
    return {"connections": [gi.describe(c) for c in rows], "can_store_credentials": not using_ephemeral_key()}


@router.post("/git-connections/test", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def test_git_credentials(body: GitCredentialsIn) -> Dict[str, Any]:
    """Check a token before it is saved: who it belongs to, and that it
    can list repositories."""
    try:
        client = gi._Client(body.provider, body.token, base_url=body.base_url, username=body.username)
        account = gi.whoami(client)
        repos = gi.list_repos(client)
    except gi.GitIntegrationError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "account": account, "repo_count": len(repos),
            "message": f"Signed in to {client.label} as {account} · {len(repos)} repositories visible."}


@router.post("/git-connections", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def create_git_connection(body: GitConnectionIn, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Save a connection after proving the token works — a listed but
    broken connection would only fail later, at the end of an export."""
    try:
        require_durable_key("a Git access token")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    try:
        client = gi._Client(body.provider, body.token, base_url=body.base_url, username=body.username)
        account = gi.whoami(client)
    except gi.GitIntegrationError as e:
        raise HTTPException(400, f"{e} Nothing was saved.")
    if body.commit_on_download and not (body.default_repo and body.default_branch):
        raise HTTPException(400, "Choose a default repository and branch to commit agents on download.")
    try:
        conn = gi.create(session, name=body.name or "", provider=body.provider, token=body.token,
                         base_url=body.base_url, username=body.username, account=account,
                         default_repo=body.default_repo, default_branch=body.default_branch,
                         default_folder=body.default_folder, commit_on_download=body.commit_on_download)
    except gi.GitIntegrationError as e:
        raise _http(e)
    return gi.describe(conn)


@router.patch("/git-connections/{connection_id}", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def update_git_connection(connection_id: str, body: GitConnectionPatch,
                          session: Session = Depends(get_session)) -> Dict[str, Any]:
    conn = _get(session, connection_id)
    new_token = body.token if body.token and body.token != gi.MASK else None
    creds_changed = (new_token is not None
                     or (body.base_url is not None and (gi.normalize_base_url(conn.provider, body.base_url)
                                                        if body.base_url else None) != conn.base_url)
                     or (body.username is not None and ((body.username or "").strip() or None) != conn.username))
    if creds_changed:
        try:
            require_durable_key("a Git access token")
            token = new_token or gi.client_for(conn).token
            client = gi._Client(conn.provider, token,
                                base_url=body.base_url if body.base_url is not None else conn.base_url,
                                username=body.username if body.username is not None else conn.username)
            conn.account = gi.whoami(client)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except gi.GitIntegrationError as e:
            raise HTTPException(e.status, f"{e} Nothing was changed.")
        conn.token = encrypt_secret(token.strip())
        conn.base_url = client.base_url
        conn.username = client.username
        conn.last_checked_at, conn.last_error = datetime.utcnow(), None
    if body.name is not None and body.name.strip():
        conn.name = body.name.strip()
    if body.default_repo is not None:
        conn.default_repo = body.default_repo.strip() or None
    if body.default_branch is not None:
        conn.default_branch = body.default_branch.strip() or None
    if body.default_folder is not None:
        conn.default_folder = body.default_folder.strip().strip("/") or gi.DEFAULT_FOLDER
    if body.commit_on_download is not None:
        conn.commit_on_download = body.commit_on_download
    if conn.commit_on_download and not (conn.default_repo and conn.default_branch):
        raise HTTPException(400, "Choose a default repository and branch to commit agents on download.")
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return gi.describe(conn)


@router.post("/git-connections/{connection_id}/test", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def retest_git_connection(connection_id: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Tokens expire and get revoked; a stored row is not evidence the
    token still works."""
    conn = _get(session, connection_id)
    try:
        client = gi.client_for(conn)
        conn.account = gi.whoami(client)
        if conn.default_repo:
            branches = gi.list_branches(client, conn.default_repo)
            if conn.default_branch and conn.default_branch not in branches:
                raise gi.GitIntegrationError(
                    f"Branch '{conn.default_branch}' no longer exists in {conn.default_repo}.")
        ok, message = True, f"Signed in as {conn.account}" + (f" · {conn.default_repo} reachable" if conn.default_repo else "")
    except gi.GitIntegrationError as e:
        ok, message = False, str(e)
    conn.last_checked_at = datetime.utcnow()
    conn.last_error = None if ok else message[:1000]
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return {"ok": ok, "message": message, **gi.describe(conn)}


@router.delete("/git-connections/{connection_id}", dependencies=[Depends(require_permission(P.INTEGRATIONS))])
def delete_git_connection(connection_id: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Forget a connection and its token. Commits already made stay in the
    repository."""
    conn = _get(session, connection_id)
    session.delete(conn)
    session.commit()
    return {"deleted": connection_id}


@router.get("/git-connections/{connection_id}/repos", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO))])
def git_connection_repos(connection_id: str, session: Session = Depends(get_session)) -> List[Dict[str, Any]]:
    try:
        return gi.list_repos(gi.client_for(_get(session, connection_id)))
    except gi.GitIntegrationError as e:
        raise _http(e)


@router.get("/git-connections/{connection_id}/branches", dependencies=[Depends(require_any_permission(P.INTEGRATIONS, P.STUDIO))])
def git_connection_branches(connection_id: str, repo: str, session: Session = Depends(get_session)) -> List[str]:
    try:
        return gi.list_branches(gi.client_for(_get(session, connection_id)), repo)
    except gi.GitIntegrationError as e:
        raise _http(e)


# --------------------------------------------------------------------------- agents

def _agent_dsl(session: Session, agent_id: str) -> Dict[str, Any]:
    a = session.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    # Same document GET /api/agents/{id}/dsl returns, so YAML and JSON
    # exports import back identically.
    return {"dsl_version": "1", "name": a.name, "subtype": a.subtype, "type": a.type,
            "app_type": a.app_type, "description": a.description, "flow": a.flow}


@router.get("/agents/{agent_id}/export.yaml", dependencies=[Depends(require_permission(P.STUDIO))])
def export_agent_yaml(agent_id: str, session: Session = Depends(get_session)) -> Response:
    dsl = _agent_dsl(session, agent_id)
    return Response(gi.agent_yaml(dsl), media_type="application/yaml", headers={
        "Content-Disposition": f'attachment; filename="{gi.agent_filename(dsl["name"])}"'})


class GitCommitIn(BaseModel):
    connection_id: str
    repo: Optional[str] = None
    branch: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@router.post("/agents/{agent_id}/git-commit", dependencies=[Depends(require_permission(P.STUDIO))])
def commit_agent_to_git(agent_id: str, body: GitCommitIn, session: Session = Depends(get_session)) -> Dict[str, Any]:
    """Commit the agent's YAML to a connected repository. repo / branch /
    path default to the connection's defaults."""
    dsl = _agent_dsl(session, agent_id)
    conn = _get(session, body.connection_id)
    repo = (body.repo or conn.default_repo or "").strip()
    branch = (body.branch or conn.default_branch or "").strip()
    if not repo:
        raise HTTPException(400, f"Choose a repository — '{conn.name}' has no default repository.")
    if not branch:
        raise HTTPException(400, f"Choose a branch — '{conn.name}' has no default branch.")
    folder = (conn.default_folder or gi.DEFAULT_FOLDER).strip("/")
    path = body.path or f"{folder}/{gi.agent_filename(dsl['name'])}"
    message = body.message or f"Export agent '{dsl['name']}' from InaiEra"
    try:
        result = gi.commit_file(gi.client_for(conn), repo, branch, path, gi.agent_yaml(dsl), message)
    except gi.GitIntegrationError as e:
        conn.last_checked_at, conn.last_error = datetime.utcnow(), str(e)[:1000]
        session.add(conn)
        session.commit()
        raise _http(e)
    conn.last_checked_at, conn.last_error = datetime.utcnow(), None
    session.add(conn)
    session.commit()
    return {**result, "connection_id": conn.id, "connection": conn.name, "provider": conn.provider,
            "repo": repo, "branch": branch, "path": gi.clean_path(path)}
