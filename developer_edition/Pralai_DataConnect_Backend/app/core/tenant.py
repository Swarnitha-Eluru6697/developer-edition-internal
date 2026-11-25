"""
Tenant / org-id for a request — taken from the gateway-verified principal (Envoy's x-tenant-id, set from the ticket's
`tenant` claim), never from a client-supplied header. The old X-Org-Id header is no longer read: a caller cannot pick
another organisation's data by sending a different value.
"""

import re

from fastapi import Depends, HTTPException, status
from gateway_auth import Principal

from app.core.gateway import get_principal

ORG_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,100}$")


def _validate_org_id(org_id: str) -> str:
    if not ORG_ID_RE.fullmatch(org_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The tenant in the gateway identity must be 3–100 alphanumeric characters (_, - allowed).",
        )
    return org_id


def get_org_id(principal: Principal = Depends(get_principal)) -> str:
    """FastAPI dependency: the caller's tenant, from the gateway principal."""
    return _validate_org_id(principal.tenant_id)
