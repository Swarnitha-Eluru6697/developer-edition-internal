"""FastAPI side of gateway_auth (contract 01-CONTRACT.md).

    from gateway_auth import protect_app, require_permission

    app = FastAPI()
    protect_app(app, public_paths=["/health", "/static/*"])      # 401 unless via Envoy

    @app.get("/api/clusters", dependencies=[Depends(require_permission("mlstudio:clusters:view"))])
    def clusters(): ...                                           # 403 without the permission

    @app.get("/api/me")
    def me(principal = Depends(get_principal)):
        return principal.me()

Deny by default: protect_app refuses every route that is not on the explicit
public list unless TRUST_ENVOY_HEADERS=true AND the request carries Envoy's
x-inaiera-gateway-secret. Authorisation is by permission only; role names are
never consulted (x-user-roles is kept on the principal for audit).
"""
import os
import warnings
from typing import Iterable, Mapping

from fastapi import Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .core import (
    GatewayIdentity, RoleConfig, TRole, _ROLE_DEPRECATION, extract_identity,
    has_permission, resolve_role, verify_gateway_secret,
)

TRUST_ENV = "TRUST_ENVOY_HEADERS"
SECRET_ENV = "ENVOY_INTERNAL_SECRET"


class Principal:
    """A gateway-verified caller. `permissions` decides; `roles` is audit only."""

    def __init__(self, identity: GatewayIdentity, role: str | None = None):
        self.identity = identity
        self.username = identity.username
        self.user_id = identity.user_id
        self.groups = identity.groups
        self.tenant_id = identity.tenant_id
        self.permissions = identity.permissions
        self.roles = identity.roles
        self.role = role  # only set by the deprecated make_gateway_dependency

    def has(self, permission: str) -> bool:
        return has_permission(self, permission)

    def me(self) -> dict:
        """The contract's GET /me body."""
        return {"user": self.username, "userId": self.user_id, "tenant": self.tenant_id,
                "permissions": sorted(self.permissions)}


class GatewayAuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def principal_from_headers(
    headers: Mapping[str, str],
    default_tenant_id: str = "default",
    trust_envoy_headers_env: str = TRUST_ENV,
    gateway_secret_env: str = SECRET_ENV,
) -> Principal:
    """The one gate. Raises GatewayAuthError(401) unless the request provably came
    through Envoy and carries an identity. `headers` must be case-insensitive
    (Starlette's request.headers is)."""
    if os.environ.get(trust_envoy_headers_env) != "true":
        raise GatewayAuthError(401, "TRUST_ENVOY_HEADERS is not enabled -- refusing to trust any identity header")
    if not verify_gateway_secret(os.environ.get(gateway_secret_env), headers.get("x-inaiera-gateway-secret")):
        raise GatewayAuthError(401, "request did not come through the gateway")
    identity = extract_identity(
        headers.get("x-user-name"), headers.get("x-user-groups"), headers.get("x-tenant-id"), default_tenant_id,
        user_id=headers.get("x-user-id"), permissions=headers.get("x-user-permissions"),
        roles=headers.get("x-user-roles"),
    )
    if identity is None:
        raise GatewayAuthError(401, "missing identity headers")
    return Principal(identity)


def _is_public(path: str, public_paths: Iterable[str]) -> bool:
    """Exact match, or prefix match for an entry ending in '*' ("/static/*")."""
    for p in public_paths:
        if p.endswith("*"):
            if path.startswith(p[:-1]):
                return True
        elif path == p or path == p.rstrip("/") or path.rstrip("/") == p:
            return True
    return False


def protect_app(
    app,
    public_paths: Iterable[str] = ("/health", "/healthz", "/livez", "/readyz", "/api/health"),
    default_tenant_id: str = "default",
    trust_envoy_headers_env: str = TRUST_ENV,
    gateway_secret_env: str = SECRET_ENV,
):
    """Require a gateway-verified principal on every route except `public_paths`
    (contract 5.1/5.2). The principal is left on request.state.principal, where
    get_principal / require_permission pick it up without re-checking."""
    public = tuple(public_paths)

    @app.middleware("http")
    async def _gateway_auth(request: Request, call_next):
        if _is_public(request.url.path, public):
            return await call_next(request)
        try:
            request.state.principal = principal_from_headers(
                request.headers, default_tenant_id, trust_envoy_headers_env, gateway_secret_env)
        except GatewayAuthError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return await call_next(request)

    return app


def make_principal_dependency(
    default_tenant_id: str = "default",
    trust_envoy_headers_env: str = TRUST_ENV,
    gateway_secret_env: str = SECRET_ENV,
):
    """FastAPI dependency returning the Principal (401 if none). Works with or
    without protect_app."""

    def dependency(request: Request) -> Principal:
        p = getattr(request.state, "principal", None)
        if isinstance(p, Principal):
            return p
        try:
            p = principal_from_headers(request.headers, default_tenant_id, trust_envoy_headers_env, gateway_secret_env)
        except GatewayAuthError as e:
            raise HTTPException(e.status_code, e.detail)
        request.state.principal = p
        return p

    return dependency


get_principal = make_principal_dependency()


def require_permission(
    permission: str,
    default_tenant_id: str = "default",
    trust_envoy_headers_env: str = TRUST_ENV,
    gateway_secret_env: str = SECRET_ENV,
):
    """Dependency: 401 without a gateway principal, 403 without `permission`."""
    get = make_principal_dependency(default_tenant_id, trust_envoy_headers_env, gateway_secret_env)

    def dependency(request: Request) -> Principal:
        p = get(request)
        if not p.has(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission {permission}")
        return p

    return dependency


def make_gateway_dependency(
    roles: "RoleConfig[TRole]",
    default_tenant_id: str = "default",
    trust_envoy_headers_env: str = TRUST_ENV,
    gateway_secret_env: str = SECRET_ENV,
):
    """Deprecated (v1.1): use protect_app + require_permission. Same gate as
    before, plus the principal now carries user_id and permissions."""
    warnings.warn(_ROLE_DEPRECATION, DeprecationWarning, stacklevel=2)

    def dependency(
        x_user_name: str | None = Header(default=None),
        x_user_groups: str | None = Header(default=None),
        x_user_roles: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_permissions: str | None = Header(default=None),
        x_inaiera_gateway_secret: str | None = Header(default=None),
    ) -> Principal:
        headers = {k: v for k, v in {
            "x-user-name": x_user_name, "x-user-groups": x_user_groups, "x-user-roles": x_user_roles,
            "x-tenant-id": x_tenant_id, "x-user-id": x_user_id, "x-user-permissions": x_user_permissions,
            "x-inaiera-gateway-secret": x_inaiera_gateway_secret,
        }.items() if v is not None}
        try:
            p = principal_from_headers(headers, default_tenant_id, trust_envoy_headers_env, gateway_secret_env)
        except GatewayAuthError as e:
            raise HTTPException(e.status_code, e.detail)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            p.role = resolve_role(x_user_roles, roles)
        return p

    return dependency
