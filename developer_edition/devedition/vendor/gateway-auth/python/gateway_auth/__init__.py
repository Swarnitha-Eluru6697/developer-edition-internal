from .core import (
    GatewayIdentity, RoleConfig, extract_identity, has_permission, resolve_role, split_multi,
    verify_gateway_secret,
)
from .fastapi import (
    GatewayAuthError, Principal, get_principal, make_gateway_dependency, make_principal_dependency,
    principal_from_headers, protect_app, require_permission,
)

__version__ = "1.1.0"

__all__ = [
    # v1.1 permission API
    "protect_app", "require_permission", "get_principal", "make_principal_dependency",
    "principal_from_headers", "has_permission", "Principal", "GatewayAuthError",
    # shared building blocks
    "GatewayIdentity", "extract_identity", "split_multi", "verify_gateway_secret",
    # deprecated (role names); removed in the next release
    "RoleConfig", "resolve_role", "make_gateway_dependency",
]
