"""Git API: OAuth flow, repo binding, commit/push/PR."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.core.config import get_settings
from app.core.data_policy import PolicyUser
from app.core.deps import DB
from app.core.errors import conflict, forbidden, not_found, validation_failed
from app.core.git_egress_policy import GitEgressPolicyError, assert_binding_url_allowed
from app.core.rbac import RequireGitWrite
from app.git import SUPPORTED_PROVIDERS, get_provider, oauth_available
from app.git.git_local import git_auth_env, scrub, split_userinfo
from pydantic import BaseModel as _BM

from app.schemas.git import (
    CommitOut, CommitRequest, GitBindingCreate, GitBindingOut, GitStatus,
    OAuthInitiateOut, PullRequestCreate, PullRequestOut, RepoListItem,
)
from app.services.audit_service import AuditService
from app.services.git_lock import (
    GitLockLostError, GitLockTimeoutError, check_lock_held, workspace_git_lock,
)
from app.services.git_oauth_state import (
    BINDING_COOKIE, BINDING_COOKIE_MAX_AGE, binding_matches, consume_oauth_state,
    generate_pkce_pair, new_browser_binding, store_oauth_state,
)
from app.services.workspace_service import WorkspaceService

# Module logger. Was missing despite log.* call sites existing here, so any
# Git-import clone failure raised NameError and masked the real cause.
log = structlog.get_logger()

router = APIRouter(prefix="/api/v1/git", tags=["git"])


# --- Inline schemas for new endpoints ---
class WorkspaceFromGitCreate(_BM):
    provider: str
    repo_url: str
    workspace_name: str
    # The branch or tag to import. OMIT it to import the repository's default branch. A ref that is given is a requirement: if it
    # cannot be cloned the import fails (W-014) — it never silently falls back to another branch.
    default_branch: str | None = None
    # Optional PAT — when present the repo is cloned and its files imported
    # into the new workspace so they show up immediately in the file tree.
    access_token: str | None = None

class PATCreate(_BM):
    token: str

class BranchCreate(_BM):
    name: str
    from_branch: str | None = None

class BranchSwitch(_BM):
    branch: str


# --- Credential store: persist PATs so push/pull can authenticate ---
async def save_git_token(user_id: str, provider: str, token: str) -> None:
    """Write a Git PAT to the secrets backend under the same path scheme
    recorded on GitCredential.vault_path (see store_pat below)."""
    if not token:
        return
    from app.storage import get_secrets_backend
    from app.storage.secrets import git_credential_path
    backend = get_secrets_backend()
    await backend.put(git_credential_path(user_id, provider), {"token": token})


async def load_git_token(user_id: str, provider: str) -> str | None:
    from app.storage import get_secrets_backend
    from app.storage.secrets import git_credential_path
    backend = get_secrets_backend()
    secret = await backend.get(git_credential_path(user_id, provider))
    return secret.get("token") if secret else None


# --- Clone + import helpers (used by create_workspace_from_git) ---
_IMPORT_EXT = {
    ".sql": "sql", ".ipynb": "ipynb", ".py": "py", ".md": "md",
    ".yml": "yaml", ".yaml": "yaml", ".json": "json",
}


def _language_for(filename: str) -> str:
    """Map a repo filename to a workspace file language.

    ``*.flink.sql`` is the convention for streaming SQL authored in the
    FlinkSQL editor — it must not come back as plain ``sql`` on import, or the
    file would open with a lakehouse Run button that can't execute it.
    """
    from pathlib import Path

    lower = filename.lower()
    if lower.endswith(".flink.sql"):
        return "flinksql"
    return _IMPORT_EXT.get(Path(lower).suffix, "other")


def _redact(text, token: str | None) -> str:
    """Scrub credentials (token in any encoding, URL userinfo, auth headers)
    from text bound for logs, exceptions or API responses."""
    return scrub(text, token)[:300]


@dataclass(frozen=True)
class CloneResult:
    """What a clone really produced: the immutable commit that is checked out, and the branch (None when detached, e.g. a tag)."""
    commit_sha: str
    branch: str | None


class GitRefUnavailable(Exception):
    """An explicitly requested branch/tag could not be cloned. The import must fail: another branch's code is never an acceptable
    substitute for the one the user named (W-014)."""

    def __init__(self, ref: str, reason: str):
        self.ref, self.reason = ref, reason
        super().__init__(f"could not clone the requested ref '{ref}': {reason}")


def _do_clone(
    url: str, dest: str, branch: str | None, *, provider: str | None = None, token: str | None = None,
) -> CloneResult:
    """Blocking shallow clone of exactly what was asked for.

    `branch` given (a branch or tag name): that ref is cloned, and if it cannot be — a typo, a deleted branch, no access, a transient
    error — this raises GitRefUnavailable. There is NO fallback to the default branch: it used to retry without the ref after ANY
    failure, so a typo or an auth error silently imported different code while the binding still recorded the requested branch
    (W-014). `branch` None: the repository's default branch is cloned, because nothing else was asked for. Returns the immutable commit
    SHA that is checked out and the branch it is on, which is what provenance must record.

    Credentials never touch disk and never appear in a URL: the token is handed to this one git subprocess as an Authorization header
    via env-supplied config (git_auth_env), which also disables HTTP redirects so an allowed host can't bounce the clone somewhere
    internal. A URL that carries credentials itself is refused rather than silently used. Runs through run_git, so it aborts if the
    caller's workspace lock is lost.
    """
    import shutil
    from app.git.git_local import run_git
    from app.services.git_lock import GitLockLostError

    _, _, password = split_userinfo(url)
    if password is not None:
        raise ValueError("repository URL must not contain credentials — pass the token separately")
    env = git_auth_env(url, provider, token)
    args = ["clone", "--depth", "1"]
    if branch:
        args += ["--single-branch", "--branch", branch]
    try:
        run_git([*args, url, dest], env=env, secrets=(token,))
    except GitLockLostError:
        raise
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)            # never leave a half clone behind
        if branch:
            raise GitRefUnavailable(branch, _redact(e, token)) from e
        raise
    sha = run_git(["rev-parse", "HEAD"], cwd=dest, env=env, secrets=(token,))
    head = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest, env=env, secrets=(token,))
    return CloneResult(commit_sha=sha, branch=None if head == "HEAD" else head)


# Cap on how many individual file names we list in responses / status_reason.
_MAX_REPORTED_FILES = 50


@dataclass
class _ImportOutcome:
    """What a clone+import actually did — nothing is skipped silently."""
    files_imported: int = 0
    # Fatal: the clone itself failed, or the import was aborted.
    error: str | None = None
    # Files that SHOULD have been imported but errored (read/create failure).
    failed_files: list[dict] = field(default_factory=list)
    failed_count: int = 0
    # Files deliberately not imported (binary, over the size limit).
    skipped_files: list[dict] = field(default_factory=list)
    skipped_count: int = 0
    # Files left out because max_files_per_workspace was reached.
    truncated_count: int = 0
    # What was actually cloned (W-014): the immutable commit and the branch it is on. None until a clone succeeded.
    commit_sha: str | None = None
    branch: str | None = None

    @property
    def failed(self) -> bool:
        return bool(self.error or self.failed_count or self.truncated_count)

    def _note(self, bucket: list[dict], path: str, reason: str) -> None:
        if len(bucket) < _MAX_REPORTED_FILES:
            bucket.append({"path": path, "reason": reason})

    def fail_file(self, path: str, reason: str) -> None:
        self.failed_count += 1
        self._note(self.failed_files, path, reason)

    def skip_file(self, path: str, reason: str) -> None:
        self.skipped_count += 1
        self._note(self.skipped_files, path, reason)

    def status_reason(self) -> str | None:
        parts: list[str] = []
        if self.error:
            parts.append(self.error)
        if self.failed_count:
            names = ", ".join(f"{f['path']} ({f['reason']})" for f in self.failed_files)
            more = "" if self.failed_count <= len(self.failed_files) else f" and {self.failed_count - len(self.failed_files)} more"
            parts.append(f"{self.failed_count} file(s) failed to import: {names}{more}")
        if self.truncated_count:
            parts.append(
                f"file limit reached: {self.truncated_count} file(s) not imported "
                f"(max_files_per_workspace={get_settings().max_files_per_workspace})"
            )
        if self.skipped_count:
            names = ", ".join(f"{f['path']} ({f['reason']})" for f in self.skipped_files)
            more = "" if self.skipped_count <= len(self.skipped_files) else f" and {self.skipped_count - len(self.skipped_files)} more"
            parts.append(f"{self.skipped_count} file(s) not importable, left in the repo only: {names}{more}")
        return "; ".join(parts) or None


def _error_text(e: Exception) -> str:
    detail = getattr(e, "detail", None)
    if isinstance(detail, dict) and detail.get("message"):
        return str(detail["message"])
    return str(e) or type(e).__name__


class _ImportAborted(Exception):
    """The import can't continue (e.g. the DB session is no longer usable)."""


async def _clone_and_import(
    db, workspace, *, provider: str, repo_url: str, branch: str | None,
    token: str | None, org_id: str,
) -> _ImportOutcome:
    """Clone the bound repo and import its text files into the workspace.

    A clone/auth failure is reported via outcome.error rather than raised.
    Per-file failures and hitting max_files_per_workspace are recorded on the
    outcome (and make the binding 'failed'); binary/oversized files are
    recorded as skipped. A database error aborts with _ImportAborted — the
    caller must roll back.

    Caller must hold workspace_git_lock: this deletes and recreates the
    workspace's working directory.
    """
    import asyncio
    import os
    import shutil
    from pathlib import Path

    from sqlalchemy.exc import SQLAlchemyError

    from app.git.git_local import working_dir_for
    from app.services.file_service import FileService
    from app.schemas.file import FileCreate

    settings = get_settings()
    outcome = _ImportOutcome()
    from app.services.git_lock import check_lock_held

    dest = working_dir_for(str(workspace.id))
    check_lock_held()
    if dest.exists():
        await asyncio.to_thread(shutil.rmtree, str(dest), True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        cloned = await asyncio.to_thread(
            lambda: _do_clone(repo_url, str(dest), branch, provider=provider, token=token)
        )
    except GitLockTimeoutError:
        raise  # lost lock: abort, never report it as a mere clone failure
    except Exception as e:  # noqa: BLE001
        log.warning("git_import_clone_failed", requested_ref=branch, error=_redact(e, token))
        outcome.error = f"clone failed: {_redact(_error_text(e), token)}"
        return outcome

    outcome.commit_sha = cloned.commit_sha
    outcome.branch = cloned.branch or branch or "HEAD"
    return await _import_tree(db, workspace, repo_dir=dest, branch=outcome.branch,
                              org_id=org_id, token=token, upsert=False, outcome=outcome)


async def _import_tree(
    db, workspace, *, repo_dir, branch: str, org_id: str, token: str | None,
    upsert: bool, outcome: "_ImportOutcome | None" = None,
) -> "_ImportOutcome":
    """Copy the working copy's text files into workspace file rows.

    Shared by the initial import (create rows) and pull (upsert rows), so
    both report the same way: per-file errors → outcome.failed_files, the
    workspace file limit → outcome.truncated_count, binary/oversized →
    outcome.skipped_files. Nothing is skipped silently. A database error
    raises _ImportAborted (the session is unusable; caller rolls back).
    Must run under the workspace Git lock; stops if that lock is lost.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.git.safe_read import FileTooLarge, UnsafeRepoPath, read_repo_file, walk_repo_files
    from app.schemas.file import FileCreate, FileUpdate
    from app.services.file_service import FileService
    from app.services.git_lock import check_lock_held

    settings = get_settings()
    outcome = outcome or _ImportOutcome()
    fsvc = FileService(db, org_id)
    for entry in walk_repo_files(repo_dir):
        check_lock_held()
        rel = entry.rel
        if entry.rejected:
            # Symlinks and non-regular files are never read (W-012/W-013); say so instead of dropping silently.
            log.warning("git_import_unsafe_path_rejected", path=rel, reason=entry.rejected)
            outcome.skip_file(rel, entry.rejected)
            continue
        abs_path = entry.path
        if outcome.files_imported >= settings.max_files_per_workspace:
            outcome.truncated_count += 1
            continue
        try:
            data = read_repo_file(repo_dir, abs_path, max_bytes=settings.max_file_size_bytes)
        except UnsafeRepoPath as e:                   # changed under us since the walk
            log.warning("git_import_unsafe_path_rejected", path=rel, reason=e.reason)
            outcome.skip_file(rel, e.reason)
            continue
        except FileTooLarge:
            outcome.skip_file(rel, "over size limit")
            continue
        except OSError as e:
            outcome.fail_file(rel, f"unreadable: {e.strerror or e}")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            outcome.skip_file(rel, "binary")
            continue
        lang = _language_for(abs_path.name)
        try:
            existing = await fsvc.find_by_path(workspace, rel, branch=branch) if upsert else None
            if existing:
                await fsvc.update(workspace, existing.id, FileUpdate(content=text))
                existing.is_dirty = False
            else:
                row, _ = await fsvc.create(
                    workspace,
                    FileCreate(path=rel, language=lang, content=text, branch=branch),
                )
                row.is_dirty = False  # freshly synced from remote, not dirty
            outcome.files_imported += 1
        except SQLAlchemyError as e:
            # The session is unusable now — nothing after this can import.
            raise _ImportAborted(f"database error importing {rel}: {type(e).__name__}") from e
        except Exception as e:  # noqa: BLE001 — recorded, never silently dropped
            outcome.fail_file(rel, _redact(_error_text(e), token)[:120])
    await db.flush()
    return outcome


# --- Connection status (what the "From Git repository" dialog shows) ---

class GitConnectionOut(_BM):
    provider: str
    oauth_available: bool            # an OAuth app is configured on this deployment
    connected: bool                  # this user has a stored credential for it
    username: str | None = None
    instance_url: str | None = None
    expires_at: datetime | None = None
    expired: bool = False


@router.get("/connections", response_model=list[GitConnectionOut])
async def list_git_connections(user: PolicyUser, db: DB) -> list[GitConnectionOut]:
    """The caller's Git sign-in status per provider. Never returns a token —
    only whether one is stored, and for which account."""
    from app.models import GitCredential
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    creds = {
        c.provider: c for c in (await db.execute(
            select(GitCredential).where(GitCredential.user_id == user_row.id)
        )).scalars().all()
    }
    now = datetime.now(timezone.utc)
    out: list[GitConnectionOut] = []
    for name in SUPPORTED_PROVIDERS:
        c = creds.get(name)
        out.append(GitConnectionOut(
            provider=name,
            oauth_available=oauth_available(name),
            connected=c is not None,
            username=c.provider_username if c else None,
            instance_url=c.instance_url if c else None,
            expires_at=c.expires_at if c else None,
            expired=bool(c and c.expires_at and c.expires_at <= now),
        ))
    return out


@router.delete("/connections/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_git(provider: str, user: PolicyUser, db: DB, request: Request) -> Response:
    """Forget the caller's stored credential for `provider` (OAuth token or
    PAT): the vault secret and the GitCredential row. Workspaces bound with it
    can no longer push/pull until the user connects again."""
    from app.models import GitCredential
    from app.storage import get_secrets_backend
    from app.storage.secrets import git_credential_path
    if provider not in SUPPORTED_PROVIDERS:
        raise validation_failed("provider", f"Unknown Git provider: {provider}")
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    rows = (await db.execute(
        select(GitCredential).where(GitCredential.user_id == user_row.id,
                                    GitCredential.provider == provider)
    )).scalars().all()
    await get_secrets_backend().delete(git_credential_path(user.user_id, provider))
    for row in rows:
        await db.delete(row)
    await audit.record(
        user_id=user_row.id, action="git.disconnect", resource_type="git_credential",
        resource_id=None, ip_address=request.client.host if request.client else None,
        metadata={"provider": provider, "removed": len(rows)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- OAuth handshake ---
@router.get("/{provider}/connect", response_model=OAuthInitiateOut)
async def initiate_oauth(
    provider: str,
    user: PolicyUser,
    db: DB,
    request: Request,
    response: Response,
    instance_url: str | None = Query(None),
) -> OAuthInitiateOut:
    """Start the OAuth authorization-code + PKCE flow (WS-GIT-009).

    `state` and the PKCE `code_verifier` are generated here, never handed to
    the caller, and stored server-side (Redis, single-use, 10-minute TTL)
    keyed by `state` — the callback re-derives everything it needs from that
    row rather than trusting anything else the browser brings back.
    """
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    try:
        git_provider = get_provider(provider, instance_url=instance_url)
    except GitEgressPolicyError as e:
        raise validation_failed("instance_url", f"Git host not permitted: {e.reason}")
    except ValueError:
        raise validation_failed("provider", f"Unknown Git provider: {provider}")

    code_verifier, code_challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    redirect_uri = str(request.url_for("oauth_callback", provider=provider))

    try:
        url = git_provider.oauth_authorize_url(
            state=state, redirect_uri=redirect_uri, code_challenge=code_challenge,
        )
    except NotImplementedError as e:
        # e.g. Azure DevOps: real OAuth needs a tenant-specific Entra ID app
        # registration this deployment doesn't have configured — fail with a
        # clear, expected error instead of an unhandled 500, and point at
        # the supported path (PAT) for this provider.
        raise validation_failed(
            "provider", f"OAuth is not available for {provider}: {e}. Use a personal access token instead.",
        )

    binding_nonce, binding_hash = new_browser_binding()
    await store_oauth_state(
        state, user_id=user.user_id, provider=provider,
        redirect_uri=redirect_uri, code_verifier=code_verifier,
        browser_binding=binding_hash, instance_url=instance_url,
    )
    # Lax, not Strict like the session cookie: it has to ride along on the
    # provider's cross-site redirect back to the callback.
    response.set_cookie(
        BINDING_COOKIE, binding_nonce, max_age=BINDING_COOKIE_MAX_AGE,
        httponly=True, secure=get_settings().environment != "dev",
        samesite="lax", path="/api/v1/git",
    )
    await AuditService(db).record(
        user_id=user_row.id, action="git.oauth_initiate",
        resource_type="git_provider", resource_id=None,
        ip_address=request.client.host if request.client else None,
        metadata={"provider": provider},
    )
    return OAuthInitiateOut(authorize_url=url, state=state)


@router.get("/{provider}/callback", name="oauth_callback")
async def oauth_callback(
    provider: str,
    db: DB,
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """Complete the OAuth flow: verify state, exchange the code, persist the
    token to the secrets backend, and record a GitCredential reference row.

    `state` is consumed (deleted) the instant it's read — a captured or
    replayed callback URL can't be used twice, and a state that never
    existed or already expired fails closed rather than silently proceeding
    unauthenticated.
    """
    from sqlalchemy import select
    from app.models import GitCredential, User
    from app.storage.secrets import git_credential_path

    data = await consume_oauth_state(state)
    if data is None or data.get("provider") != provider:
        return _oauth_error('Sign-in link expired or was already used — try connecting again.')
    if not binding_matches(request.cookies.get(BINDING_COOKIE), data.get("browser_binding")):
        # Finished in a different browser than the one that started it.
        log.warning("git_oauth_binding_mismatch", provider=provider)
        return _oauth_error('This sign-in was started from a different browser — try connecting again.')

    user_row = (await db.execute(
        select(User).where(User.external_id == data["user_id"])
    )).scalar_one_or_none()
    if not user_row:
        return _oauth_error('User no longer exists')

    try:
        git_provider = get_provider(provider, instance_url=data.get("instance_url"))
        token_pair = await git_provider.exchange_code_for_token(
            code, data["redirect_uri"], code_verifier=data["code_verifier"],
        )
    except Exception as e:  # noqa: BLE001 — surface any provider/network failure as a clean redirect
        log.warning("git_oauth_exchange_failed", provider=provider, error=str(e))
        return _oauth_error('Could not complete sign-in with ' + provider)

    await save_git_token(data["user_id"], provider, token_pair.access_token)
    vault_path = git_credential_path(data["user_id"], provider)
    existing_cred = (await db.execute(
        select(GitCredential).where(
            GitCredential.user_id == user_row.id,
            GitCredential.provider == provider,
        )
    )).scalar_one_or_none()
    if not existing_cred:
        db.add(GitCredential(
            user_id=user_row.id,
            provider=provider,
            provider_user_id=data["user_id"],
            instance_url=data.get("instance_url"),
            vault_path=vault_path,
            scopes=["repo"],
        ))
    else:
        # Reconnecting (e.g. to a different self-hosted instance): the row
        # must describe the token just saved, not the previous one.
        existing_cred.instance_url = data.get("instance_url")
        existing_cred.vault_path = vault_path
    await db.flush()

    await AuditService(db).record(
        user_id=user_row.id, action="git.oauth_complete",
        resource_type="git_provider", resource_id=None,
        ip_address=request.client.host if request.client else None,
        metadata={"provider": provider},
    )
    resp = RedirectResponse(url="/?git_connected=" + provider)
    resp.delete_cookie(BINDING_COOKIE, path="/api/v1/git")
    return resp


def _oauth_error(message: str) -> RedirectResponse:
    resp = RedirectResponse(url=f"/?git_connect_error={quote(message)}")
    resp.delete_cookie(BINDING_COOKIE, path="/api/v1/git")
    return resp


# --- Repo listing ---
@router.get("/{provider}/repos", response_model=list[RepoListItem])
async def list_repos(provider: str, user: PolicyUser, db: DB) -> list[RepoListItem]:
    """List repos visible to the caller's connected OAuth/PAT token."""
    git_provider = get_provider(provider)
    token = await load_git_token(user.user_id, provider)
    if not token and get_settings().environment == "dev":
        import os
        token = os.environ.get(f"{provider.upper()}_DEV_TOKEN", "")
    if not token:
        raise validation_failed(
            "token", f"No stored credential for {provider} — connect via OAuth or store a PAT first.",
        )
    repos = await git_provider.list_repos(token)
    return [
        RepoListItem(
            id=r.id, name=r.name, full_name=r.full_name,
            default_branch=r.default_branch, clone_url=r.clone_url, private=r.private,
        ) for r in repos
    ]


# --- Shared guards for operations on a bound repo (WS-GIT-009) ---
@asynccontextmanager
async def _repo_lock(workspace_id):
    """Hold the workspace's distributed Git lock for the block (auto-renewed,
    released on every exit path); a lock already held elsewhere → 409."""
    try:
        async with workspace_git_lock(workspace_id):
            yield
    except GitLockTimeoutError as e:
        raise conflict(str(e), code="GIT_REPOSITORY_LOCKED", resource="git_repository", field="lock")


def _assert_binding_egress(binding) -> None:
    """Re-validate a binding's remote before anything talks to it — bindings
    created before the allowlist / per-provider host binding existed (or
    before an operator tightened them) must not keep working."""
    try:
        assert_binding_url_allowed(binding.provider, binding.repo_url)
    except GitEgressPolicyError as e:
        raise forbidden(f"Git host not permitted for this binding: {e.reason}")


def _require_ready(binding, *, allow_failed: bool = False) -> None:
    """409 unless the binding is usable. `allow_failed` is for pull only:
    pull re-syncs the entire tree from the remote, so it is the recovery
    path out of 'failed' (a failed import, a partial pull, a lost lock).
    'provisioning' is never usable — the initial import still owns it."""
    if allow_failed and binding.status == "failed":
        return
    if binding.status != "ready":
        detail = f": {binding.status_reason}" if binding.status_reason else ""
        raise conflict(
            f"Repository binding is '{binding.status}', not 'ready' — "
            f"re-import the repository before using Git operations{detail}",
            code="GIT_BINDING_NOT_READY", resource="git_binding", field="status",
        )


async def _load_token_for(user, provider: str, *, dev_fallback: bool = True) -> str | None:
    token = await load_git_token(user.user_id, provider)
    if not token and dev_fallback and get_settings().environment == "dev":
        import os
        token = os.environ.get(f"{provider.upper()}_DEV_TOKEN", "") or None
    return token


async def _get_binding(db, ws, workspace_id):
    from sqlalchemy import select
    from app.models import GitBinding
    binding = (await db.execute(
        select(GitBinding).where(GitBinding.workspace_id == ws.id)
    )).scalar_one_or_none()
    if not binding:
        raise not_found("git_binding", str(workspace_id))
    return binding


async def _open_working_copy(local, branch: str, token: str | None) -> None:
    try:
        await local.clone_or_open(branch=branch)
    except Exception as e:  # noqa: BLE001
        raise validation_failed("git", f"Could not open working copy: {_redact(e, token)}")


# --- Workspace binding ---
@router.post(
    "/workspaces/{workspace_id}/binding",
    response_model=GitBindingOut, status_code=status.HTTP_201_CREATED,
)
async def bind_repo(
    workspace_id: UUID,
    payload: GitBindingCreate,
    user: PolicyUser,
    db: DB,
    request: Request,
) -> GitBindingOut:
    from app.models import GitBinding
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    try:
        assert_binding_url_allowed(payload.provider, payload.repo_url)
    except GitEgressPolicyError as e:
        raise validation_failed("repo_url", str(e))
    binding = GitBinding(
        workspace_id=ws.id,
        provider=payload.provider,
        repo_url=payload.repo_url,
        default_branch=payload.default_branch,
        current_branch=payload.default_branch,
        requested_ref=payload.default_branch if "default_branch" in payload.model_fields_set else None,
    )
    db.add(binding)
    await db.flush()
    await audit.record(
        user_id=user_row.id,
        action="git.bind",
        resource_type="workspace",
        resource_id=ws.id,
        ip_address=request.client.host if request.client else None,
        metadata={"provider": payload.provider, "repo_url": payload.repo_url},
    )
    return GitBindingOut.model_validate(binding)


@router.get(
    "/workspaces/{workspace_id}/binding",
    response_model=GitBindingOut,
)
async def get_binding(
    workspace_id: UUID,
    user: PolicyUser,
    db: DB,
) -> GitBindingOut:
    """Return the Git binding for a workspace (what repo it's connected to).

    The SPA calls this on workspace load to show the connected repo/branch.
    """
    from sqlalchemy import select
    from app.models import GitBinding
    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = (await db.execute(
        select(GitBinding).where(GitBinding.workspace_id == ws.id)
    )).scalar_one_or_none()
    if not binding:
        raise not_found("git_binding", str(workspace_id))
    return GitBindingOut.model_validate(binding)


@router.post(
    "/workspaces/{workspace_id}/commit",
    response_model=CommitOut,
)
async def commit(
    workspace_id: UUID,
    payload: CommitRequest,
    user: RequireGitWrite,
    db: DB,
    request: Request,
) -> CommitOut:
    """Commit dirty files to the bound Git working copy.

    Workflow:
      1. Resolve workspace + binding (must be 'ready', remote re-validated).
      2. Under the workspace Git lock: materialize the local working copy.
      3. Read every dirty file's content from object storage.
      4. Write into the working copy, stage, commit.
      5. Mark committed files as no-longer-dirty in metadata; record commit sha.
      6. Audit.
    """
    from sqlalchemy import select
    from app.models import WorkspaceFile
    from app.services.file_service import FileService
    from app.git.git_local import LocalRepo, working_dir_for

    settings = get_settings()
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = await _get_binding(db, ws, workspace_id)
    _require_ready(binding)
    _assert_binding_egress(binding)

    # Serializes this working copy's mutation across the whole deployment,
    # not just this process (see git_lock.py). The context manager releases
    # it on every path, including exceptions anywhere in the block.
    async with _repo_lock(ws.id):
        dirty = (await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == ws.id,
                WorkspaceFile.branch == binding.current_branch,
                WorkspaceFile.is_dirty.is_(True),
            )
        )).scalars().all()
        if not dirty and not payload.allow_empty:
            raise validation_failed("files", "No dirty files to commit")

        # Materialize working copy. The stored token (if any) is passed to
        # each git subprocess via env — never written into .git/config.
        repo_dir = working_dir_for(str(ws.id))
        token = await load_git_token(user.user_id, binding.provider)
        local = LocalRepo(repo_dir, binding.repo_url, provider=binding.provider, token=token)
        try:
            await local.clone_or_open(branch=binding.current_branch)
            # Rebase onto the remote tip BEFORE committing. An existing working
            # copy is only opened, never fetched, so without this the commit
            # lands on a stale base and the push is rejected as non-fast-forward.
            sync = await local.sync_to_remote(binding.current_branch)
        except Exception as e:  # noqa: BLE001
            raise validation_failed("git", f"Could not open working copy: {_redact(e, token)}")

        # The reset above may have dropped commits that never reached the remote
        # (a previous commit whose push was rejected). Their files are flagged
        # non-dirty, so committing only the dirty set would drop their content from
        # Git entirely. Re-materialize the whole branch instead.
        if sync and sync.discarded_local_commits:
            dirty = (await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.workspace_id == ws.id,
                    WorkspaceFile.branch == binding.current_branch,
                    WorkspaceFile.is_deleted.is_(False),
                )
            )).scalars().all()
            log.info(
                "git_commit_rematerialize_branch",
                workspace_id=str(ws.id), file_count=len(dirty),
                reason="local commits were not on the remote",
            )

        # Read content from storage and stage in the working tree
        file_svc = FileService(db, settings.org_id)
        files_to_write: dict[str, bytes] = {}
        for f in dirty:
            if f.is_deleted:
                # Deletion: remove from the tree; `git add -A` stages it.
                target = repo_dir / f.path
                check_lock_held()
                if target.exists():
                    target.unlink()
                continue
            _, text = await file_svc.get_with_content(ws, f.id)
            files_to_write[f.path] = text.encode("utf-8")

        # Commit. If the working tree already matches (nothing to commit), treat it
        # as a no-op success at the current HEAD instead of erroring.
        author_name = user.email or user.user_id
        author_email = user.email or f"{user.user_id}@workspace.inaiera.com"
        try:
            sha = await local.commit_files(
                files_to_write=files_to_write,
                message=payload.message,
                author_name=author_name,
                author_email=author_email,
            )
        except RuntimeError as e:
            if "nothing to commit" in str(e).lower():
                sha = await local.head_sha()
            else:
                raise validation_failed("commit", f"Commit failed: {_redact(e, token)}")

        # Mark non-dirty + record committed sha
        for f in dirty:
            f.is_dirty = False
            f.last_committed_sha = sha
        binding.last_synced_sha = sha
        from datetime import datetime, timezone
        binding.last_synced_at = datetime.now(timezone.utc)
        await db.flush()

        await audit.record(
            user_id=user_row.id,
            action="git.commit",
            resource_type="workspace",
            resource_id=ws.id,
            ip_address=request.client.host if request.client else None,
            metadata={
                "provider": binding.provider,
                "branch": binding.current_branch,
                "commit_sha": sha,
                "file_count": len(files_to_write),
            },
        )
        return CommitOut(
            commit_sha=sha,
            branch=binding.current_branch,
            committed_file_count=len(files_to_write),
        )


@router.post(
    "/workspaces/{workspace_id}/push",
    status_code=status.HTTP_202_ACCEPTED,
)
async def push(
    workspace_id: UUID,
    user: RequireGitWrite,
    db: DB,
    request: Request,
) -> dict:
    """Push the current branch to the remote.

    The token from the secrets vault authenticates this one git subprocess
    via an env-supplied Authorization header; it is never written to disk.
    """
    from app.git.git_local import LocalRepo, working_dir_for

    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = await _get_binding(db, ws, workspace_id)
    _require_ready(binding)
    _assert_binding_egress(binding)

    token = await _load_token_for(user, binding.provider)
    if not token:
        raise validation_failed("token", "No stored credential — connect the repo with a PAT (or store one) so push can authenticate.")

    async with _repo_lock(ws.id):
        repo_dir = working_dir_for(str(ws.id))
        local = LocalRepo(repo_dir, binding.repo_url, provider=binding.provider, token=token)
        await _open_working_copy(local, binding.current_branch, token)
        try:
            await local.push(branch=binding.current_branch)
        except Exception as e:  # noqa: BLE001 — surface git/auth failures cleanly
            raise validation_failed("push", f"Push failed: {_redact(e, token)}")

        await audit.record(
            user_id=user_row.id,
            action="git.push",
            resource_type="workspace",
            resource_id=ws.id,
            ip_address=request.client.host if request.client else None,
            metadata={"provider": binding.provider, "branch": binding.current_branch},
        )
        return {"status": "pushed", "branch": binding.current_branch}


@router.post("/workspaces/{workspace_id}/pull", status_code=status.HTTP_200_OK)
async def pull(workspace_id: UUID, user: PolicyUser, db: DB, request: Request) -> dict:
    """Pull the remote branch and re-import changed files into the workspace.

    Any file that couldn't be re-imported (or the workspace file limit) is
    reported: the response says ``"status": "pulled_with_errors"`` and lists
    the files, and the binding goes to 'failed' with them in status_reason —
    commit/push then refuse until a clean pull, so a commit can't write the
    stale workspace copy of those files back over the remote's. A clean pull
    sets the binding (back) to 'ready'.
    """
    from app.git.git_local import LocalRepo, working_dir_for

    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = await _get_binding(db, ws, workspace_id)
    _require_ready(binding, allow_failed=True)
    _assert_binding_egress(binding)

    async with _repo_lock(ws.id):
        token = await load_git_token(user.user_id, binding.provider)
        repo_dir = working_dir_for(str(ws.id))
        local = LocalRepo(repo_dir, binding.repo_url, provider=binding.provider, token=token)
        await _open_working_copy(local, binding.current_branch, token)
        try:
            sha = await local.pull(binding.current_branch)
        except GitLockTimeoutError:
            raise
        except Exception as e:  # noqa: BLE001
            raise validation_failed("pull", f"Pull failed: {_redact(e, token)}")

        try:
            outcome = await _import_tree(
                db, ws, repo_dir=repo_dir, branch=binding.current_branch,
                org_id=user.org_id, token=token, upsert=True,
            )
        except _ImportAborted as e:
            # DB session unusable: drop the partial re-import, but persist
            # that the workspace is now out of sync with the working copy.
            await db.rollback()
            await db.refresh(binding)
            binding.status = "failed"
            binding.status_reason = f"pull re-import aborted: {e}"
            await db.commit()
            raise validation_failed("pull", f"Pull re-import aborted: {e}")

        binding.last_synced_sha = sha
        binding.status = "failed" if outcome.failed else "ready"
        binding.status_reason = outcome.status_reason()
        await db.flush()
        await audit.record(
            user_id=user_row.id, action="git.pull", resource_type="workspace", resource_id=ws.id,
            ip_address=request.client.host if request.client else None,
            metadata={
                "branch": binding.current_branch, "sha": sha,
                "files_synced": outcome.files_imported,
                "files_failed": outcome.failed_count,
                "files_skipped": outcome.skipped_count,
                "files_truncated": outcome.truncated_count,
                "binding_status": binding.status,
            },
        )
        return {
            "status": "pulled_with_errors" if outcome.failed else "pulled",
            "branch": binding.current_branch,
            "files_synced": outcome.files_imported,
            "failed_files": outcome.failed_files,
            "failed_file_count": outcome.failed_count,
            "skipped_files": outcome.skipped_files,
            "skipped_file_count": outcome.skipped_count,
            "truncated_file_count": outcome.truncated_count,
            "binding_status": binding.status,
            "status_reason": binding.status_reason,
        }


@router.get(
    "/workspaces/{workspace_id}/status",
    response_model=GitStatus,
)
async def git_status(
    workspace_id: UUID,
    user: PolicyUser,
    db: DB,
) -> GitStatus:
    """Show working copy status: branch, modified files, ahead/behind counts."""
    from sqlalchemy import select
    from app.models import GitBinding
    from app.git.git_local import LocalRepo, working_dir_for

    ws_svc = WorkspaceService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)

    binding = (await db.execute(
        select(GitBinding).where(GitBinding.workspace_id == ws.id)
    )).scalar_one_or_none()
    if not binding:
        raise not_found("git_binding", str(workspace_id))

    repo_dir = working_dir_for(str(ws.id))
    if not (repo_dir / ".git").exists():
        return GitStatus(
            branch=binding.current_branch,
            ahead=0, behind=0,
            modified_files=[], untracked_files=[],
            last_commit_sha=binding.last_synced_sha,
            last_commit_message=None,
        )
    local = LocalRepo(repo_dir, binding.repo_url)
    st = await local.status()
    return GitStatus(**st)


@router.post(
    "/workspaces/{workspace_id}/pull-requests",
    response_model=PullRequestOut,
)
async def create_pull_request(
    workspace_id: UUID,
    payload: PullRequestCreate,
    user: PolicyUser,
    db: DB,
    request: Request,
) -> PullRequestOut:
    from app.models import GitBinding
    from sqlalchemy import select
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding_row = (await db.execute(
        select(GitBinding).where(GitBinding.workspace_id == ws.id)
    )).scalar_one_or_none()
    if not binding_row:
        raise not_found("git_binding", str(workspace_id))
    _assert_binding_egress(binding_row)
    if not binding_row.repo_full_name:
        raise validation_failed("repo_full_name", "Cannot create PR without repo_full_name on binding")
    provider = get_provider(binding_row.provider)
    token = await load_git_token(user.user_id, binding_row.provider)
    if not token and get_settings().environment == "dev":
        import os
        token = os.environ.get(f"{binding_row.provider.upper()}_DEV_TOKEN", "")
    if not token:
        raise validation_failed(
            "token", f"No stored credential for {binding_row.provider} — connect via OAuth or store a PAT first.",
        )
    pr = await provider.create_pull_request(
        access_token=token,
        repo_full_name=binding_row.repo_full_name,
        source_branch=payload.source_branch,
        target_branch=payload.target_branch,
        title=payload.title,
        body=payload.body,
    )
    await audit.record(
        user_id=user_row.id,
        action="git.pr_create",
        resource_type="workspace",
        resource_id=ws.id,
        ip_address=request.client.host if request.client else None,
        metadata={"provider": binding_row.provider, "pr_number": pr.number, "url": pr.url},
    )
    return PullRequestOut(
        provider=binding_row.provider,
        pr_number=pr.number,
        url=pr.url,
        state=pr.state,
        source_branch=pr.source_branch,
        target_branch=pr.target_branch,
    )


# --- Create workspace from Git ---
@router.post(
    "/workspaces/from-git",
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_from_git(
    payload: "WorkspaceFromGitCreate",
    user: PolicyUser,
    db: DB,
    request: Request,
) -> dict:
    """Create a workspace bound to a Git repository and import its files.

    This is what the 'Create workspace from Git repository' modal calls — one
    request instead of the two-step create-then-bind flow. State is made
    durable in steps (WS-GIT-009):
      1. workspace + binding(status='provisioning') are COMMITTED before the
         clone starts — visible to other requests, and a crash mid-clone
         leaves an honest 'provisioning' row rather than nothing;
      2. the clone+import runs under the workspace Git lock;
      3. the final status — 'ready', or 'failed' with status_reason — is
         committed. Any per-file import error or hitting the workspace file
         limit ends in 'failed', with the affected files listed.
    """
    from app.models import GitBinding
    from app.schemas.workspace import WorkspaceCreate
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    try:
        assert_binding_url_allowed(payload.provider, payload.repo_url)
    except GitEgressPolicyError as e:
        raise validation_failed("repo_url", str(e))

    ws = await ws_svc.create(user_row, WorkspaceCreate(
        name=payload.workspace_name,
        description=f"Created from {payload.provider} repository",
    ))
    requested_ref = (payload.default_branch or "").strip() or None
    binding = GitBinding(
        workspace_id=ws.id,
        provider=payload.provider,
        repo_url=payload.repo_url,
        # Until the clone says which branch the default really is, the placeholder is the requested ref (or "main").
        default_branch=requested_ref or "main",
        current_branch=requested_ref or "main",
        requested_ref=requested_ref,
        status="provisioning",
    )
    db.add(binding)
    await db.flush()
    # Plain values: a rollback below expires ORM instances.
    ws_id, ws_name, binding_id, user_row_id = ws.id, ws.name, binding.id, user_row.id

    # Persist the PAT so commit/push/pull can authenticate later.
    if payload.access_token:
        await save_git_token(user.user_id, payload.provider, payload.access_token)
    # No token pasted: use the one stored by a completed OAuth sign-in (or an
    # earlier PAT) for this provider, so private repos clone on the OAuth path.
    clone_token = payload.access_token or await load_git_token(user.user_id, payload.provider)

    # Step 1: 'provisioning' is durable and visible before the slow part.
    await db.commit()

    lock_conflict: Exception | None = None
    # Step 2: clone + import, serialized with every other repo mutation on
    # this workspace (it deletes/recreates the working directory).
    try:
        async with workspace_git_lock(ws_id):
            outcome = await _clone_and_import(
                db, ws,
                provider=payload.provider,
                repo_url=payload.repo_url,
                branch=requested_ref,
                token=clone_token,
                org_id=user.org_id,
            )
    except GitLockTimeoutError as e:
        # Busy, or the lock was lost mid-import (GitLockLostError): another
        # writer owns the working copy now. Drop whatever this import wrote,
        # persist 'failed', and tell the caller 409 (below).
        await db.rollback()
        await db.refresh(binding)
        lock_conflict = e
        outcome = _ImportOutcome(
            error=("lock lost mid-import: " if isinstance(e, GitLockLostError) else "repository busy: ") + str(e)
        )
    except Exception as e:  # noqa: BLE001 — record the failure, don't leave 'provisioning'
        await db.rollback()  # drop the partial import; the step-1 rows survive
        await db.refresh(binding)
        log.warning("git_import_aborted", workspace_id=str(ws_id), error=_redact(e, payload.access_token))
        outcome = _ImportOutcome(error=f"import aborted: {_redact(_error_text(e), payload.access_token)}")

    # Step 3: commit the final state.
    final_status = "failed" if outcome.failed else "ready"
    binding.status = final_status
    binding.status_reason = outcome.status_reason()
    if outcome.commit_sha:                      # W-014: what was actually imported, not what was asked for
        binding.last_synced_sha = outcome.commit_sha
        binding.last_synced_at = datetime.now(timezone.utc)
        if outcome.branch:
            binding.default_branch = binding.current_branch = outcome.branch
    await db.flush()
    await audit.record(
        user_id=user_row_id,
        action="workspace.create_from_git",
        resource_type="workspace",
        resource_id=ws_id,
        ip_address=request.client.host if request.client else None,
        metadata={
            "provider": payload.provider,
            "repo_url": payload.repo_url,
            "files_imported": outcome.files_imported,
            "files_failed": outcome.failed_count,
            "files_skipped": outcome.skipped_count,
            "files_truncated": outcome.truncated_count,
            "import_error": outcome.error,
            "requested_ref": requested_ref,
            "commit_sha": outcome.commit_sha,
            "branch": outcome.branch,
            "status": final_status,
        },
    )
    await db.commit()
    if lock_conflict is not None:
        raise conflict(
            f"{lock_conflict} — workspace {ws_id} was created but its import did not "
            f"complete (binding status 'failed'); pull to retry the sync",
            code="GIT_IMPORT_INCOMPLETE", resource="git_binding", field="status",
        )
    return {
        "workspace_id": str(ws_id),
        "workspace_name": ws_name,
        "binding_id": str(binding_id),
        "provider": payload.provider,
        "repo_url": payload.repo_url,
        "branch": outcome.branch or requested_ref,
        "requested_ref": requested_ref,
        "commit_sha": outcome.commit_sha,
        "files_imported": outcome.files_imported,
        "import_error": outcome.error,
        "failed_files": outcome.failed_files,
        "failed_file_count": outcome.failed_count,
        "skipped_files": outcome.skipped_files,
        "skipped_file_count": outcome.skipped_count,
        "truncated_file_count": outcome.truncated_count,
        "status": final_status,
        "status_reason": binding.status_reason,
    }


# --- PAT (Personal Access Token) credential storage ---
@router.post("/workspaces/{workspace_id}/token")
async def store_pat(
    workspace_id: UUID,
    payload: "PATCreate",
    user: PolicyUser,
    db: DB,
    request: Request,
) -> dict:
    """Store a personal access token for the workspace's Git binding.
    Writes to the secrets vault; never stored in the DB."""
    from sqlalchemy import select
    from app.models import GitBinding, GitCredential
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = (await db.execute(
        select(GitBinding).where(GitBinding.workspace_id == ws.id)
    )).scalar_one_or_none()
    if not binding:
        raise not_found("git_binding", str(workspace_id))
    vault_path = f"inaiera/git/{user.user_id}/{binding.provider}"
    # Persist the token to the secrets backend so commit/push/pull can authenticate.
    await save_git_token(user.user_id, binding.provider, payload.token)
    import structlog as slog
    slog.get_logger().info("pat_stored", vault_path=vault_path, provider=binding.provider)
    existing_cred = (await db.execute(
        select(GitCredential).where(
            GitCredential.user_id == user_row.id,
            GitCredential.provider == binding.provider,
        )
    )).scalar_one_or_none()
    if not existing_cred:
        cred = GitCredential(
            user_id=user_row.id,
            provider=binding.provider,
            # PAT auth doesn't carry the provider's numeric user id (that comes
            # from the OAuth flow). Record the InAiEra principal as a non-null
            # stand-in so the credential reference row satisfies its schema.
            provider_user_id=str(user.user_id),
            provider_username=user.email,
            vault_path=vault_path,
            scopes=["repo"],
        )
        db.add(cred)
        await db.flush()
    await audit.record(
        user_id=user_row.id, action="git.pat_stored",
        resource_type="workspace", resource_id=ws.id,
        ip_address=request.client.host if request.client else None,
        metadata={"provider": binding.provider},
    )
    return {"status": "stored", "provider": binding.provider}


# --- Branch management ---
@router.post("/workspaces/{workspace_id}/branches", status_code=status.HTTP_201_CREATED)
async def create_branch(
    workspace_id: UUID,
    payload: BranchCreate,
    user: PolicyUser,
    db: DB,
    request: Request,
) -> dict:
    """Create a new Git branch from the current branch (or a specified base)."""
    from app.git.git_local import LocalRepo, working_dir_for
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = await _get_binding(db, ws, workspace_id)
    _require_ready(binding)
    _assert_binding_egress(binding)
    async with _repo_lock(ws.id):
        token = await load_git_token(user.user_id, binding.provider)
        repo_dir = working_dir_for(str(ws.id))
        local = LocalRepo(repo_dir, binding.repo_url, provider=binding.provider, token=token)
        await _open_working_copy(local, binding.current_branch, token)
        try:
            await local.checkout(payload.name, create=True)
        except Exception as e:  # noqa: BLE001
            raise validation_failed("branch", f"Could not create branch: {_redact(e, token)}")
        binding.current_branch = payload.name
        await db.flush()
        await audit.record(
            user_id=user_row.id, action="git.branch_create",
            resource_type="workspace", resource_id=ws.id,
            ip_address=request.client.host if request.client else None,
            metadata={"branch": payload.name},
        )
        return {"branch": payload.name}


@router.patch("/workspaces/{workspace_id}/branch")
async def switch_branch(
    workspace_id: UUID,
    payload: "BranchSwitch",
    user: PolicyUser,
    db: DB,
    request: Request,
) -> dict:
    """Check out an existing remote branch."""
    from app.git.git_local import LocalRepo, working_dir_for
    ws_svc = WorkspaceService(db)
    audit = AuditService(db)
    user_row = await ws_svc.ensure_user(user)
    ws = await ws_svc.get(user_row, workspace_id)
    binding = await _get_binding(db, ws, workspace_id)
    _require_ready(binding)
    _assert_binding_egress(binding)
    async with _repo_lock(ws.id):
        token = await load_git_token(user.user_id, binding.provider)
        repo_dir = working_dir_for(str(ws.id))
        local = LocalRepo(repo_dir, binding.repo_url, provider=binding.provider, token=token)
        await _open_working_copy(local, binding.current_branch, token)
        try:
            await local.checkout(payload.branch, create=False)
        except Exception as e:  # noqa: BLE001
            raise validation_failed("branch", f"Could not switch branch: {_redact(e, token)}")
        binding.current_branch = payload.branch
        await db.flush()
        await audit.record(
            user_id=user_row.id, action="git.branch_switch",
            resource_type="workspace", resource_id=ws.id,
            ip_address=request.client.host if request.client else None,
            metadata={"branch": payload.branch},
        )
        return {"branch": payload.branch}


# --- Webhook receiver ---
@router.post("/webhooks/{provider}")
async def receive_webhook(provider: str, request: Request) -> dict:
    """Receive PR / push events from the Git provider.

    Real impl:
      1. Verify signature via provider.verify_webhook(headers, body)
      2. Parse event, identify affected workspace by repo_full_name
      3. Publish event to Redis on session channels of users watching the workspace

    An unverifiable webhook is rejected with 401 — including every webhook
    for a provider whose secret isn't configured outside dev (WS-GIT-009).
    """
    body = await request.body()
    try:
        git_provider = get_provider(provider)
    except ValueError:
        raise not_found("git_provider", provider)
    if not git_provider.verify_webhook(dict(request.headers), body):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WEBHOOK_SIGNATURE_INVALID", "message": "invalid webhook signature", "details": {}},
        )
    return {"status": "ok"}
