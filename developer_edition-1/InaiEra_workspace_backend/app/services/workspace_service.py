"""Workspace service.

A user gets one or more personal workspaces. Each workspace has its own
storage prefix in the data plane's object storage.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.core.auth import Principal
from app.core.config import get_settings
from app.core.errors import conflict, flush_or_conflict, not_found
from app.models import User, Workspace
from app.schemas.workspace import WorkspaceCreate, WorkspaceUpdate
from app.storage import get_blob_store

log = structlog.get_logger()


class WorkspaceService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def ensure_user(self, principal: Principal) -> User:
        """Create or update the local User mirror, keyed by the control plane's immutable subject (W-020, X-003).

        `principal.user_id` is the subject; the username is an attribute that is refreshed on every request. A row created
        before subjects were used holds the username as its key: the first request that carries both re-keys that row IN PLACE
        (same row id, so its workspaces, sessions, executions and Git credentials stay the person's) instead of creating a
        second, empty user.
        """
        user = await self._by_external_id(principal.user_id)
        if user is None:
            user = await self._adopt_legacy_row(principal)
        if user is None:
            user = User(external_id=principal.user_id, username=principal.username, email=principal.email,
                        last_seen_at=datetime.now(timezone.utc))
            try:
                async with self._db.begin_nested():
                    self._db.add(user)
                    await self._db.flush()
            except IntegrityError:           # a parallel first request created (or re-keyed to) it
                user = await self._by_external_id(principal.user_id)
                if user is None:
                    raise
        user.last_seen_at = datetime.now(timezone.utc)
        if principal.email and user.email != principal.email:
            user.email = principal.email
        if principal.username and user.username != principal.username:
            user.username = principal.username
        return user

    async def _by_external_id(self, external_id: str) -> User | None:
        return (await self._db.execute(select(User).where(User.external_id == external_id))).scalar_one_or_none()

    async def _adopt_legacy_row(self, principal: Principal) -> User | None:
        """Re-key the row that holds this person's username as its key (pre-subject data) to their subject.

        Only when the subject is a real subject (differs from the username) and no row holds it yet. The UPDATE is conditional on
        the old key, so of two parallel first requests exactly one adopts the row."""
        legacy_key = principal.username
        if not legacy_key or legacy_key == principal.user_id:
            return None
        legacy = await self._by_external_id(legacy_key)
        if legacy is None:
            return None
        done = cast(CursorResult, await self._db.execute(
            update(User).where(User.id == legacy.id, User.external_id == legacy_key)
            .values(external_id=principal.user_id, username=legacy_key)))
        if done.rowcount != 1:
            return None
        await self._db.refresh(legacy)
        log.info("user_rekeyed_to_subject", user_row=str(legacy.id), subject=principal.user_id)
        await self._move_git_secrets(legacy, legacy_key, principal.user_id)
        return legacy

    async def _move_git_secrets(self, user: User, old_key: str, new_key: str) -> None:
        """Git tokens live in the secrets backend under a path built from the user key: move them to the subject's path.
        A failure is logged and leaves the old secret where it is (the person can store the token again); it never blocks login."""
        from app.models import GitCredential
        from app.storage import get_secrets_backend
        from app.storage.secrets import git_credential_path

        try:
            backend = get_secrets_backend()
            rows = (await self._db.execute(select(GitCredential).where(GitCredential.user_id == user.id))).scalars().all()
            for cred in rows:
                old_path = git_credential_path(old_key, cred.provider)
                if cred.vault_path != old_path:
                    continue
                secret = await backend.get(old_path)
                if secret:
                    new_path = git_credential_path(new_key, cred.provider)
                    await backend.put(new_path, secret)
                    cred.vault_path = new_path
                    await backend.delete(old_path)
        except Exception as exc:                      # noqa: BLE001 — never block a login on a secrets-backend hiccup
            log.error("git_secret_move_failed", user_row=str(user.id), error=str(exc))

    async def list_for_user(self, user: User) -> list[Workspace]:
        result = await self._db.execute(
            select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.updated_at.desc())
        )
        return list(result.scalars())

    async def get(self, user: User, workspace_id: uuid.UUID) -> Workspace:
        ws = await self._db.get(Workspace, workspace_id)
        if not ws or ws.owner_id != user.id:
            raise not_found("workspace", str(workspace_id))
        return ws

    async def get_by_id(self, workspace_id: uuid.UUID) -> Workspace:
        """Internal lookup without an ownership check — for server-side
        callers (e.g. codegen context assembly) that already validated
        access to the parent resource (the codegen session)."""
        ws = await self._db.get(Workspace, workspace_id)
        if not ws:
            raise not_found("workspace", str(workspace_id))
        return ws

    async def create(self, user: User, request: WorkspaceCreate) -> Workspace:
        existing = await self._db.execute(
            select(Workspace).where(
                Workspace.owner_id == user.id,
                Workspace.name == request.name,
            )
        )
        if existing.scalar_one_or_none():
            raise conflict(f"Workspace with name '{request.name}' already exists", code="WORKSPACE_NAME_TAKEN", resource="workspace", field="name")
        settings = get_settings()
        ws_id = uuid.uuid4()
        storage_prefix = f"{settings.org_id}/{user.id}/{ws_id}"
        ws = Workspace(
            id=ws_id,
            owner_id=user.id,
            name=request.name,
            description=request.description,
            workspace_type="personal",
            storage_prefix=storage_prefix,
            default_warehouse=request.default_warehouse,
            default_kernel_image=request.default_kernel_image,
        )
        self._db.add(ws)
        await flush_or_conflict(self._db, f"Workspace with name '{request.name}' already exists", constraint="uq_workspace_owner_name")
        return ws

    async def update(
        self, user: User, workspace_id: uuid.UUID, request: WorkspaceUpdate,
    ) -> Workspace:
        ws = await self.get(user, workspace_id)
        # Rename onto an existing name: a friendly check first, and the unique constraint (owner, name) as the backstop for a race (W-022).
        if request.name is not None and request.name != ws.name:
            taken = (await self._db.execute(
                select(Workspace.id).where(Workspace.owner_id == user.id, Workspace.name == request.name,
                                           Workspace.id != ws.id))).first()
            if taken:
                raise conflict(f"Workspace with name '{request.name}' already exists", code="WORKSPACE_NAME_TAKEN", resource="workspace", field="name")
        new_name = request.name
        if request.name is not None:
            ws.name = request.name
        if request.description is not None:
            ws.description = request.description
        if request.default_warehouse is not None:
            ws.default_warehouse = request.default_warehouse
        if request.default_kernel_image is not None:
            ws.default_kernel_image = request.default_kernel_image
        await flush_or_conflict(self._db, f"Workspace with name '{new_name}' already exists", constraint="uq_workspace_owner_name")
        await self._db.refresh(ws)          # updated_at is server-set; serialising it expired raises MissingGreenlet in the async context
        return ws

    async def delete(self, user: User, workspace_id: uuid.UUID) -> None:
        """Delete the workspace row and every blob under its storage prefix.

        Blobs first, then the row (WS-LIFE-013): once the row is gone,
        `storage_prefix` is gone with it, and nothing could ever find those
        blobs again to sweep them up later — a scheduled reconciler can't
        reconstruct a prefix from a workspace that no longer exists. If the
        blob-store delete fails, the row is left in place so the caller can
        retry rather than orphaning the blobs silently.
        """
        ws = await self.get(user, workspace_id)
        try:
            deleted = await get_blob_store().delete_prefix(ws.storage_prefix)
            log.info("workspace_blobs_deleted", workspace_id=str(ws.id), count=deleted)
        except Exception:
            log.exception("workspace_blob_delete_failed", workspace_id=str(ws.id))
            raise
        await self._db.delete(ws)
