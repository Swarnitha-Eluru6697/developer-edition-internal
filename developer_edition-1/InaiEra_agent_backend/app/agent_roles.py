"""Agent-shaped roles, and how they map onto UCS.

UCS's `access_level` is a fixed pydantic Literal — `select | writer |
owner` — validated inside the UCS service. Confirmed by asking it:

    "Input should be 'select', 'writer' or 'owner'"

Those three are data-table verbs. They say nothing about whether someone
may run an agent, publish a new version of it, or hand access to someone
else, and two of them mean nothing at all for an agent: an agent is
registered as a Gravitino MODEL, whose recognised actions are
read/browse/query/list — `write` is not among them.

The roles here are UCS's own: one per access level, named with the labels
UCS shows for an agent (USE_AGENT, CREATE_AGENT_VERSION, CREATE_AGENT),
so a grant reads the same in this product and in UCS's console. UCS is
sent only the level's privileges; what each level lets someone DO with
the agent (run it, publish, grant access) is the permission list below,
decided by this service from the level UCS reports — see
ucs_client.check_agent_capability.

Two rules this file exists to keep honest:

  * every permission listed is one the platform can point at a real call
    site for, or is explicitly marked as recorded-not-yet-enforced. A
    permission that gates nothing is worse than an absent one, because
    it reads as protection.
  * roles are cumulative. Each includes everything the one below it has,
    so "which role do I need" has a single answer rather than a matrix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# --- permissions ----------------------------------------------------------
#
# `enforced` is the honest part: True means something in this codebase
# genuinely refuses when the permission is absent. False means the grant
# is recorded and readable but nothing consults it yet.

@dataclass(frozen=True)
class Permission:
    key: str
    summary: str
    enforced: bool


# `enforced` reflects what this codebase genuinely refuses.
# ucs_client.check_agent_capability can answer all of them from the UCS
# level a user holds, but a permission only counts as enforced here once
# a route actually asks.
# The routes below that do not yet ask take no invoking_user, so wiring
# them is an API change rather than a one-line call.
PERMISSIONS: Dict[str, Permission] = {p.key: p for p in [
    Permission("agent:read", "See the agent and its configuration", False),
    # check_agent_access now asks UCS for the real `invoke` capability
    # before an end user may open a published app, falling back to the
    # old data-tier read check for grants made before agent roles.
    Permission("agent:invoke", "Open and run the agent", True),
    Permission("agent:write", "Edit the agent's flow and settings", False),
    Permission("agent:version:create", "Publish a new version", False),
    Permission("agent:version:manage", "Restore or retire published versions", False),
    Permission("agent:usage:read", "See runs, traces and cost for the agent", False),
    Permission("agent:entitlement:manage", "Grant and revoke access for others", False),
]}

# The wildcard is stored as-is rather than expanded, so a permission
# added later is covered by Admin without anyone remembering to edit it.
WILDCARD = "agent:*"


@dataclass(frozen=True)
class AgentRole:
    name: str
    label: str
    summary: str
    permissions: List[str]
    # What to grant in UCS alongside it. Viewer and User need only to be
    # able to read the model object; the rest are given the broader level
    # so a future UCS-side check has something to find.
    ucs_access_level: str
    inherits: Optional[str] = None


ROLES: Dict[str, AgentRole] = {r.name: r for r in [
    # Exactly the three agent privileges UCS shows (its labels for the
    # Gravitino MODEL privileges an agent is registered under), so a grant
    # made here reads the same in UCS's console. There is no Viewer or
    # Manager: UCS has no privilege that could tell them apart from the
    # neighbouring level, so offering them here only made two grants look
    # different in this product and identical in UCS.
    AgentRole(
        "use_agent", "USE_AGENT",
        "See and run the agent. What most end users need.",
        ["agent:read", "agent:invoke"],
        ucs_access_level="select",
    ),
    AgentRole(
        "create_agent_version", "CREATE_AGENT_VERSION",
        "Everything in USE_AGENT, plus edit the agent, publish and manage its versions, and see its usage.",
        ["agent:read", "agent:invoke", "agent:write", "agent:version:create",
         "agent:version:manage", "agent:usage:read"],
        ucs_access_level="writer", inherits="use_agent",
    ),
    AgentRole(
        "create_agent", "CREATE_AGENT",
        "Full control, including granting access to others and any permission added in future.",
        [WILDCARD],
        ucs_access_level="owner", inherits="create_agent_version",
    ),
]}

DEFAULT_ROLE = "use_agent"

# Rows written before agent roles existed carry only a UCS tier. Mapped
# to the nearest role rather than left blank, so an existing grant keeps
# meaning what it already meant.
TIER_TO_ROLE = {"select": "use_agent", "writer": "create_agent_version", "owner": "create_agent"}

# The five roles this platform offered before it adopted UCS's privileges.
# Stored Role rows and older clients still carry these names; each maps to
# the UCS level it was already granted at, so nobody's access changes.
LEGACY_ROLES = {
    "agent_viewer": "use_agent", "agent_user": "use_agent",
    "agent_developer": "create_agent_version", "agent_manager": "create_agent_version",
    "agent_admin": "create_agent",
}


def canonical_role(role_name: str) -> str:
    """The current role a stored or requested name means."""
    return LEGACY_ROLES.get(role_name, role_name)


def role_for_tier(tier: str) -> str:
    return TIER_TO_ROLE.get(tier, DEFAULT_ROLE)


UCS_AGENT_TIERS: set = set()          # kept so existing imports keep resolving


def ucs_tier_for_role(role_name: str) -> str:
    """What to send UCS as access_level: the role's own level."""
    return tier_for_role(role_name)


def tier_for_role(role_name: str) -> str:
    """The UCS access_level to grant for an agent role.

    One role per UCS level, so this and role_for_tier are inverses. A
    legacy five-role name is mapped first (see LEGACY_ROLES).
    """
    role = ROLES.get(canonical_role(role_name))
    return role.ucs_access_level if role else "select"


def permissions_for(role_name: str) -> List[str]:
    role = ROLES.get(canonical_role(role_name))
    return list(role.permissions) if role else []


def role_has(role_name: str, permission: str) -> bool:
    """Whether a role carries a permission, honouring the wildcard."""
    perms = permissions_for(role_name)
    return WILDCARD in perms or permission in perms


def groups_have(role_names: List[str], permission: str) -> bool:
    """Whether ANY of the roles a caller holds carries the permission.

    A person is usually in several groups, each bound to its own role, and
    permissions are additive across them — the strongest wins. Taking the
    first or the lowest would make adding a group to someone silently
    reduce what they can do.
    """
    return any(role_has(r, permission) for r in role_names)


def catalogue() -> List[dict]:
    """The whole model, for the UI to render without restating it."""
    return [{
        "name": r.name,
        "label": r.label,
        "summary": r.summary,
        "inherits": r.inherits,
        "ucs_access_level": r.ucs_access_level,
        "permissions": [{
            "key": k,
            "summary": PERMISSIONS[k].summary if k in PERMISSIONS else "Every permission",
            "enforced": PERMISSIONS[k].enforced if k in PERMISSIONS else True,
        } for k in r.permissions],
    } for r in ROLES.values()]
