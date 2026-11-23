"""
Standardized north-south trust boundary for InAiEra data-plane services
behind Envoy, Python/FastAPI side. Mirrors the Node package
(@inaiera/gateway-auth) field-for-field and behavior-for-behavior -- the
same request against a FastAPI service and an Express service should get
the same decision.

v1.1 (contract 01-CONTRACT.md): applications authorise on PERMISSIONS
(`<app>:<area>:<action>`) from the `x-user-permissions` header Envoy sets from
the ticket's `permissions` claim -- never on role names. Roles stay in the
Admin Portal; `x-user-roles` is kept on the identity for display/audit only.
RoleConfig / resolve_role are deprecated and will be removed in the next
release.
"""
import base64
import hmac
import json
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Generic, TypeVar

TRole = TypeVar("TRole", bound=str)

# How Envoy's jwt_authn `claim_to_headers` renders a MULTI-VALUED claim
# (realm_access.roles, groups) into a header: it base64-encodes the JSON, e.g.
# the header value is  WyJyZWFkZXIiLCJhZG1pbiJd  which decodes to ["reader","admin"].
# A string claim (preferred_username) is passed through as-is, and a hand-set
# header is a plain comma list. split_multi handles all three so a real Keycloak
# token resolves correctly instead of falling through to no-access.
_MULTI = re.compile(r'[\s,\[\]"\']+')
_B64 = re.compile(r'[A-Za-z0-9+/_-]+={0,2}')


_NOT_A_LIST: list[str] = []  # sentinel: base64 of valid JSON that is not an array -> invalid -> empty


def _b64_json_list(value: str) -> list[str] | None:
    """If value is base64 of a JSON array of strings, return that array; if it is
    base64 of other JSON, return _NOT_A_LIST (invalid input = empty list); else None."""
    # Only real base64 text: the decoder silently drops other characters, which
    # would let "a:b:view, c:d:edit" be misread as base64 (Node checks the same).
    if not _B64.fullmatch(value):
        return None
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except Exception:
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        except Exception:
            return None
    decoded = decoded.strip()
    try:
        arr = json.loads(decoded)
    except Exception:
        return None
    if isinstance(arr, list):
        return [str(x).strip() for x in arr if str(x).strip()]
    return _NOT_A_LIST


def split_multi(value: str | None) -> list[str]:
    """Parse a list header (permissions, roles, groups) in any of the forms Envoy or
    a client may send: base64(JSON array) [Envoy list claim], a raw JSON array
    string, or a comma list. Contract section 4."""
    if not value:
        return []
    v = value.strip()
    arr = _b64_json_list(v)
    if arr is not None:
        return list(arr)
    if v.startswith("["):
        try:
            j = json.loads(v)
            if isinstance(j, list):
                return [str(x).strip() for x in j if str(x).strip()]
        except Exception:
            pass
    return [t for t in _MULTI.split(v) if t]


def verify_gateway_secret(expected: str | None, provided: str | None) -> bool:
    """Constant-time check that a request actually came through Envoy.
    Fails closed: an unconfigured secret means every request is rejected,
    never silently trusted. Identical logic to the Node package's
    verifyGatewaySecret -- this is the one piece with zero legitimate
    per-service variation, which is exactly why it's shared rather than
    reimplemented per service.
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


_ROLE_DEPRECATION = (
    "RoleConfig/resolve_role are deprecated: authorise on permissions "
    "(require_permission / has_permission) instead of role names. "
    "They will be removed in the next release."
)


@dataclass
class RoleConfig(Generic[TRole]):
    """Deprecated (v1.1): role names belong in the Admin Portal, not in code."""
    all_roles: tuple[TRole, ...]
    tiers: dict[TRole, int]
    default_role: TRole  # must be the LOWEST tier, never the highest

    def __post_init__(self):
        warnings.warn(_ROLE_DEPRECATION, DeprecationWarning, stacklevel=3)


def resolve_role(roles_header: str | None, config: "RoleConfig[TRole]") -> TRole:
    """Deprecated (v1.1). Split on comma, keep only this service's recognized
    roles, pick the highest tier if more than one matches, default to
    least-privileged if none match.
    """
    warnings.warn(_ROLE_DEPRECATION, DeprecationWarning, stacklevel=2)
    claimed = split_multi(roles_header)
    matched = [r for r in claimed if r in config.all_roles]
    if not matched:
        return config.default_role
    return max(matched, key=lambda r: config.tiers[r])


@dataclass
class GatewayIdentity:
    username: str
    groups: list[str] = field(default_factory=list)
    tenant_id: str = ""
    user_id: str = ""                                   # x-user-id (sub): immutable user id
    permissions: frozenset[str] = frozenset()           # x-user-permissions: what the user may do
    roles: tuple[str, ...] = ()                         # x-user-roles: display/audit ONLY, never authorise on it


def extract_identity(
    username: str | None,
    groups: str | None,
    tenant_id: str | None,
    default_tenant_id: str,
    *,
    user_id: str | None = None,
    permissions: str | None = None,
    roles: str | None = None,
) -> GatewayIdentity | None:
    """Identity from Envoy's headers, once the gateway secret has been verified.
    None when there is no username (no identity at all)."""
    if not username:
        return None
    return GatewayIdentity(
        username=username,
        groups=split_multi(groups),
        tenant_id=tenant_id or default_tenant_id,
        user_id=(user_id or "").strip(),
        permissions=frozenset(split_multi(permissions)),
        roles=tuple(split_multi(roles)),
    )


def has_permission(principal, permission: str) -> bool:
    """True if the principal (or identity) holds `permission` exactly.
    Unknown principal or no permissions = False: least privilege is "nothing"."""
    perms = getattr(principal, "permissions", None)
    return bool(perms) and permission in perms
