"""GET /me — who the caller is and what they may do (contract section 5.4).

The UI builds its pages and buttons from `permissions` here; the per-route checks remain the real control. Needs a
gateway-verified principal but no particular permission: a user with none still gets 200 and an empty list, so the UI
can say "you have no access" instead of failing.
"""

from fastapi import APIRouter, Depends
from gateway_auth import Principal
from pydantic import BaseModel, Field

from app.core.gateway import get_principal

router = APIRouter()


class Me(BaseModel):
    user: str = Field(description="Display name (x-user-name / preferred_username).")
    userId: str = Field(description="Immutable user id (x-user-id / sub).")  # noqa: N815 — contract field name
    tenant: str = Field(description="The caller's tenant (x-tenant-id); every request is scoped to it.")
    permissions: list[str] = Field(description="Every permission the caller holds, sorted.")


@router.get("/me", response_model=Me, summary="The caller's identity, tenant and permissions")
async def me(principal: Principal = Depends(get_principal)) -> dict:
    return principal.me()
