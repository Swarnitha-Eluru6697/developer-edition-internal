"""Workspace association is an authorized resource, not request JSON (W-016 / FL-003 / DBT-012).

The dbt and Flink runners store a project's / job's `workspace_id` as a grouping label. They cannot know whether the
caller may use that workspace (the `workspaces` table lives here), so before this module the Workspace proxies forwarded
whatever UUID the browser sent: any same-org user with the dbt or Flink capability could attach their project or job to
ANOTHER user's workspace and pollute its views.

Now a `workspace_id` is forwarded only after `authorize_workspace` checked that the caller owns that workspace (a workspace
that is not theirs answers 404, exactly like every other workspace route), and the verified id travels in the signed
delegated token as a `workspace_id` claim. The runners accept a workspace in a request body only when it equals that claim.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.errors import validation_failed
from app.services.workspace_service import WorkspaceService

# W-021: the collaboration model of this release. Workspaces are PERSONAL: exactly one owner, no membership or ACL, no sharing,
# no review/approval between users. This is a declared product decision, not a gap that was left open: it is exposed to clients
# (GET /auth/me -> workspace_model), enforced by the service (every workspace route is owner-only: another user's workspace is a
# 404) and guarded by tests/test_workspace_model.py, which fails if a membership/ACL table, a client-settable workspace type or an
# owner-less workspace appears without this decision being revisited. The runners' `workspace_id` is a grouping LABEL vouched for
# by this service (above), never an access-control list. Shared workspaces would need: a membership table with roles
# (owner / editor / runner / viewer), ACL checks in every workspace route, and a stable workspace resource identity propagated to
# the runners and DQ. See docs/RESOURCE_SCOPING.md.
WORKSPACE_MODEL = "personal"


def requested_workspace_id(body: bytes) -> uuid.UUID | None:
    """The `workspace_id` a client put in a JSON request body, or None. A value that is not a UUID is a 422: it must not
    be forwarded to a runner either way."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    raw = data.get("workspace_id") if isinstance(data, dict) else None
    if raw in (None, ""):
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise validation_failed("workspace_id", "workspace_id must be a UUID") from None


async def authorize_workspace(db: Any, principal: Any, requested: uuid.UUID | None) -> uuid.UUID | None:
    """`requested` if `principal` owns that workspace; None when nothing was requested. A workspace that does not exist
    or belongs to someone else raises the same 404 as GET /workspaces/{id}."""
    if requested is None:
        return None
    svc = WorkspaceService(db)
    user = await svc.ensure_user(principal)
    workspace = await svc.get(user, requested)
    return workspace.id
