"""BUG-05: every API route must require a declared dataconnect:* permission.

Individual tests in test_rbac.py prove that particular routes are wired up, but a route added later with
no `require(...)` dependency would sail through them. This walks the REAL route table instead, so
forgetting the dependency on any route — read or write — fails here. It also produces the route ->
permission table documented in docs/auth/PERMISSIONS.md and checks every declared permission is either
enforced by some route or explicitly UI-only.
"""
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from gateway_auth import Principal, get_principal

from app.core.permissions import all_permissions, require
from app.main import app

# Probes: called by load balancers / orchestrators with no user, so they are the gateway's public paths.
# /metrics carries no route-level permission: it is behind the gateway (401) unless an operator sets
# DATACONNECT_METRICS_PUBLIC=true for an in-cluster scraper (see app/core/gateway.py).
_OPEN_ON_PURPOSE = {
    ("GET", "/metrics"),
    ("GET", "/api/v1/dataconnect/health"),
    ("GET", "/api/v1/dataconnect/health/live"),
    ("GET", "/api/v1/dataconnect/health/ready"),
    # The Acknowledge / Resolve buttons in a Slack / Teams alert: the person clicking is in Slack / Teams, not signed in, so the credential is the
    # SIGNED token in the path (HMAC, one alert + one action + one channel, expiring) — app/api/routes/alert_actions.py, services/alerts/links.py.
    ("GET", "/api/v1/dataconnect/alerts/actions/{token}"),
    ("POST", "/api/v1/dataconnect/alerts/actions/{token}"),
}

# Authenticated, but needing no particular permission: GET /me reports the caller's own identity and
# permissions (an empty list is a valid answer). Still required to depend on get_principal.
_ANY_PRINCIPAL = {
    ("GET", "/api/v1/dataconnect/me"),
}

# Declared permissions no server route enforces: they gate UI pages only (the backend cannot enforce them).
# dataconnect:access:view is the portal's "may open the app" menu gate; dashboard/settings views are client pages.
_UI_ONLY = {"dataconnect:access:view", "dataconnect:dashboard:view", "dataconnect:settings:view"}


def _needs_principal(dependant: Dependant) -> bool:
    return any(d.call is get_principal or _needs_principal(d) for d in dependant.dependencies)


def _permissions_of(dependant: Dependant) -> set[str]:
    found: set[str] = set()
    for d in dependant.dependencies:
        perm = getattr(d.call, "dataconnect_permission", None)
        if perm:
            found.add(perm)
        found |= _permissions_of(d)
    return found


def _enforces_a_capability(dependant: Dependant) -> bool:
    return bool(_permissions_of(dependant))


def _all_routes(fastapi_app: FastAPI):
    """Yields (path, methods, dependant) for every route, including those behind include_router().
    Newer FastAPI keeps included routers lazy (an _IncludedRouter object in app.routes) and exposes the
    flattened routes through effective_route_contexts(); older versions flatten them into app.routes."""
    for route in fastapi_app.routes:
        if isinstance(route, APIRoute):
            yield route.path, route.methods, route.dependant
        elif hasattr(route, "effective_route_contexts"):
            for ctx in route.effective_route_contexts():
                yield ctx.path, ctx.methods, ctx.dependant


def routes_without_a_capability_check(fastapi_app: FastAPI) -> list[tuple[str, str]]:
    missing = []
    for path, methods, dependant in _all_routes(fastapi_app):
        for method in sorted(set(methods) - {"HEAD", "OPTIONS"}):
            if (method, path) in _OPEN_ON_PURPOSE:
                continue
            if (method, path) in _ANY_PRINCIPAL:
                if not _needs_principal(dependant):
                    missing.append((method, path))
                continue
            if not _enforces_a_capability(dependant):
                missing.append((method, path))
    return missing


def test_every_api_route_enforces_a_permission() -> None:
    missing = routes_without_a_capability_check(app)

    assert missing == [], (
        "These routes have no require(...) permission dependency, so any authenticated caller can use them: "
        f"{missing}"
    )


def test_the_scan_really_sees_the_whole_api() -> None:
    """Guard against the walker silently scanning nothing (e.g. after a FastAPI upgrade changes how
    included routers are stored) — a green 'no route is missing a check' would then be meaningless."""
    scanned = {(m, path) for path, methods, _ in _all_routes(app) for m in methods}

    assert len(scanned) >= 80
    assert ("GET", "/api/v1/dataconnect/pipelines") in scanned
    assert ("POST", "/api/v1/dataconnect/pipelines/{pipeline_id}/deploy") in scanned


def test_the_open_routes_allow_list_is_not_stale() -> None:
    """If a probe route is renamed/removed the allow-list must shrink with it, not quietly cover a
    different route that later reuses the path."""
    existing = {(m, path) for path, methods, _ in _all_routes(app) for m in methods}

    assert _OPEN_ON_PURPOSE <= existing
    assert _ANY_PRINCIPAL <= existing


def test_the_check_really_catches_a_route_that_forgot_its_permission() -> None:
    """Meta-test: prove the walker would have flagged the pre-fix pattern, so a green result above means something."""
    demo = FastAPI()

    @demo.get("/guarded")
    def guarded(_p: Principal = Depends(require("dataconnect:pipelines:view"))) -> dict:
        return {}

    @demo.get("/forgotten")
    def forgotten() -> dict:
        return {}

    def nested(_p: Principal = Depends(require("dataconnect:pipelines:view"))) -> None:
        return None

    @demo.get("/guarded-through-another-dependency")
    def guarded_via_nested(_dep: None = Depends(nested)) -> dict:
        return {}

    assert routes_without_a_capability_check(demo) == [("GET", "/forgotten")]


def test_an_open_route_that_is_not_allow_listed_is_flagged() -> None:
    demo = FastAPI()

    @demo.get("/new-unprotected-read")
    def unprotected() -> dict:
        return {}

    assert routes_without_a_capability_check(demo) == [("GET", "/new-unprotected-read")]


def route_permission_table(fastapi_app: FastAPI) -> dict[tuple[str, str], set[str]]:
    table = {}
    for path, methods, dependant in _all_routes(fastapi_app):
        for method in sorted(set(methods) - {"HEAD", "OPTIONS"}):
            table[(method, path)] = _permissions_of(dependant)
    return table


def test_every_route_requires_exactly_one_permission() -> None:
    multi = {k: v for k, v in route_permission_table(app).items() if len(v) > 1}
    assert multi == {}


def test_every_declared_permission_is_enforced_somewhere_or_ui_only() -> None:
    enforced = set().union(*route_permission_table(app).values())
    assert enforced <= all_permissions()
    assert all_permissions() - enforced == _UI_ONLY
