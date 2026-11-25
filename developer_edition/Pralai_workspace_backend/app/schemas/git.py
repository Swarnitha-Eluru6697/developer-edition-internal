"""Git integration schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

Provider = Literal["github", "gitlab", "azure_devops"]


class GitBindingCreate(BaseModel):
    provider: Provider
    repo_url: str
    default_branch: str = "main"


class GitBindingOut(ORMModel):
    id: UUID
    workspace_id: UUID
    provider: str
    repo_url: str
    repo_full_name: str | None
    default_branch: str
    current_branch: str
    last_synced_sha: str | None
    # W-014: the ref the user explicitly asked for (None = the repository default); last_synced_sha is what was really checked out.
    requested_ref: str | None = None
    last_synced_at: datetime | None
    status: str
    status_reason: str | None


class GitStatus(BaseModel):
    branch: str
    ahead: int
    behind: int
    modified_files: list[str]
    untracked_files: list[str]
    last_commit_sha: str | None
    last_commit_message: str | None


class CommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    file_ids: list[UUID] | None = None  # None = commit everything dirty
    allow_empty: bool = False  # allow commits when there are no dirty files


class CommitOut(BaseModel):
    commit_sha: str
    branch: str
    committed_file_count: int


class BranchCreate(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9._\-/]+$", max_length=255)
    from_branch: str | None = None  # default to current_branch


class PullRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = ""
    source_branch: str
    target_branch: str = "main"


class PullRequestOut(BaseModel):
    provider: str
    pr_number: int
    url: str
    state: Literal["open", "closed", "merged"]
    source_branch: str
    target_branch: str


class RepoListItem(BaseModel):
    id: str
    name: str
    full_name: str
    default_branch: str
    clone_url: str
    private: bool


class OAuthInitiateOut(BaseModel):
    authorize_url: str
    state: str
