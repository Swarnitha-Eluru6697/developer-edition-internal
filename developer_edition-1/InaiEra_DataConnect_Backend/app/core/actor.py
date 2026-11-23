"""
Actor identity for audit records — WHO performed a mutating action.

Taken from the gateway-verified principal (Envoy's x-user-name, from the ticket's preferred_username), never from a
client-supplied header: the old X-Actor header is no longer read, so a caller cannot write someone else's name into
the audit trail.
"""

from fastapi import Depends
from gateway_auth import Principal

from app.core.gateway import get_principal


def get_actor(principal: Principal = Depends(get_principal)) -> str:
    return principal.username or principal.user_id or "unknown"
