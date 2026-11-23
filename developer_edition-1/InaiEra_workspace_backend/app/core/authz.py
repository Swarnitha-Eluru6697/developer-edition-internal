"""Resource-ownership authorization helpers.

Every session/execution lookup or action — REST or WebSocket — must resolve
ownership through here instead of comparing IDs ad hoc, so a check can't be
computed and then silently ignored (as the previous session-termination code
did).

Org isolation is enforced upstream in `get_current_user` (a data plane
only accepts callers whose gateway-asserted tenant is its own
`settings.org_id`), so every row in this database already belongs to the
caller's org. What these helpers add is the per-user ownership check on top
of that.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import conflict, not_found
from app.models import Execution, Session as SessionModel


async def get_owned_session(
    db: AsyncSession, user_id: UUID, session_id: UUID, *, for_use: bool = False,
) -> SessionModel:
    """Fetch a session, raising 404 unless it belongs to `user_id`.

    404 (not 403) on a mismatch so a guessed/borrowed ID doesn't confirm
    another user's session exists.

    `for_use=True` is for actions that run work in the session (submit,
    WebSocket ticket, codegen): a terminated session is refused with 409,
    and an idle one — closed out for inactivity by the lifecycle reconciler,
    the tab may still be open — is resumed.
    """
    session = await db.get(SessionModel, session_id)
    if not session or session.user_id != user_id:
        raise not_found("session", str(session_id))
    if for_use:
        if session.status == "terminated":
            raise conflict("This session has ended. Reload to start a new one.", code="SESSION_ENDED", resource="session", field="status")
        if session.status == "idle":
            session.status = "active"
            session.ended_at = None
            session.last_activity_at = datetime.now(timezone.utc)
    return session


async def get_owned_execution(db: AsyncSession, user_id: UUID, execution_id: UUID) -> Execution:
    """Fetch an execution, raising 404 unless it belongs to `user_id`."""
    execution = await db.get(Execution, execution_id)
    if not execution or execution.user_id != user_id:
        raise not_found("execution", str(execution_id))
    return execution
