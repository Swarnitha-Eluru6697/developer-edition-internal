"""Workspace association is accepted only when Workspace vouched for it (DBT-012 / FL-003 / W-016).

The registry here is shared by the whole org: every caller with the right capability sees every project/job of the org. A
`workspace_id` is a GROUPING LABEL (it lets the Workspace UI show "this workspace's" projects), NOT an ownership or isolation
boundary. Because the `workspaces` table lives in the Workspace service, this service cannot tell whether a caller may use a
given workspace, so it never trusts a `workspace_id` from request JSON: Workspace checks that the caller owns the workspace
and puts it in its signed delegated token as the `workspace_id` claim; a body workspace is accepted only if it equals that
claim. A caller without the claim (an external CI principal, an older Workspace) simply omits the workspace.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, status


def authorize_workspace(principal, requested: uuid.UUID | None) -> uuid.UUID | None:
    """`requested` if the caller's token vouches for exactly that workspace; None when none was requested."""
    if requested is None:
        return None
    vouched = (getattr(principal, "workspace_id", None) or "").lower()
    if vouched and vouched == str(requested).lower():
        return requested
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
        "code": "WORKSPACE_NOT_AUTHORIZED",
        "message": "workspace_id is accepted only when the Workspace service has verified it for this call "
                   "(it travels in the delegated token); omit it, or register through Workspace"})
