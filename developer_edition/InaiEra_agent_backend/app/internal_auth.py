"""The operator boundary for maintenance operations.

Storage sweeping, bulk backend migration and reconciliation live under
/internal (routers/internal.py): `migrate-backend` moves up to 10,000
objects per call and `sweep-expired` deletes rows together with their
storage objects.

Two independent defences, because either alone can be misconfigured:

  1. The /internal prefix, which an ingress or reverse proxy can refuse in
     one rule.
  2. This dependency: a gateway-verified principal (protect_app, app/main.py)
     holding the permission `agent:maintenance:run`. Which people hold it is
     data in the Admin Portal, not a role name here.

The supported path for a scheduler or a Kubernetes Job is the CLI
(`python -m app.maintenance`), which needs no network surface at all.

Deliberately a 403 with a plain reason rather than a 404: hiding the route's
existence protects nothing that the prefix block and the fail-closed default
do not already protect.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from .permissions import MAINTENANCE


def require_operator(request: Request) -> str:
    """Who is running this maintenance operation, or a refusal.

    Returned rather than discarded so each route can log the operator
    alongside what the operation actually did.
    """
    from .gateway_identity import get_principal

    principal = get_principal(request)          # 401 without a gateway principal
    if not principal.has(MAINTENANCE):
        raise HTTPException(
            403,
            f"Maintenance operations require the permission {MAINTENANCE}. "
            "Without it, use the CLI instead — `python -m app.maintenance --help`.",
        )
    return principal.username
