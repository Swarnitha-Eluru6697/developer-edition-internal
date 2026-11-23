import { Request, Response, NextFunction } from "express";
import { verifyGatewaySecret } from "./gatewaySecret";
import { resolveRole, RoleConfig } from "./resolveRole";
import { extractIdentity, hasPermission, GatewayIdentity } from "./identity";

export { verifyGatewaySecret } from "./gatewaySecret";
export { resolveRole, RoleConfig } from "./resolveRole";
export { extractIdentity, hasPermission, GatewayIdentity } from "./identity";

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      /** The gateway-verified caller (v1.1). Authorise on principal.permissions only. */
      principal?: GatewayIdentity;
      /** Same object as principal; kept for v1.0 callers. */
      gatewayIdentity: GatewayIdentity;
      /** Deprecated: only set by createGatewayMiddleware with a RoleConfig. */
      role: string;
    }
  }
}

export interface GatewayAuthOptions {
  /** Reads process.env.ENVOY_INTERNAL_SECRET if omitted. */
  gatewaySecret?: string;
  /** Reads process.env.TRUST_ENVOY_HEADERS === "true" if omitted. */
  trustEnvoyHeaders?: boolean;
  defaultTenantId?: string;
  /**
   * Routes that need no principal (contract 5.2): health probes, static assets,
   * self-verifying webhooks. Exact paths, or a prefix ending in "*" ("/static/*").
   * Default: the standard health paths.
   */
  publicPaths?: string[];
}

const DEFAULT_PUBLIC = ["/health", "/healthz", "/livez", "/readyz", "/api/health"];

export function isPublicPath(path: string, publicPaths: readonly string[]): boolean {
  return publicPaths.some((p) =>
    p.endsWith("*") ? path.startsWith(p.slice(0, -1)) : path === p || path.replace(/\/+$/, "") === p.replace(/\/+$/, ""));
}

type Gate = { ok: true; principal: GatewayIdentity } | { ok: false; status: 401; error: string };

/** The one gate: did this request provably come through Envoy, and who is it? */
export function principalFromRequest(req: Request, options: GatewayAuthOptions = {}): Gate {
  const trust = options.trustEnvoyHeaders ?? (process.env.TRUST_ENVOY_HEADERS === "true");
  const secret = options.gatewaySecret ?? process.env.ENVOY_INTERNAL_SECRET;
  if (!trust) return { ok: false, status: 401, error: "TRUST_ENVOY_HEADERS is not enabled -- refusing to trust any identity header" };
  if (!verifyGatewaySecret(secret, req.header("X-Inaiera-Gateway-Secret"))) {
    return { ok: false, status: 401, error: "request did not come through the gateway" };
  }
  const principal = extractIdentity({
    username: req.header("X-User-Name"),
    groups: req.header("X-User-Groups"),
    tenantId: req.header("X-Tenant-Id"),
    userId: req.header("X-User-Id"),
    permissions: req.header("X-User-Permissions"),
    roles: req.header("X-User-Roles"),
  }, options.defaultTenantId ?? "default");
  if (!principal) return { ok: false, status: 401, error: "missing identity headers" };
  return { ok: true, principal };
}

/**
 * App-wide deny by default (contract 5.1). Mount before every router:
 *   app.use(gatewayAuth({ publicPaths: ["/api/health"] }));
 * Every non-public route then has req.principal, or the request got 401.
 */
export function gatewayAuth(options: GatewayAuthOptions = {}) {
  const publicPaths = options.publicPaths ?? DEFAULT_PUBLIC;
  return function gatewayAuthMiddleware(req: Request, res: Response, next: NextFunction) {
    if (isPublicPath(req.path, publicPaths)) return next();
    const gate = principalFromRequest(req, options);
    if (!gate.ok) return res.status(gate.status).json({ error: gate.error });
    req.principal = gate.principal;
    req.gatewayIdentity = gate.principal;
    next();
  };
}

/**
 * Route guard: 401 without a gateway principal, 403 without `permission`.
 *   router.get("/clusters", requirePermission("mlstudio:clusters:view"), handler)
 * Works with or without gatewayAuth in front (it runs the gate itself if needed).
 */
export function requirePermission(permission: string, options: GatewayAuthOptions = {}) {
  return function requirePermissionMiddleware(req: Request, res: Response, next: NextFunction) {
    if (!req.principal) {
      const gate = principalFromRequest(req, options);
      if (!gate.ok) return res.status(gate.status).json({ error: gate.error });
      req.principal = gate.principal;
      req.gatewayIdentity = gate.principal;
    }
    if (!hasPermission(req.principal, permission)) {
      return res.status(403).json({ error: `missing permission ${permission}` });
    }
    next();
  };
}

/** The contract's GET /me body. */
export function meBody(principal: GatewayIdentity) {
  return { user: principal.username, userId: principal.userId, tenant: principal.tenantId, permissions: [...principal.permissions].sort() };
}

export interface GatewayMiddlewareOptions<TRole extends string> extends GatewayAuthOptions {
  roles: RoleConfig<TRole>;
}

/**
 * DEPRECATED (v1.1): use gatewayAuth + requirePermission. Same gate as v1.0
 * (no public paths: every route behind it needs Envoy); additionally sets
 * req.principal with userId and permissions.
 */
export function createGatewayMiddleware<TRole extends string>(options: GatewayMiddlewareOptions<TRole>) {
  return function gatewayMiddleware(req: Request, res: Response, next: NextFunction) {
    const gate = principalFromRequest(req, options);
    if (!gate.ok) return res.status(gate.status).json({ error: gate.error });
    req.principal = gate.principal;
    req.gatewayIdentity = gate.principal;
    req.role = resolveRole(req.header("X-User-Roles"), options.roles);
    next();
  };
}
