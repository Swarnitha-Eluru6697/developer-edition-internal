"""
Permission-based authorisation for DataConnect (contract: docs/auth/01-CONTRACT.md).

DataConnect checks PERMISSIONS only — `dataconnect:<area>:<action>` IDs that Envoy forwards from the user's ticket in
`x-user-permissions`. Which person holds which permission is decided by roles in the Admin Portal; no role name appears in
this application, so a new role (or a changed one) needs no code change and no redeploy here.

The full list of IDs lives in ONE place: `permissions.json` at the repo root (contract section 7), which the Admin Portal
imports. This module loads it once, and `require()` refuses — at import time — any ID that is not declared there, so a
typo in a route fails startup instead of silently 403ing everyone.

    @router.get("", dependencies=[...])
    async def list_connections(_principal: Principal = Depends(require("dataconnect:connections:view"))): ...

The check itself is the shared gateway-auth library's `require_permission` (401 without a gateway principal, 403
without the permission); this module only adds the declared-ID guard and a marker the route-coverage test reads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from gateway_auth import Principal, require_permission

APP = "dataconnect"
PERMISSIONS_FILE = Path(__file__).resolve().parents[2] / "permissions.json"


@lru_cache(maxsize=1)
def permission_catalog() -> dict[str, Any]:
    """The parsed permissions.json (contract section 7)."""
    with PERMISSIONS_FILE.open(encoding="utf-8") as f:
        catalog = json.load(f)
    if catalog.get("app") != APP:
        raise RuntimeError(f"{PERMISSIONS_FILE} declares app {catalog.get('app')!r}, expected {APP!r}")
    return catalog


@lru_cache(maxsize=1)
def all_permissions() -> frozenset[str]:
    """Every permission ID DataConnect declares."""
    return frozenset(p["id"] for p in permission_catalog()["permissions"])


def require(permission: str) -> Callable[..., Principal]:
    """FastAPI dependency: 401 without a gateway-verified principal, 403 unless it holds `permission`.

    `permission` must be declared in permissions.json; anything else raises at import time."""
    if permission not in all_permissions():
        raise ValueError(f"{permission!r} is not declared in {PERMISSIONS_FILE.name}")
    dependency = require_permission(permission)
    # Read by tests/test_route_permission_coverage.py to prove every route declares a permission.
    dependency.dataconnect_permission = permission  # type: ignore[attr-defined]
    return dependency
